"""S3-native session registration.

`register_candidate_with_storage` is the single ingest write path shared by
Cozytrack ingest and `pod import`: hash and probe each track, upload every
raw track to S3, then commit the session row and its artifact rows in one
transaction. Re-running with identical content is idempotent (same
content-addressed keys, same fingerprint); the original local files are
never modified or removed.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from podline.artifacts import content_type_for, seed_cache
from podline.config import AppConfig
from podline.fingerprint import build_fingerprint, build_session_uid
from podline.models import (
    ArtifactKind,
    RuntimePaths,
    ScanCandidate,
    SessionRecord,
    TrackFile,
    UnsupportedSession,
)
from podline.repository import (
    backfill_artifact_source_ids,
    create_session_with_artifacts,
    get_session_by_fingerprint,
    set_cozytrack_session_id,
)
from podline.scanner import _parse_session_dir
from podline.storage import MediaStorage, StorageError, sha256_of_file

MIX_ROLE = "mix"

DurationProbe = Callable[[Path], float]


class IngestError(RuntimeError):
    pass


def _default_duration_probe(path: Path) -> float:
    from podline.auphonic import probe_audio_duration_from_packets

    return probe_audio_duration_from_packets(path)


def source_ids_by_content(
    candidate: ScanCandidate, track_source_ids: Dict[str, str]
) -> Dict[str, str]:
    """Map a candidate's track content hashes to provider source ids.

    Pairing by content instead of filename keeps the join correct when the
    stored artifacts were named differently than the staged downloads.
    Tracks with byte-identical content cannot be told apart and are skipped
    rather than guessed at.
    """
    by_sha: Dict[str, str] = {}
    duplicates = set()
    for track in candidate.spoken_tracks:
        source_id = track_source_ids.get(track.path.name)
        if not source_id:
            continue
        sha256 = sha256_of_file(track.path)
        if sha256 in by_sha:
            duplicates.add(sha256)
            continue
        by_sha[sha256] = source_id
    for sha256 in duplicates:
        by_sha.pop(sha256, None)
    return by_sha


def register_candidate_with_storage(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    candidate: ScanCandidate,
    source_provider: str,
    cozytrack_session_id: Optional[str] = None,
    track_source_ids: Optional[Dict[str, str]] = None,
    storage: Optional[MediaStorage] = None,
    duration_probe: Optional[DurationProbe] = None,
    logger: Optional[logging.Logger] = None,
) -> Tuple[SessionRecord, bool]:
    """Upload a classified session's tracks to S3 and register it.

    Returns (session, created). When an identical session already exists
    (same content fingerprint) it is returned unchanged with created=False.
    """
    if not config.storage.enabled:
        raise IngestError(
            "storage.enabled is false; S3-native ingest is paused. "
            "Enable storage in config.local.yaml to ingest new sessions."
        )

    fingerprint = build_fingerprint(candidate)
    existing = get_session_by_fingerprint(db_file, fingerprint)
    if existing is not None:
        if cozytrack_session_id and existing.cozytrack_session_id is None:
            # Same content was registered without a Cozytrack association
            # (e.g. via pod import). Backfill it so ct-list stops offering
            # the session and re-ingest short-circuits before downloading.
            # The source ids ride in the same transaction: once the
            # association is visible, retries stop downloading, so this is
            # the only moment the ids can be joined from files on disk.
            set_cozytrack_session_id(
                db_file,
                session_uid=existing.session_uid,
                cozytrack_session_id=cozytrack_session_id,
                status=existing.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
                source_ids_by_sha256=source_ids_by_content(
                    candidate, track_source_ids or {}
                ),
            )
            _log(
                logger,
                "Associated Cozytrack session %s with existing session %s",
                cozytrack_session_id,
                existing.session_uid,
            )
            refreshed = get_session_by_fingerprint(db_file, fingerprint)
            return (refreshed or existing), False
        if (
            cozytrack_session_id
            and existing.cozytrack_session_id is not None
            and existing.cozytrack_session_id != cozytrack_session_id
        ):
            raise IngestError(
                f"Session content already registered as {existing.session_uid} "
                f"from Cozytrack session {existing.cozytrack_session_id}; "
                f"refusing to also associate it with {cozytrack_session_id}"
            )
        if cozytrack_session_id and track_source_ids:
            # Re-ingest of an already-associated session: heal source ids a
            # previous interrupted or pre-migration run failed to persist.
            healed = backfill_artifact_source_ids(
                db_file,
                session_uid=existing.session_uid,
                source_ids_by_sha256=source_ids_by_content(
                    candidate, track_source_ids
                ),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            if healed:
                _log(
                    logger,
                    "Backfilled Cozytrack source ids for %s raw tracks on %s",
                    healed,
                    existing.session_uid,
                )
        _log(
            logger,
            "Session content already registered as %s; skipping",
            existing.session_uid,
        )
        return existing, False

    session_uid = build_session_uid(fingerprint)
    storage = storage or MediaStorage(config.storage)
    probe = duration_probe or _default_duration_probe
    track_source_ids = track_source_ids or {}

    uploads: List[Tuple[TrackFile, str, dict]] = []
    tracks: List[Tuple[TrackFile, str, bool]] = [
        (track, track.role or track.track_name.lower(), True)
        for track in candidate.spoken_tracks
    ]
    if candidate.mix_track is not None:
        tracks.append((candidate.mix_track, MIX_ROLE, False))

    for track, role, needs_duration in tracks:
        sha256 = sha256_of_file(track.path)
        duration = None
        if needs_duration:
            try:
                duration = float(probe(track.path))
            except Exception as exc:
                raise IngestError(
                    f"Could not compute duration for {track.path}: {exc}"
                ) from exc
        key = storage.key_for(
            session_uid=session_uid,
            kind=ArtifactKind.RAW_TRACK.value,
            sha256=sha256,
            filename=track.path.name,
        )
        content_type = content_type_for(track.path)
        _log(
            logger,
            "Uploading %s role=%s duration=%s to %s",
            track.path.name,
            role,
            duration,
            key,
        )
        try:
            s3_uri = storage.upload_file(
                local_path=track.path,
                key=key,
                content_type=content_type,
                sha256=sha256,
            )
        except StorageError as exc:
            raise IngestError(str(exc)) from exc
        uploads.append(
            (
                track,
                sha256,
                {
                    "kind": ArtifactKind.RAW_TRACK.value,
                    "role": role,
                    "filename": track.path.name,
                    "content_type": content_type,
                    "byte_size": track.file_size,
                    "sha256": sha256,
                    "duration_seconds": duration,
                    "s3_uri": s3_uri,
                    "source_provider": source_provider,
                    "source_id": track_source_ids.get(track.path.name),
                },
            )
        )

    record = create_session_with_artifacts(
        db_file,
        session_uid=session_uid,
        fingerprint=fingerprint,
        candidate=candidate,
        timestamp=datetime.now(timezone.utc).isoformat(),
        artifacts=[spec for _track, _sha, spec in uploads],
        cozytrack_session_id=cozytrack_session_id,
    )
    for track, sha256, _spec in uploads:
        seed_cache(runtime_paths, source_path=track.path, sha256=sha256)
    _log(
        logger,
        "Registered session %s with %s artifacts from %s",
        session_uid,
        len(uploads),
        source_provider,
    )
    return record, True


def import_local_folder(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    folder: Path,
    storage: Optional[MediaStorage] = None,
    duration_probe: Optional[DurationProbe] = None,
    logger: Optional[logging.Logger] = None,
) -> Tuple[SessionRecord, bool]:
    """Register a local recording folder (e.g. Riverside) into S3 storage."""
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise IngestError(f"Import folder does not exist: {folder}")

    parsed = _parse_session_dir(folder, logger=logger)
    if isinstance(parsed, UnsupportedSession):
        raise IngestError(f"Unsupported session at {folder}: {parsed.reason}")
    if not isinstance(parsed, ScanCandidate):
        raise IngestError(f"No recognizable tracks found in {folder}")

    return register_candidate_with_storage(
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        candidate=parsed,
        source_provider="local_import",
        storage=storage,
        duration_probe=duration_probe,
        logger=logger,
    )


def _log(logger: Optional[logging.Logger], message: str, *args) -> None:
    if logger is not None:
        logger.info(message, *args)
