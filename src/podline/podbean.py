import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import Dict, List, Optional, Protocol

import httpx

from podline.artifacts import ArtifactError, materialize_artifact
from podline.config import AppConfig
from podline.db import connect
from podline.models import (
    ArtifactKind,
    EpisodeRecord,
    RuntimePaths,
    SessionRecord,
    SessionStatus,
)
from podline.repository import get_episode, update_session_status


logger = logging.getLogger("podline")

_PRESIGNED_UPLOAD_ATTEMPTS = 3
_PRESIGNED_UPLOAD_BACKOFF_BASE_SECONDS = 2.0


class PodbeanError(RuntimeError):
    pass


@dataclass(frozen=True)
class PodbeanDraftResult:
    episode_number: int
    post_id: str
    draft_url: str


class PodbeanClientProtocol(Protocol):
    def get_access_token(self) -> str: ...

    def list_episodes(self, access_token: str, limit: int = 100) -> List[Dict]: ...

    def authorize_upload(
        self, access_token: str, file_path: Path, content_type: str
    ) -> Dict: ...

    def upload_presigned_file(
        self, presigned_url: str, file_path: Path, content_type: str
    ) -> None: ...

    def create_draft_episode(
        self,
        access_token: str,
        *,
        title: str,
        description: str,
        media_key: str,
        transcript_key: Optional[str],
        episode_number: int,
    ) -> Dict: ...


class PodbeanClient:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._client = httpx.Client(timeout=120.0)

    def get_access_token(self) -> str:
        data = {"grant_type": "client_credentials"}
        if self._config.podbean.podcast_id.strip():
            data["podcast_id"] = self._config.podbean.podcast_id.strip()
        response = self._client.post(
            f"{self._config.podbean.base_url.rstrip('/')}/oauth/token",
            data=data,
            auth=(self._config.podbean.client_id, self._config.podbean.client_secret),
        )
        self._raise_for_status(response, "Podbean OAuth token request")
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise PodbeanError("Podbean OAuth response did not include an access_token")
        return str(token)

    def list_episodes(self, access_token: str, limit: int = 100) -> List[Dict]:
        episodes = []
        while True:
            response = self._client.get(
                f"{self._config.podbean.base_url.rstrip('/')}/episodes",
                params={
                    "access_token": access_token,
                    "offset": len(episodes),
                    "limit": limit,
                },
            )
            self._raise_for_status(response, "Podbean episode listing")
            payload = response.json()
            page = list(payload.get("episodes") or [])
            episodes.extend(page)
            if not payload.get("has_more"):
                return episodes
            if not page:
                raise PodbeanError(
                    "Podbean episode listing returned an empty page with has_more"
                )

    def authorize_upload(
        self, access_token: str, file_path: Path, content_type: str
    ) -> Dict:
        response = self._client.get(
            f"{self._config.podbean.base_url.rstrip('/')}/files/uploadAuthorize",
            params={
                "access_token": access_token,
                "filename": file_path.name,
                "filesize": file_path.stat().st_size,
                "content_type": content_type,
            },
        )
        self._raise_for_status(response, "Podbean upload authorization")
        payload = response.json()
        if not payload.get("presigned_url") or not payload.get("file_key"):
            raise PodbeanError(
                "Podbean upload authorization response was missing required fields"
            )
        return payload

    def upload_presigned_file(
        self, presigned_url: str, file_path: Path, content_type: str
    ) -> None:
        context = f"Podbean presigned upload for {file_path.name}"
        for attempt in range(1, _PRESIGNED_UPLOAD_ATTEMPTS + 1):
            try:
                with file_path.open("rb") as handle:
                    response = self._client.put(
                        presigned_url,
                        content=handle,
                        headers={"Content-Type": content_type},
                    )
            except httpx.TransportError as exc:
                if attempt == _PRESIGNED_UPLOAD_ATTEMPTS:
                    raise PodbeanError(
                        f"{context} failed after {_PRESIGNED_UPLOAD_ATTEMPTS} "
                        f"attempts: {exc}"
                    ) from exc
                delay = _PRESIGNED_UPLOAD_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
                logger.warning(
                    "%s failed transiently on attempt %d/%d: %s; retrying in %.1fs",
                    context,
                    attempt,
                    _PRESIGNED_UPLOAD_ATTEMPTS,
                    exc,
                    delay,
                )
                sleep(delay)
                continue

            self._raise_for_status(response, context)
            return

    def create_draft_episode(
        self,
        access_token: str,
        *,
        title: str,
        description: str,
        media_key: str,
        transcript_key: Optional[str],
        episode_number: int,
    ) -> Dict:
        data = {
            "access_token": access_token,
            "title": title,
            "content": description,
            "status": "draft",
            "type": "public",
            "media_key": media_key,
            "episode_number": str(episode_number),
        }
        if transcript_key:
            data["transcripts_key"] = transcript_key
        response = self._client.post(
            f"{self._config.podbean.base_url.rstrip('/')}/episodes",
            data=data,
        )
        self._raise_for_status(response, "Podbean draft creation")
        return response.json()

    def _raise_for_status(self, response: httpx.Response, context: str) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            raise PodbeanError(
                f"{context} failed with status {exc.response.status_code}: {body}"
            ) from exc


