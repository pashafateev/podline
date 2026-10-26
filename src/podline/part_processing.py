"""Synchronous episode commands over explicit, durable part ownership."""

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import unquote, urlparse

from podline.auphonic import (
    AuphonicAssetDuration, AuphonicClient, AuphonicError, AuphonicOffsetPlan,
    AuphonicOffsetResult, AuphonicOffsetTarget, AuphonicProduction,
    _artifact_key, _extract_output_files, _find_multi_input_track_by_id,
    _find_multi_input_track_for_asset, _is_done_status, _is_failed_status,
    _pair_tracks_with_preset_ids, _persist_outputs, _recorder_order_key,
    _resolve_artifact_duration, _spoken_raw_tracks, _stem_track_bindings,
    _validate_offset_production_state, _validate_offset_timing_against_created_at,
    inspect_production, probe_audio_duration_from_packets, resolve_auphonic_service_id,
    resolve_preset_id,
)
from podline.artifacts import create_workspace
from podline.repository import (
    add_recording_part_timing_warnings, claim_recording_part_operation,
    get_recording_part, get_session,
    list_media_artifacts, list_recording_parts, update_recording_part,
    update_session_status,
)


_POST_EDIT = {"finalize_started", "ready_for_draft", "draft_created"}


def uses_part_workflow(db_file, session):
    parts = list_recording_parts(db_file, session.session_uid)
    return len(parts) > 1 or any(part.source_snapshot.get("tracks") for part in parts)


def select_parts(db_file, session, part_number=None):
    parts = list_recording_parts(db_file, session.session_uid)
    if part_number is not None:
        parts = [part for part in parts if part.part_number == part_number]
    if not parts:
        raise AuphonicError(f"No recording part found: {part_number or 'default'}")
    return parts


def aggregate_status(db_file, session):
    current = get_session(db_file, session.session_uid)
    if current.status in _POST_EDIT:
        return current
    parts = list_recording_parts(db_file, session.session_uid)
    statuses = {part.status for part in parts}
    failure = next((part for part in parts if part.status == "failed"), None)
    if all(part.ingest_complete and part.status == "complete" for part in parts):
        status = "waiting_for_finalize"
    elif failure:
        status = "failed"
    elif statuses & {"started", "starting"}:
        status = "auphonic_started"
    elif statuses <= {"previewed", "complete"}:
        status = "auphonic_previewed"
    elif statuses <= {"prepared", "previewed", "complete"}:
        status = "auphonic_prepared"
    else:
        status = "queued"
    update_session_status(
        db_file, session_uid=session.session_uid, from_state=current.status,
        to_state=status, timestamp=datetime.now(timezone.utc).isoformat(),
        detail="aggregated recording-part progress",
        auphonic_production_id=parts[0].auphonic_production_id if len(parts) == 1 else None,
        failure_reason=(f"Part {failure.part_number}: {failure.failure_reason}" if failure else None),
        failed_at_stage="auphonic" if failure else None,
    )
    return get_session(db_file, session.session_uid)


def _fail(db_file, session, part, exc):
    update_recording_part(
        db_file, part.part_id, status="failed", failure_reason=str(exc),
    )
    aggregate_status(db_file, session)
    raise AuphonicError(f"Part {part.part_number}: {exc}") from exc


def _marker(session, part):
    return f"[podline:{session.session_uid}/{part.part_id}]"


def _raw(config, db_file, part):
    tracks = sorted(
        _spoken_raw_tracks(config, db_file, part.session_uid, part_id=part.part_id),
        key=lambda artifact: _recorder_order_key(artifact.role),
    )
    identities = {
        track["id"]: track["role"]
        for track in part.source_snapshot.get("tracks", [])
        if "id" in track and "role" in track
    }
    if identities and (
        {artifact.source_id for artifact in tracks} != set(identities)
        or any(identities.get(artifact.source_id) != artifact.role for artifact in tracks)
    ):
        raise AuphonicError(
            f"Part {part.part_number}: source identity reconciliation is unfinished; "
            "rerun ct-ingest before preparing or starting this part"
        )
    return tracks


def _same_input(input_file, key):
    value = unquote(str(input_file or ""))
    if value == key:
        return True
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.path.lstrip("/") == key)


