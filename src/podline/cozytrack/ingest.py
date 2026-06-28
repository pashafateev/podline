import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from podline.config import AppConfig
from podline.cozytrack.client import (
    CozytrackClient,
    CozytrackClientProtocol,
    CozytrackSession,
)
from podline.fingerprint import build_fingerprint, build_session_uid
from podline.models import SessionRecord
from podline.repository import (
    create_session,
    get_session_by_cozytrack_id,
)
from podline.scanner import _parse_session_dir
from podline.models import ScanCandidate, UnsupportedSession


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
    db_file: str,
    cozytrack_session_id: str,
    name_override: Optional[str] = None,
    client: Optional[CozytrackClientProtocol] = None,
    logger: Optional[logging.Logger] = None,
) -> SessionRecord:
    existing = get_session_by_cozytrack_id(db_file, cozytrack_session_id)
    if existing is not None:
        _log(
            logger,
            "Cozytrack session %s already ingested at %s",
            cozytrack_session_id,
            existing.source_path,
        )
        return existing

    client = client or CozytrackClient(config)
    session = client.get_session(cozytrack_session_id)
    if not session.tracks:
        raise CozytrackIngestError(
            f"Cozytrack session {cozytrack_session_id} has no tracks"
        )

    folder = _build_session_folder(
        config=config,
        session=session,
        name_override=name_override,
    )
    folder.mkdir(parents=True, exist_ok=True)
    _log(
        logger,
        "Cozytrack ingest session=%s folder=%s tracks=%s",
        cozytrack_session_id,
        folder,
        len(session.tracks),
    )

    sorted_tracks = sorted(
        session.tracks,
        key=lambda track: (track.created_at or "", track.id),
    )
    written_paths: List[Path] = []
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
            written_paths.append(destination)
            client.download_to_file(url, destination)
    except Exception as exc:
        for path in written_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise CozytrackIngestError(
            f"Cozytrack ingest failed downloading tracks for session {cozytrack_session_id}: {exc}"
        ) from exc

    parsed = _parse_session_dir(folder, logger=logger)
    if isinstance(parsed, UnsupportedSession):
        raise CozytrackIngestError(
            f"Cozytrack ingest produced an unsupported session: {parsed.reason}"
        )
    if not isinstance(parsed, ScanCandidate):
        raise CozytrackIngestError(
            f"Cozytrack ingest could not classify the downloaded session at {folder}"
        )

    fingerprint = build_fingerprint(parsed)
    session_uid = build_session_uid(fingerprint)
    timestamp = datetime.now(timezone.utc).isoformat()
    record = create_session(
        db_file,
        session_uid=session_uid,
        fingerprint=fingerprint,
        candidate=parsed,
        timestamp=timestamp,
        cozytrack_session_id=cozytrack_session_id,
    )
    _log(
        logger,
        "Cozytrack ingest complete session=%s session_uid=%s",
        cozytrack_session_id,
        session_uid,
    )
    return record


def _build_session_folder(
    *,
    config: AppConfig,
    session: CozytrackSession,
    name_override: Optional[str],
) -> Path:
    raw_root = Path(config.paths.raw_root)
    download_subdir = config.cozytrack.download_subdir.strip()
    if download_subdir:
        subdir_path = Path(download_subdir)
        assert not subdir_path.is_absolute(), (
            f"cozytrack.download_subdir must be relative: {download_subdir!r}"
        )
        assert ".." not in subdir_path.parts, (
            f"cozytrack.download_subdir must not contain '..': {download_subdir!r}"
        )
        base_dir = raw_root / subdir_path
    else:
        base_dir = raw_root

    date_part = _format_date(session.finalized_at)
    slug = slugify(name_override or session.name or session.id)
    folder_name = f"{date_part}_{slug}" if date_part else slug
    return base_dir / folder_name


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
