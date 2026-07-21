import logging
import re
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from podline.config import AppConfig
from podline.cozytrack.client import (
    CozytrackClient,
    CozytrackClientProtocol,
    CozytrackError,
    CozytrackSession,
    CozytrackTrack,
    host_channel_number,
    is_host_participant,
)
from podline.ingest import DurationProbe, register_candidate_with_storage
from podline.models import RuntimePaths, ScanCandidate, SessionRecord, UnsupportedSession
from podline.repository import (
    get_session_by_cozytrack_id,
    list_media_artifacts,
    reconcile_artifact_roles,
)
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
        client = client or CozytrackClient(config)
        try:
            cozytrack_session = client.get_session(cozytrack_session_id)
        except CozytrackError as exc:
            raise CozytrackIngestError(
                f"Could not load Cozytrack session {cozytrack_session_id} to "
                f"verify roles for {existing.session_uid}: {exc}"
            ) from exc
        _reconcile_existing_session_roles(
            db_file=db_file,
            existing=existing,
            cozytrack_session=cozytrack_session,
            logger=logger,
        )
        return existing

    client = client or CozytrackClient(config)
    session = client.get_session(cozytrack_session_id)
    if not session.tracks:
        raise CozytrackIngestError(
            f"Cozytrack session {cozytrack_session_id} has no tracks"
        )
    roles_by_track_id = _derive_participant_roles(session.tracks)

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

    if roles_by_track_id is None:
        sorted_tracks = sorted(
            session.tracks,
            key=lambda track: (track.created_at or "", track.id),
        )
    else:
        # Download in recorder order (host channels, then guests) so the
        # TrN filenames line up with the derived roles. Cozytrack creates
        # track rows in recorder start order, which can interleave a guest
        # between host channels — that order must not drive naming.
        sorted_tracks = sorted(
            session.tracks,
            key=lambda track: _derived_role_order(roles_by_track_id[track.id]),
        )
    track_source_ids: Dict[str, str] = {}
    roles_by_filename: Dict[str, str] = {}
    try:
        for index, track in enumerate(sorted_tracks, start=1):
            destination = folder / f"Tr{index}.webm"
            url = client.get_track_download_url(track.id)
            if roles_by_track_id is not None:
                roles_by_filename[destination.name] = roles_by_track_id[track.id]
            _log(
                logger,
                "Cozytrack download track=%s index=%s destination=%s role=%s",
                track.id,
                index,
                destination,
                roles_by_filename.get(destination.name, "legacy-order"),
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
        if roles_by_filename:
            parsed = _candidate_with_participant_roles(parsed, roles_by_filename)

        record, created = register_candidate_with_storage(
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
        if not created:
            # Same content already registered (e.g. pod import backfilled
            # with this Cozytrack id); its stored roles may predate
            # participant-derived naming.
            _reconcile_existing_session_roles(
                db_file=db_file,
                existing=record,
                cozytrack_session=session,
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


def _reconcile_existing_session_roles(
    *,
    db_file: str,
    existing: SessionRecord,
    cozytrack_session: CozytrackSession,
    logger: Optional[logging.Logger],
) -> None:
    """Re-align persisted raw-track roles with Cozytrack participant identity.

    Sessions ingested before roles derived from participant ids (or before
    Cozytrack exposed them) can hold download-order roles that mislabel a
    guest interleaved between host channels. Re-running ingest is the
    migration path: when every spoken artifact maps to a Cozytrack track by
    source id, mismatched roles are rewritten in place; otherwise the
    session is left untouched with a warning. Filenames and content are
    never modified.
    """
    roles_by_track_id = _derive_participant_roles(cozytrack_session.tracks)
    if roles_by_track_id is None:
        return
    artifacts = [
        artifact
        for artifact in list_media_artifacts(
            db_file, existing.session_uid, kind="raw_track"
        )
        if artifact.role != "mix"
    ]
    if not artifacts:
        return

    role_updates: Dict[str, str] = {}
    for artifact in artifacts:
        source_id = (artifact.source_id or "").strip()
        derived_role = roles_by_track_id.get(source_id) if source_id else None
        if derived_role is None:
            _log(
                logger,
                "Cannot verify roles for %s: artifact %s (role=%s) has no "
                "matching Cozytrack track; leaving roles unchanged",
                existing.session_uid,
                artifact.filename,
                artifact.role,
            )
            return
        if derived_role != artifact.role:
            role_updates[artifact.role] = derived_role
    if not role_updates:
        return

    changes = ", ".join(
        f"{old}->{new}" for old, new in sorted(role_updates.items())
    )
    reconcile_artifact_roles(
        db_file,
        session_uid=existing.session_uid,
        status=existing.status,
        role_updates=role_updates,
        timestamp=datetime.now(timezone.utc).isoformat(),
        detail=f"reconciled raw track roles from Cozytrack participant ids: {changes}",
    )
    _log(
        logger,
        "Reconciled roles for %s from Cozytrack participant ids: %s",
        existing.session_uid,
        changes,
    )
    if existing.auphonic_production_id:
        _log(
            logger,
            "Session %s has Auphonic production %s prepared under the old "
            "roles; run 'pod auphonic-reset %s' and re-run auphonic-prepare",
            existing.session_uid,
            existing.auphonic_production_id,
            existing.session_uid,
        )


def _derive_participant_roles(
    tracks: List[CozytrackTrack],
) -> Optional[Dict[str, str]]:
    """Map Cozytrack track ids to pipeline roles via stable participant ids.

    Host-local channels become hostN by channel number; remote guests become
    guestN in recorder start order. Returns None when no track carries a
    participant id (legacy sessions keep download-order naming). A partially
    identified session is an error rather than a silent fallback: guessing
    roles from download order mislabels tracks whenever Cozytrack interleaves
    a guest between host channels.
    """
    labeled = [(track, (track.participant_id or "").strip()) for track in tracks]
    with_ids = [pair for pair in labeled if pair[1]]
    if not with_ids:
        return None
    if len(with_ids) != len(labeled):
        missing = ", ".join(track.id for track, participant_id in labeled if not participant_id)
        raise CozytrackIngestError(
            f"Cozytrack tracks are missing participantId: {missing}; "
            "cannot derive roles for a partially identified session"
        )

    guests = sorted(
        (track for track, participant_id in labeled if not is_host_participant(participant_id)),
        key=lambda track: (track.created_at or "", track.id),
    )
    guest_numbers = {track.id: index for index, track in enumerate(guests, start=1)}

    roles: Dict[str, str] = {}
    assigned = set()
    for track, participant_id in labeled:
        if is_host_participant(participant_id):
            channel = host_channel_number(participant_id)
            if channel is None:
                raise CozytrackIngestError(
                    f"Cozytrack track {track.id} has host participant id "
                    f"{participant_id!r} without a channel number"
                )
            role = f"host{channel}"
        else:
            role = f"guest{guest_numbers[track.id]}"
        if role in assigned:
            raise CozytrackIngestError(
                f"Cozytrack participant ids produce duplicate role {role!r} "
                f"(track {track.id})"
            )
        assigned.add(role)
        roles[track.id] = role
    return roles


def _derived_role_order(role: str) -> Tuple[int, int]:
    """Recorder order for participant-derived roles: host channels, then guests."""
    if role.startswith("host"):
        return (0, int(role[len("host") :]))
    return (1, int(role[len("guest") :]))


def _candidate_with_participant_roles(
    candidate: ScanCandidate, roles_by_filename: Dict[str, str]
) -> ScanCandidate:
    """Stamp participant-derived roles onto the classified tracks.

    Classification may drop silent tracks, so only survivors are relabeled;
    a surviving track without a derived role is a wiring bug.
    """
    relabeled = []
    for track in candidate.spoken_tracks:
        role = roles_by_filename.get(track.path.name)
        if role is None:
            raise CozytrackIngestError(
                f"No participant-derived role for classified track {track.path.name}"
            )
        relabeled.append(replace(track, role=role))
    return replace(candidate, spoken_tracks=relabeled)


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