def _bindings(config, db_file, part, details, track_ids=None, *, require_attached=False):
    tracks = _raw(config, db_file, part)
    saved = part.source_snapshot.get("auphonic_bindings", {})
    remote = details.get("multi_input_files") or []
    ids = track_ids or list(saved.values()) or [str(track["id"]) for track in remote]
    if saved:
        by_id = {str(track.get("id")): track for track in remote}
        if set(saved) != {artifact.role for artifact in tracks}:
            raise AuphonicError("stored preset bindings no longer match source roles")
        if any(track_id not in by_id for track_id in saved.values()):
            raise AuphonicError("prepared preset track IDs changed; inspect before retry")
        pairs = [(saved[artifact.role], artifact) for artifact in tracks]
    else:
        pairs = _pair_tracks_with_preset_ids(ids or [a.role for a in tracks], tracks)
    wanted = {track_id: _artifact_key(config, artifact) for track_id, artifact in pairs}
    for track in remote:
        if track.get("input_file") and (
            str(track.get("id")) not in wanted
            or not _same_input(track["input_file"], wanted[str(track["id"])])
        ):
            raise AuphonicError("production has foreign or changed source inputs")
    if require_attached:
        attached = {str(track.get("id")): track.get("input_file") for track in remote}
        if any(not attached.get(track_id) for track_id in wanted):
            raise AuphonicError("production is missing an expected source attachment")
    return pairs


def prepare_parts(*, config, db_file, session, client=None, part_number=None, logger=None):
    client = client or AuphonicClient(config)
    selected = select_parts(db_file, session, part_number)
    service_id = resolve_auphonic_service_id(config)
    presets = {}
    for part in selected:
        if not part.ingest_complete:
            raise AuphonicError(f"Part {part.part_number}: source ingest is unfinished")
        tracks = _raw(config, db_file, part)
        if len(tracks) not in {2, 3, 4}:
            raise AuphonicError(f"Part {part.part_number}: unsupported participant count {len(tracks)}")
        presets[part.part_id] = resolve_preset_id(config, len(tracks))
    for part in selected:
        if part.auphonic_production_id and part.status in {
            "prepared", "previewed", "starting", "started", "complete",
        }:
            continue
        owns_operation = bool(part.auphonic_production_id)
        try:
            track_ids = None
            if not part.auphonic_production_id:
                claimed = not part.operation and claim_recording_part_operation(
                    db_file, part.part_id, "create",
                )
                if not claimed:
                    latest = get_recording_part(db_file, part.part_id)
                    if latest.auphonic_production_id:
                        continue  # another prepare completed after our initial read
                    raise AuphonicError(
                        "creation is pending or its outcome is unknown; reconcile the "
                        "existing production with auphonic-sync <id> --session <episode> --part N"
                    )
                owns_operation = True
                update_recording_part(db_file, part.part_id, status="preparing")
                prepared = client.create_production(
                    title=f"{session.source_basename or session.session_uid} "
                          f"Part {part.part_number:02d} {_marker(session, part)}",
                    track_count=len(_raw(config, db_file, part)),
                    preset_id=presets[part.part_id],
                )
                if any(
                    other.part_id != part.part_id and
                    other.auphonic_production_id == prepared.production_id
                    for other in list_recording_parts(db_file, session.session_uid)
                ):
                    raise AuphonicError("production already belongs to another part")
                part = update_recording_part(
                    db_file, part.part_id, auphonic_production_id=prepared.production_id,
                    operation=None,
                )
                track_ids = prepared.upload_track_ids
            details = inspect_production(config, part.auphonic_production_id, client=client)
            if _is_failed_status(details):
                raise AuphonicError("remote production failed; inspect before explicit reset")
            if not details.get("start_allowed"):
                update_recording_part(
                    db_file, part.part_id, status="started", operation="poll", failure_reason=None,
                )
                continue  # preparation must not restart or modify paid work
            pairs = _bindings(config, db_file, part, details, track_ids)
            snapshot = {
                **part.source_snapshot,
                "auphonic_bindings": {artifact.role: track_id for track_id, artifact in pairs},
            }
            update_recording_part(db_file, part.part_id, source_snapshot=snapshot)
            attached = {str(track.get("id")): track for track in details["multi_input_files"]}
            for track_id, artifact in pairs:
                if not attached.get(track_id, {}).get("input_file"):
                    client.attach_service_input(
                        part.auphonic_production_id, track_id, service_id,
                        _artifact_key(config, artifact),
                    )
            verified = inspect_production(config, part.auphonic_production_id, client=client)
            for track_id, artifact in pairs:
                remote = _find_multi_input_track_by_id(verified["multi_input_files"], track_id)
                if remote is None or not _same_input(remote.get("input_file"), _artifact_key(config, artifact)):
                    raise AuphonicError(f"source attachment was not verified for {artifact.role}")
            update_recording_part(
                db_file, part.part_id, status="prepared", operation=None, failure_reason=None,
            )
        except Exception as exc:
            if not owns_operation:
                raise AuphonicError(f"Part {part.part_number}: {exc}") from exc
            _fail(db_file, session, part, exc)
    return aggregate_status(db_file, session)


