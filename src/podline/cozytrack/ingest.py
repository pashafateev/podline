import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from podline.config import AppConfig
from podline.cozytrack.client import (
    CozytrackClient,
    CozytrackClientProtocol,
    CozytrackSession,
)
from podline.ingest import DurationProbe, register_candidate_with_storage
from podline.models import RuntimePaths, ScanCandidate, SessionRecord, UnsupportedSession
from podline.repository import get_session_by_cozytrack_id
from podline.scanner import _parse_session_dir
from podline.storage import MediaStorage


class CozytrackIngestError(RuntimeError):
    pass


def list_ready_sessions_for_ingest(
    *,
    config: AppConfig,
    db_file: str,
    client: Optional[CozytrackClientProtocol] = None,
) -> List[CozytrackSession]:
    client = client or CozytrackClient(config)
    sessions = client.list_ready_sessions()
    already_ingested = set(_existing_cozytrack_ids(db_file))
    return [session for session in sessions if session.id not in already_ingested]


def _existing_cozytrack_ids(db_file: str) -> List[str]:
    from podline.repository import list_cozytrack_session_ids

    return list_cozytrack_session_ids(db_file)


def ingest_session(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    cozytrack_session_id: str,
    name_override: Optional[str] = None,
    client: Optional[CozytrackClientProtocol] = None,
    storage: Optional[MediaStorage] = None,
    duration_probe: Optional[DurationProbe] = None,
    logger: Optional[logging.Logger] = None,
) -> SessionRecord:
    """Download a Cozytrack session into disposable staging and register it in S3.

    Tracks land in a staging folder only long enough to classify, hash, and
    upload; on success the staging folder is removed (the cache keeps
    hardlinked copies). Re-running an already-ingested session is a no-op.
    """
    existing = get_session_by_cozytrack_id(db_file, cozytrack_session_id)
    if existing is not None:
        _log(
            logger,
            "Cozytrack session %s already ingested as %s",
            cozytrack_session_id,
            existing.session_uid,
        )
        return existing

    client = client or CozytrackClient(config)
    session = client.get_session(cozytrack_session_id)
    if not session.tracks:
        raise CozytrackIngestError(
            f"Cozytrack session {cozytrack_session_id} has no tracks"
        )

    folder = _build_staging_folder(
        runtime_paths=runtime_paths,
        session=session,
        name_override=name_override,
    )
    folder.mkdir(parents=True, exist_ok=True)
    _log(
        logger,
        "Cozytrack ingest session=%s staging=%s tracks=%s",
        cozytrack_session_id,
        folder,
        len(session.tracks),
    )

    sorted_tracks = sorted(
        session.tracks,
        key=lambda track: (track.created_at or "", track.id),
    )
    track_source_ids: Dict[str, str] = {}
    try:
        for index, track in enumerate(sorted_tracks, start=1):
            destination = folder / f"Tr{index}.webm"
            url = client.get_track_download_url(track.id)
            _log(
                logger,
                "Cozytrack download track=%s index=%s destination=%s",
                track.id,
                index,
                destination,
            )
            client.download_to_file(url, destination)
            track_source_ids[destination.name] = track.id

        parsed = _parse_session_dir(folder, logger=logger)
        if isinstance(parsed, UnsupportedSession):
            raise CozytrackIngestError(
                f"Cozytrack ingest produced an unsupported session: {parsed.reason}"
            )
        if not isinstance(parsed, ScanCandidate):
            raise CozytrackIngestError(
                f"Cozytrack ingest could not classify the downloaded session at {folder}"
            )

        record, _created = register_candidate_with_storage(
            config=config,
            runtime_paths=runtime_paths,
            db_file=db_file,
            candidate=parsed,
            source_provider="cozytrack",
            cozytrack_session_id=cozytrack_session_id,
            track_source_ids=track_source_ids,
            storage=storage,
            duration_probe=duration_probe,
            logger=logger,
        )
    except CozytrackIngestError:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise CozytrackIngestError(
            f"Cozytrack ingest failed for session {cozytrack_session_id}: {exc}"
        ) from exc

    shutil.rmtree(folder, ignore_errors=True)
    _log(
        logger,
        "Cozytrack ingest complete session=%s session_uid=%s",
        cozytrack_session_id,
        record.session_uid,
    )
    return record


def _build_staging_folder(
    *,
    runtime_paths: RuntimePaths,
    session: CozytrackSession,
    name_override: Optional[str],
) -> Path:
    date_part = _format_date(session.finalized_at)
    slug = slugify(name_override or session.name or session.id)
    folder_name = f"{date_part}_{slug}" if date_part else slug
    return Path(runtime_paths.staging_dir) / "ct-ingest" / folder_name


def _format_date(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed.strftime("%Y-%m-%d")
    except ValueError:
        return value[:10]


_SLUG_INVALID = re.compile(r"[^a-zA-Z0-9]+")


def slugify(value: str) -> str:
    cleaned = _SLUG_INVALID.sub("-", value).strip("-").lower()
    return cleaned or "session"


def _log(logger: Optional[logging.Logger], message: str, *args) -> None:
    if logger is not None:
        logger.info(message, *args)


__all__ = [
    "CozytrackIngestError",
    "ingest_session",
    "list_ready_sessions_for_ingest",
    "slugify",
]
