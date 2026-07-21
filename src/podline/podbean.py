from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Protocol

import httpx

from podline.artifacts import ArtifactError, materialize_artifact
from podline.config import AppConfig
from podline.models import (
    ArtifactKind,
    EpisodeRecord,
    RuntimePaths,
    SessionRecord,
    SessionStatus,
)
from podline.repository import get_episode, update_session_status


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
        response = self._client.get(
            f"{self._config.podbean.base_url.rstrip('/')}/episodes",
            params={"access_token": access_token, "offset": 0, "limit": limit},
        )
        self._raise_for_status(response, "Podbean episode listing")
        payload = response.json()
        return list(payload.get("episodes") or [])

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
        with file_path.open("rb") as handle:
            response = self._client.put(
                presigned_url,
                content=handle,
                headers={"Content-Type": content_type},
            )
        self._raise_for_status(
            response, f"Podbean presigned upload for {file_path.name}"
        )

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
        title=episode.title,
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


def _next_episode_number(episodes: List[Dict]) -> int:
    numbers = []
    for episode in episodes:
        value = episode.get("episode_number")
        if value in (None, ""):
            continue
        try:
            numbers.append(int(value))
        except (TypeError, ValueError):
            continue
    return (max(numbers) + 1) if numbers else 1


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
) -> EpisodeRecord:
    from podline.db import connect

    with connect(db_file) as connection:
        connection.execute(
            """
            UPDATE episodes
            SET episode_number = ?, podbean_post_id = ?, draft_url = ?
            WHERE session_uid = ?
            """,
            (episode_number, podbean_post_id, draft_url, session_uid),
        )
        connection.commit()

    episode = get_episode(db_file, session_uid)
    if episode is None:
        raise PodbeanError(f"Failed to persist Podbean draft for {session_uid}")
    return episode
