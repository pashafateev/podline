from datetime import datetime, timezone
from pathlib import Path

from podline.artifacts import ArtifactError, content_type_for, store_artifact_file
from podline.config import AppConfig
from podline.models import ArtifactKind, RuntimePaths, SessionRecord, SessionStatus
from podline.repository import (
    get_episode,
    get_media_artifact,
    get_session,
    update_session_status,
)


class FinalizeError(RuntimeError):
    pass


def finalize_session(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session: SessionRecord,
    master_path: Path,
) -> SessionRecord:
    if session.status != SessionStatus.WAITING_FOR_FINALIZE.value:
        raise FinalizeError(
            f"Session {session.session_uid} must be waiting_for_finalize before finalize"
        )
    for kind in (ArtifactKind.TRANSCRIPT.value, ArtifactKind.PRE_EDIT_MASTER.value):
        if get_media_artifact(db_file, session.session_uid, kind=kind) is None:
            raise FinalizeError(
                f"Session {session.session_uid} has no {kind} artifact"
            )

    resolved_master_path = _resolve_master_path(master_path)
    if not resolved_master_path.exists():
        raise FinalizeError(f"Final master file not found: {resolved_master_path}")
    if resolved_master_path.suffix.lower() not in {
        ".mp3",
        ".wav",
        ".m4a",
        ".aif",
        ".aiff",
    }:
        raise FinalizeError(
            f"Unsupported final master format: {resolved_master_path.suffix}"
        )

    try:
        artifact = store_artifact_file(
            config=config,
            runtime_paths=runtime_paths,
            db_file=db_file,
            session_uid=session.session_uid,
            kind=ArtifactKind.FINAL_MASTER.value,
            source_path=resolved_master_path,
            content_type=content_type_for(resolved_master_path),
            source_provider="local_edit",
        )
    except ArtifactError as exc:
        raise FinalizeError(str(exc)) from exc

    timestamp = datetime.now(timezone.utc).isoformat()
    update_session_status(
        db_file,
        session_uid=session.session_uid,
        from_state=session.status,
        to_state=SessionStatus.READY_FOR_DRAFT.value,
        timestamp=timestamp,
        detail=f"finalized session; edited master stored at {artifact.s3_uri}",
        failure_reason=None,
        failed_at_stage=None,
    )
    updated = get_session(db_file, session.session_uid)
    if updated is None:
        raise FinalizeError(f"Finalized session disappeared: {session.session_uid}")
    return updated


def get_episode_metadata(db_file: str, session_uid: str):
    return get_episode(db_file, session_uid)


def _resolve_master_path(master_path: Path) -> Path:
    resolved_master_path = master_path.expanduser().resolve()
    if resolved_master_path.exists():
        return resolved_master_path

    raw_value = str(master_path)
    if "\\ " in raw_value:
        unescaped_path = Path(raw_value.replace("\\ ", " ")).expanduser().resolve()
        if unescaped_path.exists():
            return unescaped_path

    return resolved_master_path
