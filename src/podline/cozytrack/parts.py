"""Take-aware source registration. A reconnect is evidence, never a new part."""

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import shutil
from uuid import uuid4
from typing import Dict, List, Optional, Tuple

from podline.artifacts import store_artifact_file
from podline.cozytrack.client import (
    CozytrackSession, CozytrackTake, CozytrackTrack, host_channel_number,
    is_host_participant,
)
from podline.db import connect, recording_part_id
from podline.fingerprint import build_session_uid
from podline.ingest import register_candidate_with_storage
from podline.models import ScanCandidate, TrackFile
from podline.repository import (
    add_recording_part_timing_warnings, create_recording_part, ensure_default_part,
    get_participant_roles, get_session,
    get_media_artifact, get_session_by_cozytrack_id, list_media_artifacts, list_recording_parts,
    reconcile_artifact_roles, set_participant_roles, update_recording_part,
    update_session_status,
)


def _error(message):
    from podline.cozytrack.ingest import CozytrackIngestError
    return CozytrackIngestError(message)


def _time(value, description):
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc)
    except ValueError as exc:
        raise _error(f"{description} has invalid timestamp {value!r}") from exc


def group_tracks(session: CozytrackSession):
    """Validate source associations without time-clustering or dropping tracks."""
    if not session.tracks:
        raise _error(f"Cozytrack session {session.id} has no tracks")
    ids = [track.id for track in session.tracks]
    if len(ids) != len(set(ids)):
        raise _error("Cozytrack response contains duplicate track IDs")
    take_ids = {track.take_id for track in session.tracks}
    if None in take_ids and len(take_ids) > 1:
        raise _error("Mixed take IDs: inspect tracks before ingest")
    if take_ids == {None}:
        return [(None, session.tracks)]
    if session.recording_takes is None:
        if len(take_ids) == 1:
            return [(None, session.tracks)]  # historical single-take contract
        raise _error("Multiple take IDs require recordingTakes lifecycle metadata")
    takes = {take.id: take for take in session.recording_takes}
    if len(takes) != len(session.recording_takes):
        raise _error("Cozytrack response contains duplicate take IDs")
    missing = take_ids - set(takes)
    if missing:
        raise _error(f"Tracks reference missing takes: {sorted(missing)}")
    for take in takes.values():
        if take.status != "stopped":
            raise _error(f"Take {take.id} is not stopped; finish recording before ingest")
    ordered = sorted(takes.values(), key=lambda take: (_time(take.started_at, take.id), take.id))
    return [
        (take, [track for track in session.tracks if track.take_id == take.id])
        for take in ordered if take.id in take_ids
    ]


def participant_roles(groups, previous=None):
    """Assign once per participant, not once per returned track."""
    from podline.cozytrack.ingest import _legacy_track_role
    roles = dict(previous or {})
    tracks = [track for _take, part_tracks in groups for track in part_tracks]
    if not any(track.participant_id for track in tracks):
        if len(groups) != 1:
            raise _error("Multipart tracks require participantId for stable speaker identity")
        return {}, {
            track.id: _legacy_track_role(index)
            for index, track in enumerate(
                sorted(tracks, key=lambda track: (track.created_at or "", track.id)), 1
            )
        }
    next_guest = max(
        [int(role[5:]) for role in roles.values()
         if role.startswith("guest") and role[5:].isdigit()] or [0]
    ) + 1
    track_roles = {}
    for _take, part_tracks in groups:
        assigned = set()
        for track in sorted(part_tracks, key=lambda item: (item.created_at or "", item.id)):
            identity = (track.participant_id or "").strip()
            if not identity:
                raise _error(f"Track {track.id} is missing participantId")
            if identity not in roles:
                if is_host_participant(identity):
                    number = host_channel_number(identity)
                    if number is None:
                        raise _error(f"Track {track.id} has invalid host channel {identity}")
                    roles[identity] = f"host{number}"
                else:
                    roles[identity] = f"guest{next_guest}"
                    next_guest += 1
            role = roles[identity]
            if role in assigned:
                raise _error(f"Take contains duplicate participant role {role}: {track.id}")
            assigned.add(role)
            track_roles[track.id] = role
    return roles, track_roles


def timing_warnings(tracks):
    warnings = []
    for track in tracks:
        for condition, reason in (
            (track.partial is True, "partial recording"),
            ((track.segment_count or 0) > 1, "known reconnect/recorder restart"),
            ((track.incomplete_segment_count or 0) > 0, "incomplete recorder segments"),
        ):
            if condition:
                warnings.append({"track_id": track.id, "reason": reason})
    return warnings