def create_podbean_draft(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session: SessionRecord,
    client: Optional[PodbeanClientProtocol] = None,
) -> EpisodeRecord:
    validate_podbean_config(config)
    if session.status != SessionStatus.READY_FOR_DRAFT.value:
        raise PodbeanError(
            f"Session {session.session_uid} must be ready_for_draft before creating a Podbean draft"
        )

    episode = get_episode(db_file, session.session_uid)
    if episode is None or not episode.title or not episode.description:
        raise PodbeanError(
            f"Session {session.session_uid} does not have saved episode metadata. Run pod episode-meta first."
        )

    try:
        final_master = materialize_artifact(
            config=config,
            runtime_paths=runtime_paths,
            db_file=db_file,
            session_uid=session.session_uid,
            kind=ArtifactKind.FINAL_MASTER.value,
        )
        transcript = materialize_artifact(
            config=config,
            runtime_paths=runtime_paths,
            db_file=db_file,
            session_uid=session.session_uid,
            kind=ArtifactKind.TRANSCRIPT.value,
        )
    except ArtifactError as exc:
        raise PodbeanError(str(exc)) from exc

    client = client or PodbeanClient(config)
    access_token = client.get_access_token()
    next_episode_number = _next_episode_number(client.list_episodes(access_token))
    with connect(db_file) as connection:
        reserved = connection.execute(
            "SELECT session_uid FROM episodes WHERE episode_number = ? AND session_uid != ?",
            (next_episode_number, session.session_uid),
        ).fetchone()
    if reserved:
        raise PodbeanError(
            f"Episode {next_episode_number} is already assigned to {reserved['session_uid']}. "
            "Resolve that draft before creating another episode."
        )
    draft_title = _draft_title(episode.title, next_episode_number)
    media_auth = client.authorize_upload(
        access_token, final_master, _content_type_for(final_master)
    )
    client.upload_presigned_file(
        media_auth["presigned_url"], final_master, _content_type_for(final_master)
    )

    transcript_auth = client.authorize_upload(
        access_token, transcript, _content_type_for(transcript)
    )
    client.upload_presigned_file(
        transcript_auth["presigned_url"], transcript, _content_type_for(transcript)
    )

    payload = client.create_draft_episode(
        access_token,
        title=draft_title,
        description=episode.description,
        media_key=media_auth["file_key"],
        transcript_key=transcript_auth["file_key"],
        episode_number=next_episode_number,
    )
    episode_node = payload.get("episode") or payload
    draft_url = str(
        episode_node.get("url")
        or episode_node.get("link")
        or episode_node.get("share_url")
        or ""
    )
    post_id = str(
        episode_node.get("id")
        or episode_node.get("episode_id")
        or episode_node.get("post_id")
        or ""
    )
    if not post_id:
        raise PodbeanError("Podbean draft response did not include an episode id")

    updated_episode = _persist_podbean_draft(
        db_file,
        session_uid=session.session_uid,
        episode_number=next_episode_number,
        title=draft_title,
        podbean_post_id=post_id,
        draft_url=draft_url,
    )
    update_session_status(
        db_file,
        session_uid=session.session_uid,
        from_state=session.status,
        to_state=SessionStatus.DRAFT_CREATED.value,
        timestamp=datetime.now(timezone.utc).isoformat(),
        detail="created Podbean draft episode",
        failure_reason=None,
        failed_at_stage=None,
    )
    return updated_episode


def validate_podbean_config(config: AppConfig) -> None:
    if not config.podbean.enabled:
        raise PodbeanError("Podbean is disabled in config.yaml")
    if not config.podbean.client_id.strip() or not config.podbean.client_secret.strip():
        raise PodbeanError("Podbean client_id/client_secret are missing in config.yaml")
    if not config.podbean.base_url.strip():
        raise PodbeanError("Podbean base_url is missing in config.yaml")


# Match the show's existing "38. Title" prefix, not numbers within a title.
_NUMBERED_TITLE = re.compile(r"^\s*(\d+)\.\s+")


def _next_episode_number(episodes: List[Dict]) -> int:
    published = [episode for episode in episodes if episode.get("status") == "publish"]
    numbers = []
    for episode in published:
        try:
            number = int(episode.get("episode_number") or 0)
        except (TypeError, ValueError):
            number = 0
        if number <= 0:
            match = _NUMBERED_TITLE.match(str(episode.get("title") or ""))
            number = int(match.group(1)) if match else 0
        if number > 0:
            numbers.append(number)
    if published and not numbers:
        raise PodbeanError(
            "Cannot determine a published episode number from Podbean history"
        )
    return max(numbers) + 1 if numbers else 1


def _draft_title(title: str, episode_number: int) -> str:
    title = _NUMBERED_TITLE.sub("", title.strip())
    # Legacy all-caps metadata needs a fallback; retain already edited casing.
    if title.isupper():
        title = title.capitalize()
        title = re.sub(r"\bi\b", "I", title)
    return f"{episode_number}. {title}"


def _content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".m4a":
        return "audio/mp4"
    if suffix == ".vtt":
        return "text/vtt"
    return "application/octet-stream"


def _persist_podbean_draft(
    db_file: str,
    *,
    session_uid: str,
    episode_number: int,
    podbean_post_id: str,
    draft_url: str,
    title: str,
) -> EpisodeRecord:
    with connect(db_file) as connection:
        connection.execute(
            """
            UPDATE episodes
            SET episode_number = ?, podbean_post_id = ?, draft_url = ?, title = ?
            WHERE session_uid = ?
            """,
            (episode_number, podbean_post_id, draft_url, title, session_uid),
        )
        connection.commit()

    episode = get_episode(db_file, session_uid)
    if episode is None:
        raise PodbeanError(f"Failed to persist Podbean draft for {session_uid}")
    return episode
