import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from time import sleep
from typing import Dict, List, Optional, Protocol
from urllib.parse import quote

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
from podline.repository import get_episode, get_session
from podline.workflow_lock import WorkflowBusy, session_lock


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


@dataclass(frozen=True)
class PendingDraft:
    episode_number: int
    title: str
    started_at: str


class PodbeanClientProtocol(Protocol):
    def get_access_token(self) -> str: ...

    def list_episodes(self, access_token: str, limit: int = 100) -> List[Dict]: ...

    def get_remote_episode(self, access_token: str, post_id: str) -> Dict: ...

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

    def get_remote_episode(self, access_token: str, post_id: str) -> Dict:
        response = self._client.get(
            f"{self._config.podbean.base_url.rstrip('/')}/episodes/{quote(post_id, safe='')}",
            params={"access_token": access_token},
        )
        self._raise_for_status(response, "Podbean episode lookup")
        return response.json()

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
                    "%s failed transiently on attempt %d/%d: %s; "
                    "retrying in %.1fs",
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
    with _draft_lock(db_file, session.session_uid):
        current = get_session(db_file, session.session_uid)
        if current is None:
            raise PodbeanError(f"Unknown session {session.session_uid}")
        episode = get_episode(db_file, current.session_uid)
        if episode and episode.podbean_post_id:
            # Earlier versions committed the episode ID before changing the state.
            if current.status == SessionStatus.READY_FOR_DRAFT.value:
                with connect(db_file) as connection:
                    _mark_draft_created(connection, current.session_uid)
            return episode
        pending = get_pending_draft(db_file, current.session_uid)
        if pending:
            raise PodbeanError(_recovery_message(pending))
        try:
            return _submit_draft(config, runtime_paths, db_file, current, episode, client)
        except (httpx.HTTPError, ValueError) as exc:
            raise PodbeanError("Podbean preparation failed before draft submission. Retry this step.") from exc


def _submit_draft(
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session: SessionRecord,
    episode: Optional[EpisodeRecord],
    client: Optional[PodbeanClientProtocol],
) -> EpisodeRecord:
    validate_podbean_config(config)
    if session.status != SessionStatus.READY_FOR_DRAFT.value:
        raise PodbeanError(
            f"Session {session.session_uid} must be ready_for_draft before creating a Podbean draft"
        )

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

    started_at = datetime.now(timezone.utc).isoformat()
    with connect(db_file) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current_inputs = connection.execute(
            """SELECT 1 FROM sessions JOIN episodes USING(session_uid)
               WHERE session_uid=? AND status='ready_for_draft'
               AND transcript_refresh_production_id IS NULL
               AND COALESCE(podbean_post_id, '')=''
               AND title=? AND description=?""",
            (session.session_uid, episode.title, episode.description),
        ).fetchone()
        if current_inputs is None:
            raise PodbeanError("Draft inputs changed. Review the current transcript and metadata.")
        # Podbean's listing can omit drafts. Reserve numbers held locally as well.
        highest = connection.execute(
            """SELECT MAX(episode_number) FROM (
                   SELECT episode_number FROM episodes
                   UNION ALL SELECT episode_number FROM pending_podbean_drafts
               )"""
        ).fetchone()[0]
        next_episode_number = max(next_episode_number, (highest or 0) + 1)
        pending = PendingDraft(next_episode_number, episode.title, started_at)
        connection.execute(
            """INSERT INTO pending_podbean_drafts(session_uid, episode_number, title, started_at)
               VALUES (?, ?, ?, ?)""",
            (session.session_uid, pending.episode_number, pending.title, pending.started_at),
        )

    try:
        payload = client.create_draft_episode(
            access_token,
            title=episode.title,
            description=episode.description,
            media_key=media_auth["file_key"],
            transcript_key=transcript_auth["file_key"],
            episode_number=next_episode_number,
        )
        return _record_remote_draft(db_file, session.session_uid, pending, payload)
    except (PodbeanError, httpx.HTTPError, ValueError, sqlite3.Error) as exc:
        raise PodbeanError(f"{exc}. {_recovery_message(pending)}") from exc


def get_pending_draft(db_file: str, session_uid: str) -> Optional[PendingDraft]:
    with connect(db_file) as connection:
        row = connection.execute(
            "SELECT episode_number, title, started_at FROM pending_podbean_drafts WHERE session_uid=?",
            (session_uid,),
        ).fetchone()
    return PendingDraft(**row) if row else None


def _recovery_message(pending: PendingDraft) -> str:
    return (
        f"Check Podbean for draft #{pending.episode_number} ({pending.title}) "
        f"from {pending.started_at}. Run pod podbean-draft for this session with "
        "--existing-id ID to record the matching draft, or --confirm-absent "
        "after verifying the submission did not create an episode."
    )