def format_part_tracks(session):
    from podline.cozytrack.ingest import _format_duration_ms
    warning = ""
    try:
        groups = group_tracks(session)
        _roles, by_track = participant_roles(groups)
        numbers = {track.id: number for number, (_take, tracks) in enumerate(groups, 1)
                   for track in tracks}
    except Exception as exc:
        warning = f"Metadata warning: {exc}\n"
        by_track, numbers = {}, {}
    lines = ["selector  take  participant  duration  started  status  track id  timing"]
    for track in session.tracks:
        selector = (
            f"part-{numbers[track.id]:02d}:{by_track[track.id]}"
            if track.id in numbers else "(use track id)"
        )
        reasons = ", ".join(item["reason"] for item in timing_warnings([track]))
        if not reasons:
            reasons = "unknown" if track.segment_count is None else "none known"
        lines.append("  ".join([
            selector, track.take_id or "legacy/default",
            (track.participant_name or track.participant_id or "unknown").replace("\n", " "),
            _format_duration_ms(track.duration_ms), track.created_at or "unknown",
            track.status or "unknown", track.id, reasons,
        ]))
    return warning + "\n".join(lines)


def _snapshot(take, tracks, track_roles, excluded):
    return {
        "take_id": take.id if take else tracks[0].take_id,
        "tracks": [
            {**asdict(track), "role": track_roles[track.id]}
            for track in sorted(tracks, key=lambda item: item.id)
        ],
        "excluded_track_ids": sorted(excluded),
    }


def _materialization(snapshot):
    # Names are mutable; they are not audio identity or reconnect evidence.
    return [
        {key: value for key, value in track.items() if key != "participant_name"}
        for track in snapshot.get("tracks", [])
    ]


def select_tracks(session, selectors, previous_roles=None):
    from dataclasses import replace
    tracks = list(session.tracks)
    exact_ids = {track.id for track in tracks}
    excluded = set(selectors) & exact_ids
    remaining = [selector for selector in selectors if selector not in exact_ids]
    session = replace(session, tracks=[track for track in tracks if track.id not in excluded])
    if remaining:
        groups = group_tracks(session)
        _roles, by_track = participant_roles(groups, previous_roles)
        choices = {}
        for number, (_take, part_tracks) in enumerate(groups, 1):
            for track in part_tracks:
                role = by_track[track.id]
                choices.setdefault(role, []).append(track.id)
                choices[f"part-{number:02d}:{role}"] = [track.id]
        for selector in remaining:
            matches = choices.get(selector.lower(), [])
            if len(matches) != 1:
                raise _error(
                    f"Unknown or ambiguous track selector {selector!r}; "
                    "use an exact track ID or part-02:guest1"
                )
            excluded.add(matches[0])
    selected = replace(session, tracks=[track for track in tracks if track.id not in excluded])
    return selected, excluded


def _register_episode(db_file, session, name, groups, snapshots, roles):
    fingerprint = hashlib.sha256(f"cozytrack-session:{session.id}".encode()).hexdigest()
    uid = build_session_uid(fingerprint)
    now = datetime.now(timezone.utc).isoformat()
    with connect(db_file) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO sessions (session_uid,source_basename,source_path,"
            "fingerprint,status,cozytrack_session_id,participant_roles,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, name or session.name or session.id, f"cozytrack://{session.id}",
             fingerprint, "queued", session.id, json.dumps(roles), now, now),
        )
        for number, ((take, tracks), snapshot) in enumerate(zip(groups, snapshots), 1):
            connection.execute(
                "INSERT INTO recording_parts (part_id,session_uid,part_number,take_id,"
                "status,ingest_complete,source_snapshot,timing_warnings,started_at,"
                "stopped_at,take_status,output_basename,created_at,updated_at) "
                "VALUES (?,?,?,?,'queued',0,?,?,?,?,?,?,?,?)",
                (recording_part_id(uid, number), uid, number, snapshot["take_id"],
                 json.dumps(snapshot), json.dumps(timing_warnings(tracks)),
                 take.started_at if take else None, take.stopped_at if take else None,
                 take.status if take else None, f"part-{number:02d}", now, now),
            )
        connection.commit()
    return get_session(db_file, uid)