def preview_parts(*, config, db_file, session, client=None, part_number=None, logger=None):
    client = client or AuphonicClient(config)
    selected = select_parts(db_file, session, part_number)
    for part in selected:
        if not part.auphonic_production_id or part.status not in {"prepared", "previewed", "complete"}:
            raise AuphonicError(f"Part {part.part_number} must be prepared before preview")
    previews = []
    for part in selected:
        details = inspect_production(config, part.auphonic_production_id, client=client)
        if _is_failed_status(details) or not details.get("edit_page"):
            raise AuphonicError(f"Part {part.part_number} has no usable edit preview")
        _bindings(config, db_file, part, details, require_attached=True)
        if part.status != "complete":
            part = update_recording_part(
                db_file, part.part_id, status="previewed",
                source_snapshot={**part.source_snapshot, "previewed": True},
            )
        previews.append((part, details))
    aggregate_status(db_file, session)
    return previews


def _persist_part(config, runtime_paths, db_file, session, part, production, client, logger):
    if production.production_id != part.auphonic_production_id:
        raise AuphonicError("completed production does not belong to this part")
    details = inspect_production(config, part.auphonic_production_id, client=client)
    _bindings(config, db_file, part, details, require_attached=True)
    basename = part.output_basename
    if basename == session.session_uid:
        basename = None  # existing single-part filenames
    _persist_outputs(
        client=client, config=config, runtime_paths=runtime_paths, db_file=db_file,
        session_uid=session.session_uid, production=production, logger=logger,
        part_id=part.part_id, output_basename=basename,
    )
    update_recording_part(
        db_file, part.part_id, status="complete", operation=None, failure_reason=None,
    )
    create_workspace(
        config=config, runtime_paths=runtime_paths, db_file=db_file,
        session_uid=session.session_uid,
    )


def start_parts(*, config, runtime_paths, db_file, session, client=None,
                part_number=None, logger=None):
    client = client or AuphonicClient(config)
    selected = select_parts(db_file, session, part_number)
    for part in selected:
        if not part.ingest_complete or not part.auphonic_production_id:
            raise AuphonicError(f"Part {part.part_number}: prepare and preview before start")
        if (part.status not in {"previewed", "starting", "started", "complete"}
                and not part.source_snapshot.get("previewed")):
            raise AuphonicError(f"Part {part.part_number} requires preview before start")
        if part.status != "complete":
            details = inspect_production(config, part.auphonic_production_id, client=client)
            _bindings(config, db_file, part, details, require_attached=True)
    for part in selected:
        if part.status == "complete":
            continue
        try:
            details = inspect_production(config, part.auphonic_production_id, client=client)
            _bindings(config, db_file, part, details, require_attached=True)
            if _is_failed_status(details):
                raise AuphonicError("remote production failed; inspect before replacement")
            if _is_done_status(details):
                production = AuphonicProduction(
                    part.auphonic_production_id, _extract_output_files(details),
                )
            else:
                if details.get("start_allowed"):
                    if part.operation:
                        raise AuphonicError(
                            "previous start outcome is uncertain; inspect and sync before retry"
                        )
                    if not claim_recording_part_operation(db_file, part.part_id, "start"):
                        raise AuphonicError("another start operation owns this part")
                    update_recording_part(db_file, part.part_id, status="starting")
                    client.start_production(part.auphonic_production_id)
                update_recording_part(db_file, part.part_id, status="started", operation="poll")
                production = client.wait_for_production(part.auphonic_production_id)
            update_recording_part(db_file, part.part_id, operation="outputs")
            _persist_part(config, runtime_paths, db_file, session, part, production, client, logger)
        except Exception as exc:
            _fail(db_file, session, part, exc)
    return aggregate_status(db_file, session)


