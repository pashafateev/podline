from datetime import datetime, timezone
from pathlib import Path
from sexdrunks_automation.models import SessionRecord, SessionStatus
from sexdrunks_automation.repository import (
    get_episode,
    get_session,
    update_session_status,
)


class FinalizeError(RuntimeError):
    pass


def finalize_session(
    *,
    db_file: str,
    session: SessionRecord,
    master_path: Path,
) -> SessionRecord:
    if session.status != SessionStatus.WAITING_FOR_FINALIZE.value:
        raise FinalizeError(
            f"Session {session.session_uid} must be waiting_for_finalize before finalize"
        )
    if not session.transcript_path:
        raise FinalizeError(f"Session {session.session_uid} has no transcript path")
    if not Path(session.transcript_path).exists():
        raise FinalizeError(f"Transcript file not found: {session.transcript_path}")
    if not session.pre_edit_master_path:
        raise FinalizeError(
            f"Session {session.session_uid} has no pre-edit master path"
        )
    if not Path(session.pre_edit_master_path).exists():
        raise FinalizeError(
            f"Pre-edit master file not found: {session.pre_edit_master_path}"
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

    timestamp = datetime.now(timezone.utc).isoformat()
    update_session_status(
        db_file,
        session_uid=session.session_uid,
        from_state=session.status,
        to_state=SessionStatus.READY_FOR_DRAFT.value,
        timestamp=timestamp,
        detail="finalized session with edited master; episode metadata pending AI generation",
        final_master_path=str(resolved_master_path),
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