def _download_candidate(session, tracks, track_roles, folder, client, name):
    from podline.scanner import recorder_track_number
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    source_ids = {}
    for number, track in enumerate(sorted(
        tracks, key=lambda item: (recorder_track_number(track_roles[item.id]) or 100, item.id)
    ), 1):
        path = folder / f"Tr{number}.webm"
        client.download_to_file(client.get_track_download_url(track.id), path)
        source_ids[path.name] = track.id
        files.append(TrackFile(
            track_name=f"Tr{number}", track_number=number,
            role=track_roles[track.id], path=path, file_size=path.stat().st_size,
            has_audio=True, activity_rms_db=None,
        ))
    return ScanCandidate(
        source_basename=name or session.name or session.id, session_path=folder,
        session_folder_name=folder.name, date_folder_name="", recorded_at=None,
        recorded_at_source=None, spoken_tracks=files, mix_track=None,
    ), source_ids


def ingest_parts(*, config, runtime_paths, db_file, session, client,
                 name_override=None, excluded_track_selectors=None, storage=None,
                 duration_probe=None, logger=None):
    """Durable source checkpoints; no recording or Auphonic mutations."""
    from podline.auphonic import probe_audio_duration_from_packets
    if not config.storage.enabled:
        raise _error("storage.enabled is false; S3-native ingest is paused")
    existing = get_session_by_cozytrack_id(db_file, session.id)
    selectors = excluded_track_selectors or []
    if existing and selectors:
        raise _error("--exclude only applies to the first ingest")
    previous_roles = get_participant_roles(db_file, existing.session_uid) if existing else {}
    existing_parts = list_recording_parts(db_file, existing.session_uid) if existing else []
    persisted_exclusions = {
        source_id for part in existing_parts
        for source_id in part.source_snapshot.get("excluded_track_ids", [])
    }
    session, excluded = select_tracks(
        session, list(persisted_exclusions or selectors), previous_roles,
    )
    groups = group_tracks(session)
    for _take, tracks in groups:
        for track in tracks:
            if track.status != "complete" or track.s3_purged_at:
                raise _error(f"Track {track.id} is not a complete downloadable recording")
    roles, by_track = participant_roles(groups, previous_roles)
    snapshots = [_snapshot(take, tracks, by_track, excluded) for take, tracks in groups]
    if existing and len(existing_parts) == 1 and not existing_parts[0].source_snapshot:
        from podline.cozytrack.ingest import (
            _heal_associated_session, _spoken_tracks_missing_source_ids,
        )
        if _spoken_tracks_missing_source_ids(db_file, existing.session_uid):
            _heal_associated_session(
                runtime_paths=runtime_paths, db_file=db_file, existing=existing,
                cozytrack_session=session, client=client, logger=logger,
            )
    # Each invocation owns fresh inodes, including when two ingests overlap.
    folder = (Path(runtime_paths.staging_dir) / "ct-ingest" /
              f"{hashlib.sha256(session.id.encode()).hexdigest()}-{uuid4().hex}")
    probe = duration_probe or probe_audio_duration_from_packets
    try:
        if existing is None and len(groups) == 1:
            # Preserve the established local-import/content deduplication path.
            candidate, source_ids = _download_candidate(
                session, groups[0][1], by_track, folder / "single", client, name_override,
            )
            existing, _created = register_candidate_with_storage(
                config=config, runtime_paths=runtime_paths, db_file=db_file,
                candidate=candidate, source_provider="cozytrack",
                cozytrack_session_id=session.id, track_source_ids=source_ids,
                storage=storage, duration_probe=probe, logger=logger,
                part_metadata={
                    "take_id": snapshots[0]["take_id"], "source_snapshot": snapshots[0],
                    "timing_warnings": timing_warnings(groups[0][1]),
                    "started_at": groups[0][0].started_at if groups[0][0] else None,
                    "stopped_at": groups[0][0].stopped_at if groups[0][0] else None,
                    "take_status": groups[0][0].status if groups[0][0] else None,
                    "participant_roles": roles,
                },
            )
            existing_parts = [ensure_default_part(db_file, existing.session_uid)]
        if existing is None:
            existing = _register_episode(
                db_file, session, name_override, groups, snapshots, roles,
            )
            existing_parts = list_recording_parts(db_file, existing.session_uid)
        parts = list(existing_parts)
        finalized = existing.status in {
            "finalize_started", "ready_for_draft", "draft_created",
        } or get_media_artifact(db_file, existing.session_uid, kind="final_master") is not None
        plans = []
        for number, ((take, tracks), snapshot) in enumerate(zip(groups, snapshots), 1):
            take_id = snapshot["take_id"]
            part = next((part for part in parts if part.take_id == take_id), None)
            if part is None and len(parts) == 1 and parts[0].take_id is None:
                raw = list_media_artifacts(
                    db_file, existing.session_uid, kind="raw_track",
                    part_id=parts[0].part_id,
                )
                if {a.source_id for a in raw} == {track.id for track in tracks}:
                    part = update_recording_part(db_file, parts[0].part_id, take_id=take_id)
            if part is None:
                if finalized:
                    raise _error(f"Finalized episode cannot add take {take_id}")
                if number <= len(parts):
                    raise _error(f"Take {take_id} would renumber an existing part")
                part = create_recording_part(
                    db_file, session_uid=existing.session_uid, part_number=number,
                    take_id=take_id, ingest_complete=False,
                    output_basename=f"part-{number:02d}" if len(groups) > 1 else None,
                )
                parts.append(part)
            if part.source_snapshot and _materialization(part.source_snapshot) != _materialization(snapshot):
                raise _error(
                    f"Part {part.part_number} source materialization changed; "
                    "reconcile the source explicitly before reusing its production"
                )
            plans.append((part, take, tracks, snapshot))
        if {part.part_id for part, *_ in plans} != {part.part_id for part in parts}:
            raise _error("Source takes changed: an existing part is missing")
        set_participant_roles(db_file, existing.session_uid, roles)
        for part, take, tracks, snapshot in plans:
            raw = list_media_artifacts(
                db_file, existing.session_uid, kind="raw_track", part_id=part.part_id,
            )
            by_source = {artifact.source_id: artifact for artifact in raw}
            if raw:
                role_updates = {
                    artifact.role: by_track[artifact.source_id]
                    for artifact in raw if artifact.source_id in by_track
                    and artifact.role != by_track[artifact.source_id]
                }
                if role_updates:
                    reconcile_artifact_roles(
                        db_file, session_uid=existing.session_uid,
                        status=existing.status, role_updates=role_updates,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        detail="reconciled Cozytrack participant roles", part_id=part.part_id,
                    )
            update_recording_part(
                db_file, part.part_id,
                source_snapshot={**part.source_snapshot, **snapshot},
                started_at=take.started_at if take else None,
                stopped_at=take.stopped_at if take else None,
                take_status=take.status if take else None,
            )
            add_recording_part_timing_warnings(db_file, part.part_id, timing_warnings(tracks))
            from podline.cozytrack.ingest import _derived_role_order
            for index, track in enumerate(sorted(
                tracks, key=lambda item: _derived_role_order(by_track[item.id])
            ), 1):
                if track.id in by_source:
                    continue
                path = folder / part.part_id / f"Tr{index}.webm"
                path.parent.mkdir(parents=True, exist_ok=True)
                client.download_to_file(client.get_track_download_url(track.id), path)
                store_artifact_file(
                    config=config, runtime_paths=runtime_paths, db_file=db_file,
                    session_uid=existing.session_uid, part_id=part.part_id,
                    kind="raw_track", role=by_track[track.id], source_path=path,
                    content_type="audio/webm", duration_seconds=float(probe(path)),
                    source_provider="cozytrack", source_id=track.id, storage=storage,
                )
                path.unlink()  # do not write through the cache's hardlink on retry
            update_recording_part(db_file, part.part_id, ingest_complete=True)
        try:
            latest = client.get_session(session.id)
            latest, _ = select_tracks(latest, list(excluded), roles)
            latest_groups = group_tracks(latest)
            _, latest_roles = participant_roles(latest_groups, roles)
            latest_snapshots = [
                _snapshot(take, tracks, latest_roles, excluded)
                for take, tracks in latest_groups
            ]
            if ([_materialization(s) for s in snapshots] !=
                    [_materialization(s) for s in latest_snapshots]):
                raise _error("Source materialization changed during ingest; retry after recording stops")
        except Exception:
            for part, *_ in plans:
                if part.status == "queued":
                    update_recording_part(db_file, part.part_id, ingest_complete=False)
            raise
        if not finalized and any(part.status == "queued" for part in list_recording_parts(db_file, existing.session_uid)):
            update_session_status(
                db_file, session_uid=existing.session_uid, from_state=existing.status,
                to_state="queued", timestamp=datetime.now(timezone.utc).isoformat(),
                detail="Cozytrack part sources registered",
                replace_auphonic_production_id=len(parts) > 1,
            )
        return get_session(db_file, existing.session_uid)
    except Exception as exc:
        from podline.cozytrack.ingest import CozytrackIngestError
        if isinstance(exc, CozytrackIngestError):
            raise
        raise _error(f"Cozytrack part ingest interrupted: {exc}") from exc
    finally:
        shutil.rmtree(folder, ignore_errors=True)