def sync_parts(*, config, runtime_paths, db_file, session, production_id=None,
               client=None, part_number=None, logger=None):
    client = client or AuphonicClient(config)
    selected = select_parts(db_file, session, part_number)
    if production_id is not None:
        if part_number is None:
            selected = [part for part in selected if part.auphonic_production_id == production_id]
            if not selected:
                if len(list_recording_parts(db_file, session.session_uid)) == 1:
                    selected = select_parts(db_file, session)
                else:
                    raise AuphonicError("Select --part N to reconcile an unbound production")
        if len(selected) != 1:
            raise AuphonicError("A production ID must select exactly one part")
    for part in selected:
        remote_id = production_id or part.auphonic_production_id
        if not remote_id:
            continue
        try:
            if remote_id != part.auphonic_production_id:
                if part.auphonic_production_id:
                    raise AuphonicError("part already owns another production; explicit reset required")
                if any(
                    other.part_id != part.part_id and other.auphonic_production_id == remote_id
                    for other in list_recording_parts(db_file, session.session_uid)
                ):
                    raise AuphonicError("production belongs to another part")
                payload = client.get_production(remote_id)
                data = payload.get("data", payload)
                if _marker(session, part) not in (data.get("metadata") or {}).get("title", ""):
                    raise AuphonicError("production title does not identify this episode/part")
                expected_preset = resolve_preset_id(config, len(_raw(config, db_file, part)))
                remote_preset = data.get("preset")
                if isinstance(remote_preset, dict):
                    remote_preset = remote_preset.get("uuid") or remote_preset.get("id")
                if remote_preset != expected_preset:
                    raise AuphonicError("production preset could not be verified")
                _bindings(config, db_file, part, data)
                part = update_recording_part(
                    db_file, part.part_id, auphonic_production_id=remote_id, operation=None,
                )
            details = inspect_production(config, remote_id, client=client)
            if _is_failed_status(details):
                raise AuphonicError("remote production failed")
            if _is_done_status(details):
                _persist_part(
                    config, runtime_paths, db_file, session, part,
                    AuphonicProduction(remote_id, _extract_output_files(details)), client, logger,
                )
            elif details.get("start_allowed"):
                pairs = _bindings(config, db_file, part, details)
                attached = {
                    str(track.get("id")): track for track in details["multi_input_files"]
                }
                complete = all(
                    attached.get(track_id, {}).get("input_file")
                    for track_id, _artifact in pairs
                )
                update_recording_part(
                    db_file, part.part_id,
                    status=("previewed" if part.source_snapshot.get("previewed") else "prepared")
                    if complete else "preparing",
                    operation=None, failure_reason=None,
                )
            else:
                update_recording_part(
                    db_file, part.part_id, status="started", operation="poll", failure_reason=None,
                )
        except Exception as exc:
            _fail(db_file, session, part, exc)
    return aggregate_status(db_file, session)


def reset_parts(*, db_file, session, part_number=None):
    if part_number is None and len(list_recording_parts(db_file, session.session_uid)) != 1:
        raise AuphonicError("Multipart reset requires --part N")
    part = select_parts(db_file, session, part_number)[0]
    if part.status not in {"prepared", "previewed", "failed"} or part.operation:
        raise AuphonicError("Only a known, unstarted production can be reset; inspect/sync first")
    if part.status == "failed" and part.source_snapshot.get("previewed"):
        raise AuphonicError("Sync failed processing before abandoning its production")
    snapshot = {key: value for key, value in part.source_snapshot.items()
                if key not in {"previewed", "auphonic_bindings"}}
    update_recording_part(
        db_file, part.part_id, status="queued", auphonic_production_id=None,
        source_snapshot=snapshot, failure_reason=None,
    )
    return aggregate_status(db_file, session)


def _unsafe(db_file, part, reason, *, record=True):
    warning = {"reason": reason}
    warnings = list(part.timing_warnings)
    if record and warning not in warnings:
        add_recording_part_timing_warnings(db_file, part.part_id, [warning])
    raise AuphonicError(
        f"Part {part.part_number}: {reason}; manual alignment required. "
        f"Use workspace --sources --part {part.part_number} or inspect its Auphonic preview."
    )


