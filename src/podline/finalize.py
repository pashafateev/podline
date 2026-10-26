from datetime import datetime, timezone
from pathlib import Path

from podline.artifacts import ArtifactError, content_type_for, store_artifact_file
from podline.config import AppConfig
from podline.models import ArtifactKind, RuntimePaths, SessionRecord, SessionStatus
from podline.repository import (
    get_episode,
    get_media_artifact,
    get_session,
    list_recording_parts,
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
    parts = list_recording_parts(db_file, session.session_uid)
    if len(parts) > 1:
        for part in parts:
            if not part.ingest_complete:
                raise FinalizeError(
                    f"Part {part.part_number} has incomplete source ingest; "
                    "finish ct-ingest before finalizing the assembled master"
                )
            if part.status != "complete":
                raise FinalizeError(
                    f"Part {part.part_number} is {part.status}, not complete; "
                    "finish this part's processing before finalizing"
                )
            for kind in (
                ArtifactKind.PRE_EDIT_MASTER.value, ArtifactKind.TRANSCRIPT.value,
            ):
                artifact = get_media_artifact(
                    db_file, session.session_uid, kind=kind, part_id=part.part_id,
                )
                if artifact is None:
                    raise FinalizeError(
                        f"Part {part.part_number} has no {kind} artifact; "
                        "finish this part's processing before finalizing"
                    )
                if (
                    not part.auphonic_production_id
                    or artifact.source_id != part.auphonic_production_id
                ):
                    raise FinalizeError(
                        f"Part {part.part_number} {kind} does not belong to its "
                        "current Auphonic production; run auphonic-sync for this part"
                    )
    if session.status != SessionStatus.WAITING_FOR_FINALIZE.value:
        raise FinalizeError(
            f"Session {session.session_uid} must be waiting_for_finalize before finalize"
        )
    if len(parts) <= 1:
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
            part_id=None,
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


def require_final_transcript_for_multipart(db_file: str, session_uid: str) -> None:
    """Fail before consumer side effects when only part transcripts exist.

    Final refresh already records its Auphonic production as source provenance.
    That episode-owned record, not the latest part or its compatibility alias,
    is the factual transcript for metadata and draft creation.
    """
    parts = list_recording_parts(db_file, session_uid)
    if len(parts) <= 1:
        return
    transcript = get_media_artifact(
        db_file, session_uid, kind=ArtifactKind.TRANSCRIPT.value, part_id=None,
    )
    part_transcripts = [
        get_media_artifact(
            db_file, session_uid, kind=ArtifactKind.TRANSCRIPT.value,
            part_id=part.part_id,
        )
        for part in parts
    ]
    part_production_ids = {part.auphonic_production_id for part in parts}
    part_production_ids.update(
        item.source_id for item in part_transcripts if item is not None
    )
    if (
        transcript is None
        or transcript.source_provider != "auphonic"
        or not transcript.source_id
        or transcript.source_id in part_production_ids
        or any(
            item is not None and item.s3_uri == transcript.s3_uri
            for item in part_transcripts
        )
    ):
        raise FinalizeError(
            f"Multipart session {session_uid} needs a refreshed final-master "
            f"transcript. Run pod transcript-refresh {session_uid} after "
            "pod finalize; a part transcript is not the assembled episode."
        )


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
