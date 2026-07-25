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
from podline.ingest import (
    DurationProbe,
    register_candidate_with_storage,
    source_ids_by_content,
)
from podline.models import RuntimePaths, ScanCandidate, SessionRecord, UnsupportedSession
from podline.repository import (
    backfill_artifact_source_ids,
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
    excluded_track_selectors: Optional[List[str]] = None,
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
    excluded_track_selectors = [
        selector.strip()
        for selector in (excluded_track_selectors or [])
        if selector.strip()
    ]
    existing = get_session_by_cozytrack_id(db_file, cozytrack_session_id)
    if existing is not None:
        if excluded_track_selectors:
            raise CozytrackIngestError(
                f"Cozytrack session {cozytrack_session_id} is already ingested as "
                f"{existing.session_uid}; --exclude only applies to the first ingest"
            )
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
        missing_ids = _spoken_tracks_missing_source_ids(db_file, existing.session_uid)
        if missing_ids:
            # A previous run associated the session but never persisted the
            # track source ids (interrupted migration or pre-migration
            # code). Heal against the session the association names, joined
            # strictly by content hash — never through fingerprint-based
            # registration, which depends on track naming that may have
            # changed across ingest versions and would collide with the
            # unique Cozytrack association on re-registration.
            _log(
                logger,
                "Session %s lacks source ids for %s track(s); re-downloading "
                "to heal by content hash",
                existing.session_uid,
                missing_ids,
            )
            _heal_associated_session(
                runtime_paths=runtime_paths,
                db_file=db_file,
                existing=existing,
                cozytrack_session=cozytrack_session,
                client=client,
                logger=logger,
            )
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
    selected_tracks = _select_tracks(
        session=session,
        excluded_track_selectors=excluded_track_selectors,
    )
    if excluded_track_selectors:
        selected_track_ids = {track.id for track in selected_tracks}
        excluded_track_ids = [
            track.id for track in session.tracks if track.id not in selected_track_ids
        ]
        _log(
            logger,
            "Cozytrack ingest session=%s exclusions=%s excluded_track_ids=%s",
            cozytrack_session_id,
            excluded_track_selectors,
            excluded_track_ids,
        )
    session = replace(session, tracks=selected_tracks)
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
            message = (
                f"Cozytrack ingest produced an unsupported session: {parsed.reason}"
            )
            if parsed.spoken_track_count > 4:
                message = "\n\n".join(
                    [
                        message,
                        "Available Cozytrack tracks:\n"
                        + format_session_track_table(session),
                        _track_exclusion_guidance(cozytrack_session_id),
                    ]
                )
            raise CozytrackIngestError(message)
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
            # Same content already registered (a pod import that register
            # just associated and backfilled source ids for, or a resumed
            # migration). With ids in place the roles can reconcile.
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


def _heal_associated_session(
    *,
    runtime_paths: RuntimePaths,
    db_file: str,
    existing: SessionRecord,
    cozytrack_session: CozytrackSession,
    client: CozytrackClientProtocol,
    logger: Optional[logging.Logger],
) -> None:
    """Re-download an associated session's tracks and heal missing source ids.

    The association already names the session, so the join is content hash
    against its stored artifacts — registration and fingerprints are never
    involved (both depend on track naming, which changed across ingest
    versions). Content that matches nothing leaves its ids missing and is
    reported, not guessed at; the next run simply tries again.
    """
    folder = _build_staging_folder(
        runtime_paths=runtime_paths,
        session=cozytrack_session,
        name_override=None,
    )
    folder.mkdir(parents=True, exist_ok=True)
    try:
        downloads = []
        for index, track in enumerate(cozytrack_session.tracks, start=1):
            destination = folder / f"Tr{index}.webm"
            url = client.get_track_download_url(track.id)
            client.download_to_file(url, destination)
            downloads.append((destination, track.id))
        healed = backfill_artifact_source_ids(
            db_file,
            session_uid=existing.session_uid,
            source_ids_by_sha256=source_ids_by_content(downloads),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if healed:
        _log(
            logger,
            "Backfilled Cozytrack source ids for %s raw tracks on %s",
            healed,
            existing.session_uid,
        )
    still_missing = _spoken_tracks_missing_source_ids(db_file, existing.session_uid)
    if still_missing:
        _log(
            logger,
            "Session %s still lacks source ids for %s track(s): stored audio "
            "does not content-match the Cozytrack downloads; leaving them "
            "unset",
            existing.session_uid,
            still_missing,
        )


def _spoken_tracks_missing_source_ids(db_file: str, session_uid: str) -> int:
    """Count spoken raw tracks whose Cozytrack source id was never persisted."""
    return sum(
        1
        for artifact in list_media_artifacts(db_file, session_uid, kind="raw_track")
        if artifact.role != "mix" and not (artifact.source_id or "").strip()
    )


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


def _select_tracks(
    *,
    session: CozytrackSession,
    excluded_track_selectors: List[str],
) -> List[CozytrackTrack]:
    if not excluded_track_selectors:
        return list(session.tracks)

    roles_by_track_id = _display_roles_by_track_id(session.tracks)
    tracks_by_id = {track.id: track for track in session.tracks}
    track_ids_by_role = {
        role.lower(): track_id for track_id, role in roles_by_track_id.items()
    }
    excluded_track_ids = set()
    for selector in excluded_track_selectors:
        selected_track = tracks_by_id.get(selector)
        if selected_track is None:
            track_id = track_ids_by_role.get(selector.lower())
            selected_track = tracks_by_id.get(track_id) if track_id else None
        if selected_track is None:
            raise CozytrackIngestError(
                "\n\n".join(
                    [
                        f"Unknown Cozytrack track selector {selector!r}. "
                        "Use a role or exact track id from the table.",
                        "Available Cozytrack tracks:\n"
                        + format_session_track_table(session),
                        _track_exclusion_guidance(session.id),
                    ]
                )
            )
        excluded_track_ids.add(selected_track.id)

    selected_tracks = [
        track for track in session.tracks if track.id not in excluded_track_ids
    ]
    if len(selected_tracks) < 2:
        raise CozytrackIngestError(
            "\n\n".join(
                [
                    "Cozytrack ingest requires at least two tracks after exclusions; "
                    f"{len(selected_tracks)} would remain.",
                    "Available Cozytrack tracks:\n"
                    + format_session_track_table(session),
                ]
            )
        )
    return selected_tracks


def _display_roles_by_track_id(tracks: List[CozytrackTrack]) -> Dict[str, str]:
    roles = _derive_participant_roles(tracks)
    if roles is not None:
        return roles
    sorted_tracks = sorted(
        tracks,
        key=lambda track: (track.created_at or "", track.id),
    )
    return {
        track.id: _legacy_track_role(index)
        for index, track in enumerate(sorted_tracks, start=1)
    }


def _legacy_track_role(index: int) -> str:
    if index <= 2:
        return f"host{index}"
    return f"guest{index - 2}"


def format_session_track_table(session: CozytrackSession) -> str:
    """Render stable, copyable track choices for an ingest operator."""
    if not session.tracks:
        return "(no tracks)"
    roles_by_track_id = _display_roles_by_track_id(session.tracks)
    sorted_tracks = sorted(
        session.tracks,
        key=lambda track: _derived_role_order(roles_by_track_id[track.id]),
    )
    headers = ("role", "participant", "duration", "started", "status", "track id")
    rows = [
        (
            roles_by_track_id[track.id],
            _table_cell(track.participant_name or "unknown", 24),
            _format_duration_ms(track.duration_ms),
            _table_cell(track.created_at or "unknown", 24),
            _table_cell(track.status or "unknown", 10),
            track.id,
        )
        for track in sorted_tracks
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    lines = [
        "  ".join(
            header.ljust(widths[index]) for index, header in enumerate(headers)
        ),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))
        for row in rows
    )
    return "\n".join(lines)


def _format_duration_ms(duration_ms: Optional[int]) -> str:
    if duration_ms is None or duration_ms < 0:
        return "unknown"
    total_seconds, milliseconds = divmod(duration_ms, 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
    return f"{minutes}:{seconds:02d}.{milliseconds:03d}"


def _table_cell(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return value[: width - 1] + "…"


def _track_exclusion_guidance(session_id: str) -> str:
    return "\n".join(
        [
            "Inspect this metadata without downloading audio:",
            f"  pod ct-ingest {session_id} --list-tracks",
            "After deciding which tracks are irrelevant, retry with one or more "
            "role labels or exact ids:",
            f"  pod ct-ingest {session_id} --exclude <role-or-track-id> "
            "[--exclude <role-or-track-id> ...]",
            "Short duration alone is not proof that a track is safe to exclude.",
        ]
    )


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
    "format_session_track_table",
    "ingest_session",
    "list_ready_sessions_for_ingest",
    "slugify",
]
