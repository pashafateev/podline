"""Advance a session through pre-edit processing to its Cubase workspace."""

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Dict, Optional

from podline.auphonic import (
    AuphonicClient,
    AuphonicClientProtocol,
    AuphonicError,
    AuphonicProduction,
    _extract_output_files,
    _find_multi_input_track_for_asset,
    _is_done_status,
    _is_failed_status,
    _spoken_raw_tracks,
    _validate_offset_production_state,
    complete_pre_edit_production,
    inspect_production,
    offset_session_auphonic_production,
    prepare_session_with_auphonic,
    verify_production_offsets,
)
from podline.config import AppConfig
from podline.models import RuntimePaths, SessionRecord, SessionStatus
from podline.repository import offsets_applied, update_session_status


def _transition(
    db_file: str, session: SessionRecord, status: SessionStatus, detail: str
) -> SessionRecord:
    update_session_status(
        db_file,
        session_uid=session.session_uid,
        from_state=session.status,
        to_state=status.value,
        timestamp=datetime.now(timezone.utc).isoformat(),
        detail=detail,
    )
    return replace(session, status=status.value, failed_at_stage=None, failure_reason=None)


def advance_pre_edit(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    session: SessionRecord,
    logger: Optional[logging.Logger] = None,
    client: Optional[AuphonicClientProtocol] = None,
) -> SessionRecord:
    db_file = runtime_paths.db_file
    client = client or AuphonicClient(config)
    if (
        session.status == SessionStatus.FAILED.value
        and session.failed_at_stage == "auphonic"
        and not session.auphonic_production_id
    ):
        session = _transition(
            db_file, session, SessionStatus.QUEUED, "retrying Auphonic preparation"
        )
    if session.status == SessionStatus.QUEUED.value:
        session = prepare_session_with_auphonic(
            config=config, db_file=db_file, session=session, client=client, logger=logger
        )
    production_id = session.auphonic_production_id
    if session.status not in {
        SessionStatus.AUPHONIC_PREPARED.value,
        SessionStatus.AUPHONIC_PREVIEWED.value,
        SessionStatus.AUPHONIC_STARTED.value,
    } or not production_id:
        raise AuphonicError(f"Cannot advance pre-edit from {session.status} for {session.session_uid}")

    details = inspect_production(config, production_id, client=client)
    if _is_failed_status(details):
        reason = details["error_message"] or details["status_string"]
        update_session_status(
            db_file,
            session_uid=session.session_uid,
            from_state=session.status,
            to_state=SessionStatus.FAILED.value,
            timestamp=datetime.now(timezone.utc).isoformat(),
            detail=f"Auphonic production {production_id} failed: {reason}",
            failure_reason=reason,
            failed_at_stage="auphonic",
        )
        raise AuphonicError(f"Auphonic production {production_id} failed: {reason}")

    if _is_done_status(details):
        production = AuphonicProduction(production_id, _extract_output_files(details))
    else:
        if details["start_allowed"] is True:
            if session.status == SessionStatus.AUPHONIC_STARTED.value:
                raise AuphonicError(
                    f"A start was already requested for {production_id}. Verify its state with "
                    f"pod auphonic-inspect --session {session.session_uid}, then run "
                    f"pod auphonic-sync --session {session.session_uid}."
                )
            _validate_offset_production_state(details)
            try:
                _align_multitrack(config, runtime_paths, session, client, details)
            except AuphonicError as exc:
                update_session_status(
                    db_file, session_uid=session.session_uid, from_state=session.status,
                    to_state=session.status, timestamp=datetime.now(timezone.utc).isoformat(),
                    detail="track alignment requires review", failed_at_stage="alignment",
                    failure_reason=str(exc),
                )
                raise
            # Save intent before the request. A lost response must never cause a second start.
            session = _transition(
                db_file, session, SessionStatus.AUPHONIC_STARTED, "requesting Auphonic start"
            )
            client.start_production(production_id)
        elif details["start_allowed"] is not False:
            raise AuphonicError(f"Auphonic did not report whether production {production_id} may start")
        elif session.status != SessionStatus.AUPHONIC_STARTED.value:
            session = _transition(
                db_file, session, SessionStatus.AUPHONIC_STARTED, "resuming remote Auphonic run"
            )
        production = client.wait_for_production(production_id)
        details = None

    return complete_pre_edit_production(
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        session=session,
        production=production,
        client=client,
        production_details=details,
        logger=logger,
    )



def _align_multitrack(
    config: AppConfig,
    runtime_paths: RuntimePaths,
    session: SessionRecord,
    client: AuphonicClientProtocol,
    details: Dict,
) -> None:
    db_file = runtime_paths.db_file
    tracks = _spoken_raw_tracks(config, db_file, session.session_uid)
    if len(tracks) == 1:
        _find_multi_input_track_for_asset(details["multi_input_files"], tracks[0])
        return
    applied = offsets_applied(db_file, session.session_uid, session.auphonic_production_id)
    result = offset_session_auphonic_production(
        config=config, runtime_paths=runtime_paths, db_file=db_file,
        session=session, client=client, apply=not applied,
    )
    if applied:
        # An operator may have edited a previously verified production in the browser.
        latest = inspect_production(config, result.plan.production_id, client=client)
        verify_production_offsets(result.plan, latest)