@contextmanager
def _draft_lock(db_file: str, uid: str):
    try:
        with session_lock(db_file, uid, "podbean"):
            yield
    except WorkflowBusy as exc:
        raise PodbeanError(f"A Podbean request is in progress for {uid}.") from exc


def protect_draft_inputs(error_type):
    """Serialize manual and guided edits with submission and preserve ambiguous drafts."""

    def decorate(operation):
        @wraps(operation)
        def guarded(*, db_file, session, **kwargs):
            try:
                with _draft_lock(db_file, session.session_uid):
                    pending = get_pending_draft(db_file, session.session_uid)
                    if pending:
                        raise PodbeanError(_recovery_message(pending))
                    current = get_session(db_file, session.session_uid)
                    if current is None:
                        raise PodbeanError(f"Unknown session {session.session_uid}")
                    episode = get_episode(db_file, session.session_uid)
                    if episode and episode.podbean_post_id and current.status == "ready_for_draft":
                        raise PodbeanError("Run pod podbean-draft to record the saved draft's status.")
                    return operation(db_file=db_file, session=current, **kwargs)
            except PodbeanError as exc:
                raise error_type(str(exc)) from exc

        return guarded

    return decorate


def recover_podbean_draft(
    *,
    config: AppConfig,
    db_file: str,
    session: SessionRecord,
    existing_id: Optional[str] = None,
    confirm_absent: bool = False,
    client: Optional[PodbeanClientProtocol] = None,
) -> Optional[EpisodeRecord]:
    if bool(existing_id) == confirm_absent:
        raise PodbeanError("Choose either --existing-id ID or --confirm-absent.")
    uid = session.session_uid
    with _draft_lock(db_file, uid):
        pending = get_pending_draft(db_file, uid)
        if pending is None:
            raise PodbeanError("This session has no pending Podbean submission.")
        if confirm_absent:
            with connect(db_file) as connection:
                connection.execute("DELETE FROM pending_podbean_drafts WHERE session_uid=?", (uid,))
            return None
        validate_podbean_config(config)
        client = client or PodbeanClient(config)
        try:
            payload = client.get_remote_episode(client.get_access_token(), existing_id)
        except (httpx.HTTPError, ValueError) as exc:
            raise PodbeanError("Unable to verify the episode with Podbean. The submission remains pending.") from exc
        return _record_remote_draft(db_file, uid, pending, payload, verified_id=existing_id)


def _episode_node(payload: Dict) -> Dict:
    if isinstance(payload, dict):
        node = payload.get("episode", payload)
        if isinstance(node, dict):
            return node
    raise PodbeanError("Invalid Podbean episode response")


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


def _mark_draft_created(connection: sqlite3.Connection, uid: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    changed = connection.execute(
        """UPDATE sessions SET status='draft_created', updated_at=?, failure_reason=NULL,
                  failed_at_stage=NULL WHERE session_uid=? AND status='ready_for_draft'""",
        (now, uid),
    )
    if changed.rowcount != 1:
        raise PodbeanError("Session state changed during Podbean submission.")
    connection.execute(
        """INSERT INTO events(session_uid, from_state, to_state, timestamp, detail)
           VALUES (?, 'ready_for_draft', 'draft_created', ?, 'recorded Podbean draft episode')""",
        (uid, now),
    )


def _record_remote_draft(
    db_file: str,
    uid: str,
    pending: PendingDraft,
    payload: Dict,
    *,
    verified_id: Optional[str] = None,
) -> EpisodeRecord:
    remote = _episode_node(payload)
    post_id = remote.get("id") or remote.get("episode_id") or remote.get("post_id")
    if type(post_id) not in (str, int) or not str(post_id).strip():
        raise PodbeanError("Podbean draft response did not include an episode id")
    expected = {"status": "draft", "title": pending.title, "episode_number": pending.episode_number}
    # Creation may return only the ID. Recovery must positively identify the draft.
    if verified_id is not None:
        expected["id"] = verified_id
    for field, value in expected.items():
        if verified_id is None and field not in remote:
            continue
        if str(remote.get(field)) != str(value):
            raise PodbeanError(f"Podbean {field} does not match the pending submission.")
    url = (
        remote.get("url")
        or remote.get("link")
        or remote.get("share_url")
        or remote.get("permalink_url")
        or ""
    )
    with connect(db_file) as connection:
        updated = connection.execute(
            """UPDATE episodes SET episode_number=?, podbean_post_id=?, draft_url=?
               WHERE session_uid=?""",
            (pending.episode_number, str(post_id), str(url), uid),
        )
        if updated.rowcount != 1:
            raise PodbeanError("Saved episode metadata disappeared during Podbean submission.")
        _mark_draft_created(connection, uid)
        connection.execute("DELETE FROM pending_podbean_drafts WHERE session_uid=?", (uid,))
    episode = get_episode(db_file, uid)
    if episode is None:
        raise PodbeanError(f"Could not read recorded Podbean draft for {uid}")
    return episode