def _offset_plan(config, runtime_paths, db_file, part, client, duration_probe):
    if part.timing_warnings:
        _unsafe(
            db_file, part,
            "; ".join(str(item.get("reason", item)) if isinstance(item, dict) else str(item)
                      for item in part.timing_warnings),
            record=False,
        )
    if not part.auphonic_production_id:
        raise AuphonicError(f"Part {part.part_number} has no prepared production")
    tracks = _raw(config, db_file, part)
    if len(tracks) not in {2, 3, 4}:
        raise AuphonicError(f"Part {part.part_number} has unsupported participant count")
    durations = [
        AuphonicAssetDuration(asset, _resolve_artifact_duration(
            config=config, runtime_paths=runtime_paths, db_file=db_file,
            artifact=asset, duration_probe=duration_probe,
        )) for asset in tracks
    ]
    reference = max(durations, key=lambda item: item.duration_seconds)
    metadata = {track["id"]: track for track in part.source_snapshot.get("tracks", [])}
    created = {}
    if metadata and (len(tracks) > 2 or part.take_status == "stopped"):
        try:
            for artifact in tracks:
                source = metadata[artifact.source_id]
                value = datetime.fromisoformat((source.get("created_at") or "").replace("Z", "+00:00"))
                if value.tzinfo is None:
                    raise ValueError("missing recorder timezone")
                created[artifact.source_id] = value
            _validate_offset_timing_against_created_at(
                asset_durations=durations, reference=reference,
                created_at_by_source_id=created,
            )
        except (ValueError, KeyError, AuphonicError) as exc:
            _unsafe(db_file, part, f"recorder timing is inconsistent or unavailable: {exc}")
    elif len(tracks) > 2 and len({item.duration_seconds for item in durations}) > 1:
        _unsafe(db_file, part, "recorder topology is unavailable")
    details = inspect_production(config, part.auphonic_production_id, client=client)
    _validate_offset_production_state(details)
    _bindings(config, db_file, part, details)
    targets = []
    aligned = []
    for item in durations:
        offset = reference.duration_seconds - item.duration_seconds
        if offset == 0:
            if item is not reference:
                aligned.append(item.asset)
            continue
        target = _find_multi_input_track_for_asset(details["multi_input_files"], item.asset)
        targets.append(AuphonicOffsetTarget(
            item.asset, str(target["id"]), str(target.get("input_file") or ""), offset,
        ))
    return AuphonicOffsetPlan(
        part.auphonic_production_id, durations, [], reference.asset, targets, aligned,
    )


def offset_parts(*, config, runtime_paths, db_file, session, apply=False,
                 client=None, part_number=None, duration_probe=probe_audio_duration_from_packets):
    client = client or AuphonicClient(config)
    plans = [
        (part, _offset_plan(config, runtime_paths, db_file, part, client, duration_probe))
        for part in select_parts(db_file, session, part_number)
    ]  # preflight the whole selection before any remote write
    results = []
    for part, plan in plans:
        states = []
        if apply:
            for target in plan.targets:
                client.update_multi_input_file_offset(
                    plan.production_id, target.track_id, target.offset_seconds,
                )
            details = inspect_production(config, plan.production_id, client=client)
            for target in plan.targets:
                state = _find_multi_input_track_by_id(details["multi_input_files"], target.track_id)
                if state is None or abs(float(state.get("offset", 0)) - target.offset_seconds) > 0.001:
                    raise AuphonicError(f"Part {part.part_number}: offset update was not verified")
                states.append(state)
        results.append((part, AuphonicOffsetResult(plan, apply, states)))
    return results


def stem_metadata(*, config, db_file, session, client=None, part_number=None):
    client = client or AuphonicClient(config)
    result = {}
    selected = select_parts(db_file, session, part_number)
    for part in selected:
        stems = list_media_artifacts(
            db_file, session.session_uid, kind="pre_edit_stem", part_id=part.part_id,
        )
        if not stems or {stem.source_id for stem in stems} != {part.auphonic_production_id}:
            raise AuphonicError(f"Part {part.part_number}: sync its processed stems first")
        details = inspect_production(config, part.auphonic_production_id, client=client)
        bindings = _stem_track_bindings(
            config=config, db_file=db_file, session_uid=session.session_uid,
            part_id=part.part_id, multi_input_files=details["multi_input_files"],
        )
        result[part.part_id] = {
            binding.role: {"track_id": binding.track_id,
                           "input_filename": binding.input_filename,
                           "offset_seconds": binding.offset_seconds}
            for binding in bindings
        }
    if len(list_recording_parts(db_file, session.session_uid)) == 1:
        return result[selected[0].part_id]
    return result
