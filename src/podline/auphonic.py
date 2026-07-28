from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import stat
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import subprocess
import tempfile
from time import sleep
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Tuple
from urllib.parse import quote, unquote, urlparse
import zipfile

import httpx

from podline.artifacts import (
    create_workspace,
    materialize_artifact_record,
    store_artifact_file,
    store_artifact_files,
)
from podline.config import AppConfig
from podline.cozytrack.client import (
    CozytrackClient,
    CozytrackClientProtocol,
    CozytrackError,
    is_host_participant,
)
from podline.models import (
    ArtifactKind,
    MediaArtifactRecord,
    RuntimePaths,
    SessionRecord,
    SessionStatus,
)
from podline.repository import (
    begin_auphonic_offset_apply,
    clear_auphonic_offsets_verified,
    complete_transcript_refresh,
    delete_media_artifacts,
    discard_transcript_refresh_production,
    get_media_artifact,
    get_session,
    list_media_artifacts,
    record_auphonic_offsets_verified,
    record_transcript_refresh_production,
    reset_session_to_queued,
    restore_media_artifact_after_failed_replacement,
    update_media_artifact_duration,
    update_session_status,
)
from podline.scanner import recorder_track_number
from podline.storage import parse_s3_uri, redact_url


_MAX_STEM_ENTRY_BYTES = 2 * 1024**3
_MAX_STEM_TOTAL_BYTES = 8 * 1024**3
_MAX_STEM_COMPRESSION_RATIO = 200


class AuphonicError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuphonicOutputFile:
    kind: str
    download_url: str
    extension: str


@dataclass(frozen=True)
class AuphonicProduction:
    production_id: str
    output_files: List[AuphonicOutputFile]


@dataclass(frozen=True)
class StemTrackBinding:
    role: str
    track_id: str
    input_filename: str
    offset_seconds: float


@dataclass(frozen=True)
class ExtractedStem:
    role: str
    track_id: str
    offset_seconds: float
    path: Path


@dataclass(frozen=True)
class AuphonicPreparedProduction:
    production_id: str
    upload_track_ids: List[str]
    status_page: Optional[str]
    edit_page: Optional[str]


@dataclass(frozen=True)
class AuphonicPreview:
    session: SessionRecord
    details: Dict


@dataclass(frozen=True)
class AuphonicAssetDuration:
    asset: MediaArtifactRecord
    duration_seconds: float


@dataclass(frozen=True)
class AuphonicOffsetTarget:
    asset: MediaArtifactRecord
    track_id: str
    input_file: str
    offset_seconds: float


@dataclass(frozen=True)
class AuphonicOffsetPlan:
    production_id: str
    asset_durations: List[AuphonicAssetDuration]
    local_assets: List[MediaArtifactRecord]
    reference_asset: MediaArtifactRecord
    targets: List[AuphonicOffsetTarget]
    aligned_assets: List[MediaArtifactRecord]


@dataclass(frozen=True)
class AuphonicOffsetResult:
    plan: AuphonicOffsetPlan
    applied: bool
    track_states: List[Dict]


@dataclass(frozen=True)
class _CozytrackOffsetTopology:
    local_assets: List[AuphonicAssetDuration]
    created_at_by_source_id: Dict[str, datetime]


DurationProbe = Callable[[Path], float]


# Allow recorder setup and container packet-timing jitter while still rejecting
# whole-second stop overhangs that duration-only alignment would misinterpret.
_OFFSET_CREATED_AT_TOLERANCE_SECONDS = 0.5


class AuphonicClientProtocol(Protocol):
    def create_production(
        self, *, title: str, track_count: int, preset_id: str
    ) -> AuphonicPreparedProduction: ...

    def attach_service_input(
        self, production_id: str, track_id: str, service_id: str, input_key: str
    ) -> Dict: ...

    def create_single_input_production(
        self, *, title: str, preset_id: str, service_id: str, input_key: str
    ) -> str: ...

    def start_production(self, production_id: str) -> None: ...

    def wait_for_production(self, production_id: str) -> AuphonicProduction: ...

    def download_file(self, url: str, destination: Path) -> None: ...

    def get_production(self, production_id: str) -> Dict: ...

    def update_multi_input_file_offset(
        self, production_id: str, track_id: str, offset: float
    ) -> Dict: ...


class AuphonicClient:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._client = httpx.Client(
            base_url=config.auphonic.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {config.auphonic.api_key}"},
            timeout=httpx.Timeout(
                config.auphonic.request_timeout_seconds, connect=60.0
            ),
        )

    def create_production(
        self,
        *,
        title: str,
        track_count: int,
        preset_id: str,
    ) -> AuphonicPreparedProduction:
        payload = {
            "preset": preset_id,
            "metadata": {"title": title},
        }
        if track_count > 1:
            payload["is_multitrack"] = True

        response = self._request_with_retries(
            "POST",
            "/productions.json",
            json=payload,
            headers={"Content-Type": "application/json"},
        )

        payload = response.json()
        data_node = payload.get("data", payload)
        production_id = (
            data_node.get("uuid")
            or data_node.get("id")
            or data_node.get("production_uuid")
        )
        if not production_id:
            raise AuphonicError(
                "Auphonic create response did not include a production id"
            )
        upload_track_ids = (
            ["input_file"]
            if track_count == 1
            else _resolve_preset_track_ids(data_node, track_count)
        )
        logging.getLogger("podline").debug(
            "Auphonic create details production_id=%s status_page=%s edit_page=%s start_allowed=%s change_allowed=%s upload_track_ids=%s",
            production_id,
            data_node.get("status_page"),
            data_node.get("edit_page"),
            data_node.get("start_allowed"),
            data_node.get("change_allowed"),
            upload_track_ids,
        )
        return AuphonicPreparedProduction(
            production_id=str(production_id),
            upload_track_ids=upload_track_ids,
            status_page=data_node.get("status_page"),
            edit_page=data_node.get("edit_page"),
        )

    def attach_service_input(
        self, production_id: str, track_id: str, service_id: str, input_key: str
    ) -> Dict:
        escaped_track_id = quote(str(track_id), safe="")
        response = self._request_with_retries(
            "POST",
            f"/production/{production_id}/multi_input_files/{escaped_track_id}.json",
            json={
                "type": "multitrack",
                "service": service_id,
                "input_file": input_key,
            },
            headers={"Content-Type": "application/json"},
        )
        payload = response.json()
        if payload.get("status_code", response.status_code) >= 400:
            raise AuphonicError(
                f"Auphonic attach failed for track {track_id} with status {payload.get('status_code')}: {payload.get('error_message') or payload.get('form_errors') or payload}"
            )
        logging.getLogger("podline").debug(
            "Auphonic attach production_id=%s track_id=%s input_key=%s",
            production_id,
            track_id,
            input_key,
        )
        return payload

    def create_single_input_production(
        self, *, title: str, preset_id: str, service_id: str, input_key: str
    ) -> str:
        response = self._request_with_retries(
            "POST",
            "/productions.json",
            json={
                "preset": preset_id,
                "metadata": {"title": title},
                "service": service_id,
                "input_file": input_key,
            },
            headers={"Content-Type": "application/json"},
        )
        payload = response.json()
        data_node = payload.get("data", payload)
        production_id = data_node.get("uuid") or data_node.get("id")
        if not production_id:
            raise AuphonicError(
                "Auphonic single-input create response did not include a production id"
            )
        return str(production_id)

    def start_production(self, production_id: str) -> None:
        self._request_with_retries(
            "POST",
            f"/production/{production_id}/start.json",
        )

    def wait_for_production(self, production_id: str) -> AuphonicProduction:
        for _attempt in range(self._config.auphonic.max_poll_attempts):
            response = self._request_with_retries(
                "GET",
                f"/production/{production_id}.json",
            )
            payload = response.json()
            data_node = payload.get("data", payload)
            logging.getLogger("podline").debug(
                "Auphonic poll production_id=%s attempt=%s status=%s status_string=%s error_message=%s warning_message=%s",
                production_id,
                _attempt + 1,
                data_node.get("status"),
                data_node.get("status_string"),
                payload.get("error_message") or data_node.get("error_message"),
                data_node.get("warning_message"),
            )

            if _is_failed_status(data_node):
                raise AuphonicError(
                    f"Auphonic production {production_id} failed with status={data_node.get('status')} status_string={data_node.get('status_string')} error={payload.get('error_message') or data_node.get('error_message') or 'none'}"
                )
            if _is_done_status(data_node):
                return AuphonicProduction(
                    production_id=production_id,
                    output_files=_extract_output_files(data_node),
                )

            sleep(self._config.auphonic.poll_interval_seconds)

        raise AuphonicError(
            f"Timed out waiting for Auphonic production {production_id}"
        )

    def download_file(self, url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._client.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)

    def get_production(self, production_id: str) -> Dict:
        response = self._request_with_retries(
            "GET",
            f"/production/{production_id}.json",
        )
        return response.json()

    def update_multi_input_file_offset(
        self, production_id: str, track_id: str, offset: float
    ) -> Dict:
        escaped_track_id = quote(str(track_id), safe="")
        response = self._request_with_retries(
            "POST",
            f"/production/{production_id}/multi_input_files/{escaped_track_id}.json",
            json={"type": "multitrack", "offset": offset},
            headers={"Content-Type": "application/json"},
        )
        payload = response.json()
        if payload.get("status_code", response.status_code) >= 400:
            raise AuphonicError(
                f"Auphonic offset update failed with status {payload.get('status_code')}: {payload.get('error_message') or payload.get('form_errors') or payload}"
            )
        return payload

    def _request_with_retries(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_error: Optional[Exception] = None
        for _attempt in range(3):
            try:
                logging.getLogger("podline").debug(
                    "Auphonic request method=%s url=%s attempt=%s",
                    method,
                    url,
                    _attempt + 1,
                )
                response = self._client.request(method, url, **kwargs)
                logging.getLogger("podline").debug(
                    "Auphonic response method=%s url=%s status=%s",
                    method,
                    url,
                    response.status_code,
                )
                if response.status_code >= 400:
                    body_preview = response.text[:1000]
                    logging.getLogger("podline").debug(
                        "Auphonic error body method=%s url=%s body=%s",
                        method,
                        url,
                        body_preview,
                    )
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status_code = exc.response.status_code
                if 400 <= status_code < 500 and status_code != 429:
                    raise AuphonicError(
                        f"Auphonic request failed with status {status_code}: {exc.response.text[:300]}"
                    ) from exc
                logging.getLogger("podline").debug(
                    "Auphonic retry method=%s url=%s error=%s",
                    method,
                    url,
                    exc,
                )
                sleep(1)
            except (httpx.HTTPError, httpx.TransportError) as exc:
                last_error = exc
                logging.getLogger("podline").debug(
                    "Auphonic retry method=%s url=%s error=%s",
                    method,
                    url,
                    exc,
                )
                sleep(1)
        raise AuphonicError(f"Auphonic request failed: {last_error}") from last_error


def process_session_with_auphonic(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session: SessionRecord,
    client: Optional[AuphonicClientProtocol] = None,
    logger: Optional[logging.Logger] = None,
) -> SessionRecord:
    raise AuphonicError(
        "Combined Auphonic processing cannot bypass offset verification and preview; "
        f"run pod auphonic-prepare {session.session_uid} instead"
    )


def prepare_session_with_auphonic(
    *,
    config: AppConfig,
    db_file: str,
    session: SessionRecord,
    client: Optional[AuphonicClientProtocol] = None,
    logger: Optional[logging.Logger] = None,
) -> SessionRecord:
    if session.status != SessionStatus.QUEUED.value:
        raise AuphonicError(
            f"Session {session.session_uid} must be queued before Auphonic prepare"
        )

    tracks = _spoken_raw_tracks(config, db_file, session.session_uid)
    preset_id = resolve_preset_id(config, len(tracks))
    service_id = resolve_auphonic_service_id(config)
    client = client or AuphonicClient(config)
    timestamp = datetime.now(timezone.utc).isoformat()
    production_id = None

    try:
        _log(logger, "Preparing Auphonic production for %s", session.session_uid)
        _debug_log(
            logger,
            "Auphonic preset=%s service=%s roles=%s keys=%s",
            preset_id,
            service_id,
            [artifact.role for artifact in tracks],
            [_artifact_key(config, artifact) for artifact in tracks],
        )
        prepared_production = client.create_production(
            title=session.source_basename or session.session_uid,
            track_count=len(tracks),
            preset_id=preset_id,
        )
        production_id = prepared_production.production_id
        _log(
            logger,
            "Created Auphonic production %s for %s",
            production_id,
            session.session_uid,
        )
        track_ids = prepared_production.upload_track_ids or [
            artifact.role for artifact in tracks
        ]
        pairs = _pair_tracks_with_preset_ids(track_ids, tracks)
        for track_id, artifact in pairs:
            _log(
                logger,
                "Attaching %s (role %s) to Auphonic track '%s'",
                artifact.filename,
                artifact.role,
                track_id,
            )
            client.attach_service_input(
                production_id,
                track_id,
                service_id,
                _artifact_key(config, artifact),
            )
        _log(
            logger,
            "Attached %s S3 tracks to Auphonic production %s",
            len(tracks),
            production_id,
        )
        details = inspect_production(config, production_id, client=client)
        if not _tracks_look_attached(
            details.get("multi_input_files") or [], track_ids
        ):
            raise AuphonicError(
                f"Auphonic production {production_id} did not attach all tracks; "
                f"inspected tracks: {_format_track_choices(details.get('multi_input_files') or [])}"
            )
        update_session_status(
            db_file,
            session_uid=session.session_uid,
            from_state=session.status,
            to_state=SessionStatus.AUPHONIC_PREPARED.value,
            timestamp=timestamp,
            detail="created Auphonic production and attached S3 tracks",
            auphonic_production_id=production_id,
        )
    except Exception as exc:
        _log(logger, "Auphonic prepare failed for %s: %s", session.session_uid, exc)
        update_session_status(
            db_file,
            session_uid=session.session_uid,
            from_state=session.status,
            to_state=SessionStatus.FAILED.value,
            timestamp=datetime.now(timezone.utc).isoformat(),
            detail=f"Auphonic processing failed: {exc}",
            auphonic_production_id=production_id,
            failure_reason=str(exc),
            failed_at_stage="auphonic_prepare",
        )
        raise

    updated = get_session(db_file, session.session_uid)
    if updated is None:
        raise AuphonicError(f"Processed session disappeared: {session.session_uid}")
    return updated


def preview_prepared_session_with_auphonic(
    *,
    config: AppConfig,
    db_file: str,
    session: SessionRecord,
    client: Optional[AuphonicClientProtocol] = None,
    logger: Optional[logging.Logger] = None,
) -> AuphonicPreview:
    if session.status not in {
        SessionStatus.AUPHONIC_PREPARED.value,
        SessionStatus.AUPHONIC_PREVIEWED.value,
    }:
        raise AuphonicError(
            f"Session {session.session_uid} must be auphonic_prepared before preview"
        )
    if not session.auphonic_production_id:
        raise AuphonicError(
            f"Session {session.session_uid} has no stored Auphonic production id"
        )
    if (
        session.auphonic_offsets_verified_production_id
        != session.auphonic_production_id
    ):
        raise AuphonicError(
            f"Session {session.session_uid} offsets have not been verified for "
            f"production {session.auphonic_production_id}; run pod auphonic-offset "
            f"{session.session_uid} --apply first"
        )

    production_id = session.auphonic_production_id
    details = inspect_production(config, production_id, client=client)
    if _is_failed_status(details):
        raise AuphonicError(
            f"Auphonic production {production_id} is failed and cannot be previewed"
        )
    if not details.get("edit_page"):
        raise AuphonicError(
            f"Auphonic production {production_id} did not include an edit page for preview"
        )

    updated = session
    if session.status != SessionStatus.AUPHONIC_PREVIEWED.value:
        update_session_status(
            db_file,
            session_uid=session.session_uid,
            from_state=session.status,
            to_state=SessionStatus.AUPHONIC_PREVIEWED.value,
            timestamp=datetime.now(timezone.utc).isoformat(),
            detail="opened Auphonic edit preview",
            auphonic_production_id=production_id,
        )
        refreshed = get_session(db_file, session.session_uid)
        if refreshed is None:
            raise AuphonicError(
                f"Previewed session disappeared: {session.session_uid}"
            )
        updated = refreshed

    _log(
        logger,
        "Auphonic preview ready for %s production=%s edit_page=%s",
        session.session_uid,
        production_id,
        details.get("edit_page"),
    )
    return AuphonicPreview(session=updated, details=details)


def start_prepared_session_with_auphonic(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session: SessionRecord,
    client: Optional[AuphonicClientProtocol] = None,
    logger: Optional[logging.Logger] = None,
    allow_unpreviewed: bool = False,
) -> SessionRecord:
    allowed_statuses = {SessionStatus.AUPHONIC_PREVIEWED.value}
    if allow_unpreviewed:
        allowed_statuses.add(SessionStatus.AUPHONIC_PREPARED.value)
    if session.status not in allowed_statuses:
        raise AuphonicError(
            f"Session {session.session_uid} must be auphonic_previewed before start"
        )
    if not session.auphonic_production_id:
        raise AuphonicError(
            f"Session {session.session_uid} has no stored Auphonic production id"
        )
    if (
        session.auphonic_offsets_verified_production_id
        != session.auphonic_production_id
    ):
        raise AuphonicError(
            f"Session {session.session_uid} offsets have not been verified for "
            f"production {session.auphonic_production_id}; run pod auphonic-offset "
            f"{session.session_uid} --apply first"
        )

    client = client or AuphonicClient(config)
    production_id = session.auphonic_production_id
    current_details = inspect_production(config, production_id, client=client)
    if (
        _is_done_status(current_details)
        or _is_failed_status(current_details)
        or current_details.get("start_allowed") is not True
    ):
        raise AuphonicError(
            f"Auphonic production {production_id} changed state after preview; "
            f"run pod auphonic-sync --session {session.session_uid}"
        )
    try:
        _validate_verified_offsets_current(
            config=config,
            db_file=db_file,
            session_uid=session.session_uid,
            details=current_details,
        )
    except AuphonicError as exc:
        clear_auphonic_offsets_verified(
            db_file,
            session_uid=session.session_uid,
            status=session.status,
            production_id=production_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            detail=f"invalidated Auphonic offset verification before start: {exc}",
        )
        raise

    try:
        _log(
            logger,
            "Starting Auphonic production %s for %s",
            production_id,
            session.session_uid,
        )
        client.start_production(production_id)
        update_session_status(
            db_file,
            session_uid=session.session_uid,
            from_state=session.status,
            to_state=SessionStatus.AUPHONIC_STARTED.value,
            timestamp=datetime.now(timezone.utc).isoformat(),
            detail="started Auphonic production",
            auphonic_production_id=production_id,
        )

        production = client.wait_for_production(production_id)
        _log(
            logger,
            "Auphonic production %s finished; persisting outputs to S3",
            production_id,
        )
        artifacts = _persist_outputs(
            client=client,
            config=config,
            runtime_paths=runtime_paths,
            db_file=db_file,
            session_uid=session.session_uid,
            production=production,
            logger=logger,
        )
        workspace = create_workspace(
            config=config,
            runtime_paths=runtime_paths,
            db_file=db_file,
            session_uid=session.session_uid,
            expected_auphonic_production_id=production.production_id,
        )

        update_session_status(
            db_file,
            session_uid=session.session_uid,
            from_state=SessionStatus.AUPHONIC_STARTED.value,
            to_state=SessionStatus.WAITING_FOR_FINALIZE.value,
            timestamp=datetime.now(timezone.utc).isoformat(),
            detail=f"persisted Auphonic outputs to S3; workspace at {workspace}",
            auphonic_production_id=production.production_id,
        )
        _log(
            logger,
            "Stored Auphonic outputs for %s master=%s transcript=%s workspace=%s",
            session.session_uid,
            artifacts["master"].s3_uri,
            artifacts["transcript"].s3_uri,
            workspace,
        )
    except Exception as exc:
        _log(logger, "Auphonic start failed for %s: %s", session.session_uid, exc)
        update_session_status(
            db_file,
            session_uid=session.session_uid,
            from_state=session.status,
            to_state=SessionStatus.FAILED.value,
            timestamp=datetime.now(timezone.utc).isoformat(),
            detail=f"Auphonic processing failed: {exc}",
            auphonic_production_id=production_id,
            failure_reason=str(exc),
            failed_at_stage="auphonic_start",
        )
        raise

    updated = get_session(db_file, session.session_uid)
    if updated is None:
        raise AuphonicError(f"Processed session disappeared: {session.session_uid}")
    return updated


def refresh_transcript_with_auphonic(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session: SessionRecord,
    preset_id: Optional[str] = None,
    client: Optional[AuphonicClientProtocol] = None,
    logger: Optional[logging.Logger] = None,
) -> SessionRecord:
    if session.status != SessionStatus.READY_FOR_DRAFT.value:
        raise AuphonicError(
            f"Session {session.session_uid} must be ready_for_draft before transcript refresh"
        )
    validate_auphonic_config(config)
    client = client or AuphonicClient(config)
    production_id = session.transcript_refresh_production_id
    final_master = get_media_artifact(
        db_file,
        session.session_uid,
        kind=ArtifactKind.FINAL_MASTER.value,
    )
    if final_master is None:
        raise AuphonicError(
            f"Session {session.session_uid} has no final master artifact; run pod finalize first"
        )

    if production_id:
        if session.transcript_refresh_source_sha256 != final_master.sha256:
            discard_transcript_refresh_production(
                db_file,
                session_uid=session.session_uid,
                status=session.status,
                production_id=production_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
                detail=(
                    "discarded stale Auphonic transcript refresh production "
                    f"{production_id} after the final master changed"
                ),
            )
            raise AuphonicError(
                f"Session {session.session_uid} final master changed after Auphonic "
                f"transcript refresh production {production_id} was created; the "
                "stale production was discarded, so run transcript-refresh again"
            )
        _log(
            logger,
            "Resuming Auphonic transcript refresh production %s for %s",
            production_id,
            session.session_uid,
        )
        payload = client.get_production(production_id)
        data_node = payload.get("data", payload)
        if _is_failed_status(data_node):
            discard_transcript_refresh_production(
                db_file,
                session_uid=session.session_uid,
                status=session.status,
                production_id=production_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
                detail=f"discarded failed Auphonic transcript refresh production {production_id}",
            )
            raise AuphonicError(
                f"Auphonic transcript refresh production {production_id} failed; run transcript-refresh again to create a replacement"
            )
        if _is_done_status(data_node):
            production = AuphonicProduction(
                production_id=production_id,
                output_files=_extract_output_files(data_node),
            )
        else:
            if data_node.get("start_allowed"):
                client.start_production(production_id)
            production = client.wait_for_production(production_id)
    else:
        resolved_preset_id = resolve_transcript_refresh_preset_id(config, preset_id)
        service_id = resolve_auphonic_service_id(config)
        _log(
            logger,
            "Refreshing transcript for %s from %s",
            session.session_uid,
            final_master.s3_uri,
        )
        production_id = client.create_single_input_production(
            title=f"{session.source_basename or session.session_uid} transcript refresh",
            preset_id=resolved_preset_id,
            service_id=service_id,
            input_key=_artifact_key(config, final_master),
        )
        _log(
            logger,
            "Created Auphonic transcript refresh production %s for %s",
            production_id,
            session.session_uid,
        )
        record_transcript_refresh_production(
            db_file,
            session_uid=session.session_uid,
            status=session.status,
            production_id=production_id,
            final_master_sha256=final_master.sha256,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        client.start_production(production_id)
        production = client.wait_for_production(production_id)

    previous_transcript = get_media_artifact(
        db_file,
        session.session_uid,
        kind=ArtifactKind.TRANSCRIPT.value,
    )
    transcript_artifact = _persist_transcript_output(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        session_uid=session.session_uid,
        production=production,
    )

    try:
        complete_transcript_refresh(
            db_file,
            session_uid=session.session_uid,
            status=session.status,
            production_id=production_id,
            final_master_sha256=final_master.sha256,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    except Exception:
        try:
            restore_media_artifact_after_failed_replacement(
                db_file,
                session_uid=session.session_uid,
                kind=ArtifactKind.TRANSCRIPT.value,
                role="",
                failed_sha256=transcript_artifact.sha256,
                previous=previous_transcript,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as rollback_error:
            raise RuntimeError(
                f"Transcript refresh completion failed for {session.session_uid}, "
                "and the prior transcript could not be restored"
            ) from rollback_error
        raise
    _log(
        logger,
        "Stored refreshed transcript for %s transcript=%s",
        session.session_uid,
        transcript_artifact.s3_uri,
    )

    updated = get_session(db_file, session.session_uid)
    if updated is None:
        raise AuphonicError(
            f"Transcript refreshed session disappeared: {session.session_uid}"
        )
    return updated


def resolve_auphonic_service_id(config: AppConfig) -> str:
    validate_auphonic_config(config)
    service_id = config.auphonic.s3_service_id.strip()
    if not service_id:
        raise AuphonicError(
            "Missing auphonic.s3_service_id in config. Register the Podline bucket "
            "as an Amazon S3 external service in Auphonic and set its UUID."
        )
    return service_id


def _spoken_raw_tracks(
    config: AppConfig, db_file: str, session_uid: str
) -> List[MediaArtifactRecord]:
    artifacts = list_media_artifacts(
        db_file, session_uid, kind=ArtifactKind.RAW_TRACK.value
    )
    tracks = [artifact for artifact in artifacts if artifact.role != "mix"]
    if not tracks:
        raise AuphonicError(
            f"No raw track artifacts registered for session {session_uid}"
        )
    return tracks


def _validate_attached_source_tracks(
    config: AppConfig,
    db_file: str,
    session_uid: str,
    multi_input_files: List[Dict],
) -> None:
    expected_tracks = _spoken_raw_tracks(config, db_file, session_uid)
    attached_tracks = [
        track for track in multi_input_files if track.get("input_file")
    ]
    if len(attached_tracks) != len(expected_tracks):
        raise AuphonicError(
            f"Auphonic production does not have all attached source tracks for "
            f"session {session_uid}: expected {len(expected_tracks)}, found "
            f"{len(attached_tracks)}; inspected tracks: "
            f"{_format_track_choices(multi_input_files)}"
        )
    try:
        for artifact in expected_tracks:
            _find_multi_input_track_for_asset(attached_tracks, artifact)
    except AuphonicError as exc:
        raise AuphonicError(
            f"Auphonic production does not have the expected attached source tracks "
            f"for session {session_uid}: {exc}"
        ) from exc


def _validate_verified_offsets_current(
    *,
    config: AppConfig,
    db_file: str,
    session_uid: str,
    details: Dict,
) -> None:
    artifacts = _spoken_raw_tracks(config, db_file, session_uid)
    durations = {}
    for artifact in artifacts:
        if artifact.duration_seconds is None:
            raise AuphonicError(
                f"Auphonic offsets changed after verification or cannot be "
                f"revalidated for {session_uid}; {artifact.filename} has no stored "
                "duration. Rerun pod auphonic-offset --apply."
            )
        durations[artifact] = Decimal(
            str(
                _validated_duration(
                    artifact.duration_seconds,
                    source=artifact.filename,
                )
            )
        )

    reference_duration = max(durations.values())
    inspected_tracks = details.get("multi_input_files") or []
    for artifact, duration in durations.items():
        track = _find_multi_input_track_for_asset(inspected_tracks, artifact)
        expected_offset = reference_duration - duration
        raw_offset = track.get("offset")
        try:
            actual_offset = (
                Decimal("0")
                if raw_offset in (None, "")
                else Decimal(str(raw_offset))
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise AuphonicError(
                f"Auphonic offsets changed after verification for {session_uid}; "
                f"track {track.get('id') or artifact.filename} has invalid offset "
                f"{raw_offset!r}. Rerun pod auphonic-offset --apply."
            ) from exc
        if (
            not actual_offset.is_finite()
            or abs(actual_offset - expected_offset) > Decimal("0.0005")
        ):
            raise AuphonicError(
                f"Auphonic offsets changed after verification for {session_uid}; "
                f"track {track.get('id') or artifact.filename} expected "
                f"{expected_offset:.3f}s but found {raw_offset or 0!r}. "
                "Rerun pod auphonic-offset --apply and preview again."
            )


def _artifact_key(config: AppConfig, artifact: MediaArtifactRecord) -> str:
    bucket, key = parse_s3_uri(artifact.s3_uri)
    if bucket != config.storage.bucket:
        raise AuphonicError(
            f"Artifact {artifact.kind}/{artifact.role or 'default'} for session "
            f"{artifact.session_uid} lives in s3://{bucket} but the Auphonic S3 "
            f"service is backed by bucket {config.storage.bucket}"
        )
    return key


def resolve_preset_id(config: AppConfig, speaker_count: int) -> str:
    validate_auphonic_config(config)
    key = f"speakers_{speaker_count}"
    preset_id = config.auphonic.preset_ids.get(key, "").strip()
    if not preset_id:
        raise AuphonicError(f"Missing Auphonic preset id for {speaker_count} speakers")
    return preset_id


def resolve_transcript_refresh_preset_id(
    config: AppConfig, preset_id: Optional[str] = None
) -> str:
    validate_auphonic_config(config)
    if preset_id and preset_id.strip():
        return preset_id.strip()

    configured = config.auphonic.preset_ids.get("transcript_refresh", "").strip()
    if configured:
        return configured

    raise AuphonicError(
        "Missing Auphonic preset id for transcript refresh. Set auphonic.preset_ids.transcript_refresh or pass --preset."
    )


def validate_auphonic_config(config: AppConfig) -> None:
    if not config.auphonic.enabled:
        raise AuphonicError("Auphonic is disabled in config.yaml")
    if not config.auphonic.api_key.strip():
        raise AuphonicError("Auphonic api_key is missing in config.yaml")
    if not config.auphonic.base_url.strip():
        raise AuphonicError("Auphonic base_url is missing in config.yaml")


def _persist_outputs(
    *,
    client: AuphonicClientProtocol,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session_uid: str,
    production: AuphonicProduction,
    production_details: Optional[Dict] = None,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, object]:
    staging_dir = Path(runtime_paths.staging_dir) / session_uid
    staging_dir.mkdir(parents=True, exist_ok=True)

    master_output = next(
        (item for item in production.output_files if item.extension == "mp3"),
        None,
    )
    transcript_output = next(
        (item for item in production.output_files if item.extension == "vtt"),
        None,
    )
    if master_output is None or transcript_output is None:
        raise AuphonicError("Auphonic outputs did not include both mp3 and vtt files")

    staged_master = staging_dir / f"{session_uid}_pre_edit.mp3"
    staged_transcript = staging_dir / f"{session_uid}.vtt"
    client.download_file(master_output.download_url, staged_master)
    client.download_file(transcript_output.download_url, staged_transcript)

    master_artifact = store_artifact_file(
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        session_uid=session_uid,
        kind=ArtifactKind.PRE_EDIT_MASTER.value,
        source_path=staged_master,
        content_type="audio/mpeg",
        source_provider="auphonic",
        source_id=production.production_id,
    )
    transcript_artifact = store_artifact_file(
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        session_uid=session_uid,
        kind=ArtifactKind.TRANSCRIPT.value,
        source_path=staged_transcript,
        content_type="text/vtt",
        source_provider="auphonic",
        source_id=production.production_id,
    )
    # The cache holds hardlinks to these staged files; unlink the staging
    # names so a later re-download cannot write through the shared inode.
    staged_master.unlink()
    staged_transcript.unlink()
    result = {"master": master_artifact, "transcript": transcript_artifact}

    tracks_output = next(
        (
            item
            for item in production.output_files
            if item.kind.lower() == "tracks" and item.extension.lower() == "flac.zip"
        ),
        None,
    )
    if tracks_output is not None:
        try:
            details = production_details or inspect_production(
                config, production.production_id, client=client
            )
            stems = _persist_stem_archive(
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                db_file=db_file,
                session_uid=session_uid,
                production_id=production.production_id,
                tracks_output=tracks_output,
                multi_input_files=details.get("multi_input_files") or [],
            )
            result["stems"] = stems
        except Exception as exc:
            existing_stems = list_media_artifacts(
                db_file,
                session_uid,
                kind=ArtifactKind.PRE_EDIT_STEM.value,
            )
            if any(
                stem.source_id == production.production_id
                for stem in existing_stems
            ):
                delete_media_artifacts(
                    db_file,
                    session_uid=session_uid,
                    kind=ArtifactKind.PRE_EDIT_STEM.value,
                )
                disposition = "invalidated stems from the reprocessed production"
            else:
                disposition = "preserved any stems from an older production"
            warning_logger = logger or logging.getLogger("podline")
            warning_logger.warning(
                "Could not persist optional Auphonic stems for %s; %s: %s",
                session_uid,
                disposition,
                exc,
            )
    else:
        # The completed production is authoritative. Do not leave a stem set
        # from an earlier reprocess current when Individual Tracks is absent.
        delete_media_artifacts(
            db_file,
            session_uid=session_uid,
            kind=ArtifactKind.PRE_EDIT_STEM.value,
        )
    return result


def _persist_stem_archive(
    *,
    client: AuphonicClientProtocol,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session_uid: str,
    production_id: str,
    tracks_output: AuphonicOutputFile,
    multi_input_files: List[Dict],
) -> List[MediaArtifactRecord]:
    staging_dir = Path(runtime_paths.staging_dir) / session_uid
    staging_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f"{session_uid}_pre_edit_stems_",
        suffix=".flac.zip",
        dir=staging_dir,
        delete=False,
    ) as temporary_archive:
        archive_path = Path(temporary_archive.name)
    try:
        client.download_file(tracks_output.download_url, archive_path)

        bindings = _stem_track_bindings(
            config=config,
            db_file=db_file,
            session_uid=session_uid,
            multi_input_files=multi_input_files,
        )
        with tempfile.TemporaryDirectory(
            prefix="pre_edit_stems_", dir=staging_dir
        ) as extraction_dir:
            extracted = _extract_stem_archive(
                archive_path=archive_path,
                destination_dir=Path(extraction_dir),
                bindings=bindings,
            )
            files = [
                {
                    "role": item.role,
                    "source_path": item.path,
                    "content_type": "audio/flac",
                    "duration_seconds": probe_audio_duration_from_packets(item.path),
                    "source_provider": "auphonic",
                    "source_id": production_id,
                }
                for item in extracted
            ]
            return store_artifact_files(
                config=config,
                runtime_paths=runtime_paths,
                db_file=db_file,
                session_uid=session_uid,
                kind=ArtifactKind.PRE_EDIT_STEM.value,
                files=files,
                expected_roles=[binding.role for binding in bindings],
            )
    finally:
        archive_path.unlink(missing_ok=True)


def _stem_track_bindings(
    *,
    config: AppConfig,
    db_file: str,
    session_uid: str,
    multi_input_files: List[Dict],
) -> List[StemTrackBinding]:
    raw_tracks = sorted(
        _spoken_raw_tracks(config, db_file, session_uid),
        key=lambda artifact: _recorder_order_key(artifact.role),
    )
    prepared_tracks = [
        item
        for item in multi_input_files
        if str(item.get("type") or "").lower() == "multitrack"
        and item.get("input_file")
    ]
    if len(prepared_tracks) != len(raw_tracks):
        raise AuphonicError(
            "Auphonic prepared track count does not match the session's exact roles"
        )

    bindings = []
    matched_ids = set()
    for artifact in raw_tracks:
        track = _find_multi_input_track_for_asset(prepared_tracks, artifact)
        track_id = str(track.get("id") or "").strip()
        if not track_id:
            raise AuphonicError(
                f"Auphonic track for role {artifact.role} has no stable id"
            )
        normalized_id = _normalize_stem_token(track_id)
        if not normalized_id or normalized_id in matched_ids:
            raise AuphonicError("Auphonic prepared tracks contain duplicate ids")
        matched_ids.add(normalized_id)
        try:
            offset_seconds = float(track.get("offset") or 0)
        except (TypeError, ValueError) as exc:
            raise AuphonicError(
                f"Auphonic track {track_id} has an invalid offset"
            ) from exc
        if offset_seconds < 0:
            raise AuphonicError(
                f"Auphonic track {track_id} has a negative offset"
            )
        bindings.append(
            StemTrackBinding(
                role=artifact.role,
                track_id=track_id,
                input_filename=artifact.filename,
                offset_seconds=offset_seconds,
            )
        )
    return bindings


def _extract_stem_archive(
    *,
    archive_path: Path,
    destination_dir: Path,
    bindings: List[StemTrackBinding],
) -> List[ExtractedStem]:
    """Validate the whole ZIP before extracting any Auphonic stem."""
    expected_by_token = {}
    for binding in bindings:
        tokens = {
            _normalize_stem_token(binding.track_id),
            _normalize_stem_token(binding.role),
            _normalize_stem_token(Path(binding.input_filename).stem),
        }
        for token in {token for token in tokens if token}:
            expected_by_token.setdefault(token, []).append(binding)

    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise AuphonicError(f"Auphonic tracks archive is not a valid ZIP: {exc}") from exc

    with archive:
        members = archive.infolist()
        _validate_stem_archive_expansion(members)
        seen_names = set()
        mapped = {}
        for member in members:
            raw_name = member.filename
            normalized_path = raw_name.replace("\\", "/")
            pure_path = PurePosixPath(normalized_path)
            mode = member.external_attr >> 16
            unsafe = (
                not raw_name
                or pure_path.is_absolute()
                or bool(re.match(r"^[A-Za-z]:", normalized_path))
                or ".." in pure_path.parts
                or stat.S_ISLNK(mode)
                or bool(member.flag_bits & 0x1)
            )
            if unsafe:
                raise AuphonicError(
                    f"Auphonic tracks archive contains unsafe entry: {raw_name!r}"
                )
            normalized_name = normalized_path.casefold()
            if normalized_name in seen_names:
                raise AuphonicError(
                    f"Auphonic tracks archive contains duplicate entry: {raw_name!r}"
                )
            seen_names.add(normalized_name)
            if member.is_dir() or pure_path.suffix.lower() != ".flac":
                raise AuphonicError(
                    f"Auphonic tracks archive contains non-FLAC entry: {raw_name!r}"
                )
            token = _normalize_stem_token(pure_path.stem)
            candidates = expected_by_token.get(token, [])
            if len(candidates) != 1:
                raise AuphonicError(
                    f"Auphonic tracks archive contains extra or unmapped entry: {raw_name!r}"
                )
            binding = candidates[0]
            if binding.role in mapped:
                raise AuphonicError(
                    f"Auphonic tracks archive contains duplicate stem for role {binding.role}"
                )
            mapped[binding.role] = member

        expected_roles = {binding.role for binding in bindings}
        actual_roles = set(mapped)
        if actual_roles != expected_roles:
            raise AuphonicError(
                "Auphonic tracks archive is missing expected roles: "
                f"{sorted(expected_roles - actual_roles)}"
            )

        filenames = {}
        for binding in bindings:
            filename = _safe_stem_filename(binding.track_id)
            folded = filename.casefold()
            if folded in filenames:
                raise AuphonicError(
                    "Auphonic track ids resolve to duplicate stem filenames"
                )
            filenames[folded] = filename

        destination_dir.mkdir(parents=True, exist_ok=True)
        extracted = []
        for binding in bindings:
            target = destination_dir / _safe_stem_filename(binding.track_id)
            with archive.open(mapped[binding.role]) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            extracted.append(
                ExtractedStem(
                    role=binding.role,
                    track_id=binding.track_id,
                    offset_seconds=binding.offset_seconds,
                    path=target,
                )
            )
    return extracted


def _validate_stem_archive_expansion(members: List[zipfile.ZipInfo]) -> None:
    total_expanded_bytes = 0
    for member in members:
        if member.file_size > _MAX_STEM_ENTRY_BYTES:
            raise AuphonicError(
                "Auphonic tracks archive entry exceeds expanded size limit: "
                f"{member.filename!r}"
            )
        total_expanded_bytes += member.file_size
        if total_expanded_bytes > _MAX_STEM_TOTAL_BYTES:
            raise AuphonicError(
                "Auphonic tracks archive total expanded size exceeds limit"
            )
        if member.file_size <= 0:
            continue
        compression_ratio = member.file_size / max(member.compress_size, 1)
        if compression_ratio > _MAX_STEM_COMPRESSION_RATIO:
            raise AuphonicError(
                "Auphonic tracks archive entry has suspicious compression ratio: "
                f"{member.filename!r}"
            )


def _normalize_stem_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def _safe_stem_filename(track_id: str) -> str:
    basename = re.sub(r"[^A-Za-z0-9 _.-]+", "-", track_id).strip(" .")
    if not basename:
        raise AuphonicError("Auphonic track id cannot form a safe stem filename")
    return f"{basename}.flac"


def _persist_transcript_output(
    *,
    client: AuphonicClientProtocol,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session_uid: str,
    production: AuphonicProduction,
) -> MediaArtifactRecord:
    staging_dir = Path(runtime_paths.staging_dir) / session_uid
    staging_dir.mkdir(parents=True, exist_ok=True)

    transcript_output = next(
        (item for item in production.output_files if item.extension == "vtt"),
        None,
    )
    if transcript_output is None:
        raise AuphonicError("Auphonic outputs did not include a vtt transcript")

    staged_transcript = staging_dir / f"{session_uid}.vtt"
    client.download_file(transcript_output.download_url, staged_transcript)

    artifact = store_artifact_file(
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        session_uid=session_uid,
        kind=ArtifactKind.TRANSCRIPT.value,
        source_path=staged_transcript,
        content_type="text/vtt",
        source_provider="auphonic",
        source_id=production.production_id,
    )
    # See _persist_outputs: break the staging hardlink into the cache.
    staged_transcript.unlink()
    return artifact


def _is_done_status(data_node: Dict) -> bool:
    status = str(data_node.get("status") or "").lower()
    status_string = str(data_node.get("status_string") or "").lower()
    return status in {"3", "done", "finished"} or status_string in {
        "done",
        "finished",
        "complete",
    }


def _is_failed_status(data_node: Dict) -> bool:
    status = str(data_node.get("status") or "").lower()
    status_string = str(data_node.get("status_string") or "").lower()
    return status in {"2", "11", "12", "failed", "error"} or status_string in {
        "failed",
        "error",
        "stopped",
        "not enough credits",
    }


def _extract_output_files(data_node: Dict) -> List[AuphonicOutputFile]:
    output_nodes: Iterable[Dict] = (
        data_node.get("output_files") or data_node.get("downloads") or []
    )
    results: List[AuphonicOutputFile] = []
    for output in output_nodes:
        url = output.get("download_url") or output.get("url") or output.get("download")
        extension = str(
            output.get("ending") or output.get("format") or _suffix_from_url(url)
        ).lower()
        if not url or not extension:
            continue
        results.append(
            AuphonicOutputFile(
                kind=str(output.get("format") or extension),
                download_url=str(url),
                extension=extension.lstrip("."),
            )
        )
    return results


def _suffix_from_url(url: Optional[str]) -> str:
    if not url:
        return ""
    return Path(url.split("?", 1)[0]).suffix


def _pair_tracks_with_preset_ids(
    track_ids: List[str], tracks: List["MediaArtifactRecord"]
) -> List[Tuple[str, "MediaArtifactRecord"]]:
    """Decide which artifact attaches to which preset track.

    Preset tracks are named and carry per-track settings, but there is no
    server-side link between a preset track id and a session role. When the
    preset's track ids match the artifact roles exactly (case-insensitive),
    pair by name; otherwise fall back to position — preset order against
    recorder order (Tr1 -> host1, Tr2 -> host2, Tr3 -> guest1, ...). Any
    role-name sort would put guests before hosts.
    """
    if len(track_ids) != len(tracks):
        raise AuphonicError(
            "Auphonic preset track count does not match the session's spoken tracks"
        )
    by_role = {artifact.role.lower(): artifact for artifact in tracks}
    if len(by_role) == len(tracks) and {
        track_id.lower() for track_id in track_ids
    } == set(by_role):
        return [(track_id, by_role[track_id.lower()]) for track_id in track_ids]

    ordered = sorted(tracks, key=lambda artifact: _recorder_order_key(artifact.role))
    return list(zip(track_ids, ordered))


def _recorder_order_key(role: str) -> Tuple[int, int, Tuple[str, int, str]]:
    """Sort roles by recorder track number; unknown roles follow, naturally sorted."""
    number = recorder_track_number(role)
    if number is None:
        return (1, 0, _natural_role_key(role))
    return (0, number, _natural_role_key(role))


def _natural_role_key(role: str) -> Tuple[str, int, str]:
    match = re.fullmatch(r"(.*?)(\d+)", role)
    if match:
        return (match.group(1).lower(), int(match.group(2)), role.lower())
    return (role.lower(), -1, role.lower())


def _tracks_look_attached(
    multi_input_files: List[Dict], expected_track_ids: List[str]
) -> bool:
    attached_ids = {
        str(item.get("id")) for item in multi_input_files if item.get("input_file")
    }
    return set(expected_track_ids).issubset(attached_ids)


def _resolve_preset_track_ids(data_node: Dict, track_count: int) -> List[str]:
    """Track ids defined by the preset on the fresh production, if any.

    Returns [] when the preset defines no tracks (caller falls back to
    artifact roles); a preset with more tracks than the session has spoken
    tracks is a configuration mismatch.
    """
    multi_input_files = (
        data_node.get("multi_input_files") if isinstance(data_node, dict) else None
    )
    if isinstance(multi_input_files, list) and multi_input_files:
        preset_track_ids = [
            str(item.get("id")) for item in multi_input_files if item.get("id")
        ]
        if len(preset_track_ids) == track_count:
            return preset_track_ids
        if len(preset_track_ids) > track_count:
            raise AuphonicError(
                "Auphonic preset track count does not match the scanned speaker count"
            )
    return []


def inspect_production(
    config: AppConfig,
    production_id: str,
    client: Optional[AuphonicClientProtocol] = None,
) -> Dict:
    validate_auphonic_config(config)
    client = client or AuphonicClient(config)
    payload = client.get_production(production_id)
    data_node = payload.get("data", payload)
    return {
        "production_id": data_node.get("uuid") or production_id,
        "status": data_node.get("status"),
        "status_string": data_node.get("status_string"),
        "start_allowed": data_node.get("start_allowed"),
        "change_allowed": data_node.get("change_allowed"),
        "edit_page": data_node.get("edit_page"),
        "status_page": data_node.get("status_page"),
        "error_message": payload.get("error_message")
        or data_node.get("error_message")
        or "",
        "warning_message": data_node.get("warning_message") or "",
        "multi_input_files": data_node.get("multi_input_files") or [],
        "output_files": data_node.get("output_files") or [],
    }


def resolve_stem_workspace_metadata(
    *,
    config: AppConfig,
    db_file: str,
    session: SessionRecord,
    client: Optional[AuphonicClientProtocol] = None,
) -> Dict[str, Dict[str, object]]:
    """Resolve persisted stem roles to the completed Auphonic track state."""
    if not session.auphonic_production_id:
        raise AuphonicError(
            f"Session {session.session_uid} has no stored Auphonic production id"
        )
    stems = list_media_artifacts(
        db_file, session.session_uid, kind=ArtifactKind.PRE_EDIT_STEM.value
    )
    if not stems:
        raise AuphonicError(
            f"Session {session.session_uid} has no persisted Auphonic stems"
        )
    stem_production_ids = {stem.source_id for stem in stems}
    if stem_production_ids != {session.auphonic_production_id}:
        stored_ids = ", ".join(
            sorted(
                str(production_id or "unknown")
                for production_id in stem_production_ids
            )
        )
        raise AuphonicError(
            f"Persisted repair stems belong to Auphonic production {stored_ids}, "
            f"not the newer Auphonic production {session.auphonic_production_id}; "
            "rerun auphonic-sync before creating a stem workspace"
        )
    client = client or AuphonicClient(config)
    details = inspect_production(
        config, session.auphonic_production_id, client=client
    )
    bindings = _stem_track_bindings(
        config=config,
        db_file=db_file,
        session_uid=session.session_uid,
        multi_input_files=details.get("multi_input_files") or [],
    )
    persisted_roles = {stem.role for stem in stems}
    binding_roles = {binding.role for binding in bindings}
    if persisted_roles != binding_roles:
        raise AuphonicError(
            "Persisted Auphonic stem roles do not match the completed production; "
            f"missing={sorted(binding_roles - persisted_roles) or 'none'} "
            f"extra={sorted(persisted_roles - binding_roles) or 'none'}"
        )
    return {
        binding.role: {
            "track_id": binding.track_id,
            "input_filename": binding.input_filename,
            "offset_seconds": binding.offset_seconds,
        }
        for binding in bindings
    }


def probe_audio_duration_from_packets(path: Path) -> float:
    ffprobe_path = shutil.which("ffprobe")
    if ffprobe_path is None:
        raise AuphonicError("ffprobe is required to compute audio duration")
    if not path.exists():
        raise AuphonicError(f"Audio asset does not exist: {path}")

    command = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_packets",
        "-show_entries",
        "packet=pts_time,duration_time",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        stderr_preview = result.stderr[:300] or result.stdout[:300]
        raise AuphonicError(
            f"ffprobe failed while reading audio packets for {path}: {stderr_preview}"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AuphonicError(
            f"ffprobe returned non-JSON packet output for {path}: {result.stdout[:300]}"
        ) from exc

    return _duration_from_ffprobe_packet_data(payload, source=path)


def _load_cozytrack_offset_topology(
    *,
    config: AppConfig,
    session: SessionRecord,
    asset_durations: List[AuphonicAssetDuration],
    cozytrack_client: Optional[CozytrackClientProtocol],
) -> _CozytrackOffsetTopology:
    """Load and validate the Cozytrack identity and timing topology.

    Cozytrack starts every recorder sequentially and stops them together, so
    offsets come from duration shortfalls alone — local channels need them
    too. For sessions with more than two tracks, every registered asset must
    match a Cozytrack track and the topology must contain both host-local and
    remote channels. Host-local assets are retained for display, while track
    creation times independently validate the duration-derived offsets.
    """
    if not session.cozytrack_session_id:
        raise AuphonicError(
            f"Session {session.session_uid} has more than 2 audio assets but no Cozytrack session id; local/remote track topology is required"
        )

    try:
        cozytrack_client = cozytrack_client or CozytrackClient(config)
        cozytrack_session = cozytrack_client.get_session(
            session.cozytrack_session_id
        )
    except CozytrackError as exc:
        raise AuphonicError(
            f"Could not load Cozytrack topology for session {session.cozytrack_session_id}: {exc}"
        ) from exc

    tracks_by_id = {track.id: track for track in cozytrack_session.tracks}
    local_assets = []
    remote_count = 0
    created_at_by_source_id = {}
    for item in asset_durations:
        source_id = (item.asset.source_id or "").strip()
        source_track = tracks_by_id.get(source_id)
        if source_track is None:
            raise AuphonicError(
                f"Could not match registered asset {item.asset.filename} source_id={source_id or 'none'} to Cozytrack session {session.cozytrack_session_id}"
            )
        participant_id = (source_track.participant_id or "").strip()
        if not participant_id:
            raise AuphonicError(
                f"Cozytrack track {source_track.id} for {item.asset.filename} has no participantId; local/remote topology is required for sessions with more than 2 tracks"
            )
        raw_created_at = (source_track.created_at or "").strip()
        if not raw_created_at:
            raise AuphonicError(
                f"Cozytrack track {source_track.id} for {item.asset.filename} has no createdAt; recorder timing is required for sessions with more than 2 tracks"
            )
        try:
            created_at = datetime.fromisoformat(
                raw_created_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise AuphonicError(
                f"Cozytrack track {source_track.id} for {item.asset.filename} has invalid createdAt={raw_created_at!r}"
            ) from exc
        if created_at.tzinfo is None:
            raise AuphonicError(
                f"Cozytrack track {source_track.id} for {item.asset.filename} has timezone-less createdAt={raw_created_at!r}"
            )
        created_at_by_source_id[source_id] = created_at.astimezone(timezone.utc)
        if is_host_participant(participant_id):
            local_assets.append(item)
        else:
            remote_count += 1

    if not local_assets or not remote_count:
        raise AuphonicError(
            f"Session {session.session_uid} must contain at least one host-local and one remote Cozytrack track to compute multitrack offsets; found {len(local_assets)} local and {remote_count} remote"
        )
    return _CozytrackOffsetTopology(
        local_assets=local_assets,
        created_at_by_source_id=created_at_by_source_id,
    )


def _validate_offset_timing_against_created_at(
    *,
    asset_durations: List[AuphonicAssetDuration],
    reference: AuphonicAssetDuration,
    created_at_by_source_id: Dict[str, datetime],
) -> None:
    reference_source_id = (reference.asset.source_id or "").strip()
    reference_created_at = created_at_by_source_id[reference_source_id]
    tolerance = Decimal(str(_OFFSET_CREATED_AT_TOLERANCE_SECONDS))
    for item in asset_durations:
        source_id = (item.asset.source_id or "").strip()
        duration_offset = Decimal(str(reference.duration_seconds)) - Decimal(
            str(item.duration_seconds)
        )
        created_at_offset = Decimal(
            str(
                (
                    created_at_by_source_id[source_id] - reference_created_at
                ).total_seconds()
            )
        )
        difference = abs(duration_offset - created_at_offset)
        if difference > tolerance:
            raise AuphonicError(
                f"Duration-derived offset for {item.asset.filename} "
                f"({duration_offset:.3f}s) disagrees with Cozytrack createdAt "
                f"offset ({created_at_offset:.3f}s) relative to "
                f"{reference.asset.filename}; difference {difference:.3f}s "
                f"exceeds {_OFFSET_CREATED_AT_TOLERANCE_SECONDS:.3f}s "
                "tolerance. Recorder stop times may not match."
            )


def plan_session_auphonic_offset(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session: SessionRecord,
    client: Optional[AuphonicClientProtocol] = None,
    cozytrack_client: Optional[CozytrackClientProtocol] = None,
    duration_probe: DurationProbe = probe_audio_duration_from_packets,
) -> AuphonicOffsetPlan:
    if session.status not in {
        SessionStatus.AUPHONIC_PREPARED.value,
        SessionStatus.AUPHONIC_PREVIEWED.value,
    }:
        raise AuphonicError(
            f"Session {session.session_uid} must be auphonic_prepared or "
            "auphonic_previewed before offset verification"
        )
    tracks = sorted(
        _spoken_raw_tracks(config, db_file, session.session_uid),
        key=lambda artifact: _recorder_order_key(artifact.role),
    )
    if len(tracks) < 2:
        raise AuphonicError(
            f"Session {session.session_uid} must have at least 2 audio assets to compute an Auphonic offset; found {len(tracks)}"
        )
    if not session.auphonic_production_id:
        raise AuphonicError(
            f"Session {session.session_uid} has no stored Auphonic production id"
        )

    asset_durations = [
        AuphonicAssetDuration(
            asset=artifact,
            duration_seconds=_resolve_artifact_duration(
                config=config,
                runtime_paths=runtime_paths,
                db_file=db_file,
                artifact=artifact,
                duration_probe=duration_probe,
            ),
        )
        for artifact in tracks
    ]
    local_assets = []
    created_at_by_source_id = {}
    if len(asset_durations) > 2:
        topology = _load_cozytrack_offset_topology(
            config=config,
            session=session,
            asset_durations=asset_durations,
            cozytrack_client=cozytrack_client,
        )
        local_assets = topology.local_assets
        created_at_by_source_id = topology.created_at_by_source_id

    reference = max(asset_durations, key=lambda item: item.duration_seconds)
    if created_at_by_source_id:
        _validate_offset_timing_against_created_at(
            asset_durations=asset_durations,
            reference=reference,
            created_at_by_source_id=created_at_by_source_id,
        )

    client = client or AuphonicClient(config)
    details = inspect_production(config, session.auphonic_production_id, client=client)
    _validate_offset_production_state(details)
    inspected_tracks = details.get("multi_input_files") or []
    targets = []
    aligned_assets = []
    # Cozytrack starts each track's recorder sequentially and stops them all
    # together, so every shorter track — additional host-local channels
    # included — started late by exactly its duration shortfall against the
    # reference. Only the reference stays at zero.
    for item in asset_durations:
        if item is reference:
            continue
        offset_decimal = Decimal(str(reference.duration_seconds)) - Decimal(
            str(item.duration_seconds)
        )
        if offset_decimal == 0:
            aligned_assets.append(item.asset)
            continue
        target_track = _find_multi_input_track_for_asset(
            inspected_tracks, item.asset
        )
        target_track_id = str(target_track.get("id") or "")
        if not target_track_id:
            raise AuphonicError(
                f"Matched Auphonic track for {item.asset.filename} has no id; inspected tracks: {_format_track_choices(inspected_tracks)}"
            )
        targets.append(
            AuphonicOffsetTarget(
                asset=item.asset,
                track_id=target_track_id,
                input_file=str(target_track.get("input_file") or ""),
                offset_seconds=float(offset_decimal),
            )
        )

    return AuphonicOffsetPlan(
        production_id=session.auphonic_production_id,
        asset_durations=asset_durations,
        local_assets=[item.asset for item in local_assets],
        reference_asset=reference.asset,
        targets=targets,
        aligned_assets=aligned_assets,
    )


def offset_session_auphonic_production(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session: SessionRecord,
    apply: bool = False,
    client: Optional[AuphonicClientProtocol] = None,
    cozytrack_client: Optional[CozytrackClientProtocol] = None,
    duration_probe: DurationProbe = probe_audio_duration_from_packets,
) -> AuphonicOffsetResult:
    client = client or AuphonicClient(config)
    plan = plan_session_auphonic_offset(
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        session=session,
        client=client,
        cozytrack_client=cozytrack_client,
        duration_probe=duration_probe,
    )

    if not apply:
        return AuphonicOffsetResult(plan=plan, applied=False, track_states=[])

    verification_status = begin_auphonic_offset_apply(
        db_file,
        session_uid=session.session_uid,
        status=session.status,
        production_id=plan.production_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    for target in plan.targets:
        client.update_multi_input_file_offset(
            plan.production_id, target.track_id, target.offset_seconds
        )
    details = inspect_production(config, plan.production_id, client=client)
    inspected_tracks = details.get("multi_input_files") or []
    expected_offsets = {
        target.track_id: Decimal(str(target.offset_seconds)) for target in plan.targets
    }
    for asset in [plan.reference_asset, *plan.aligned_assets]:
        track = _find_multi_input_track_for_asset(inspected_tracks, asset)
        track_id = str(track.get("id") or "")
        if not track_id:
            raise AuphonicError(
                f"Matched Auphonic track for {asset.filename} has no id during "
                "offset re-inspection"
            )
        expected_offsets[track_id] = Decimal("0")

    track_states = []
    target_ids = {target.track_id for target in plan.targets}
    for track_id, expected_offset in expected_offsets.items():
        track_state = _find_multi_input_track_by_id(
            inspected_tracks, track_id
        )
        if track_state is None:
            raise AuphonicError(
                f"Auphonic offset was applied but track {track_id} was not present "
                "on re-inspection"
            )
        try:
            raw_offset = track_state.get("offset")
            actual_offset = (
                Decimal("0")
                if raw_offset in (None, "")
                else Decimal(str(raw_offset))
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise AuphonicError(
                f"Auphonic offset for track {track_id} could not be "
                "verified on re-inspection"
            ) from exc
        if abs(actual_offset - expected_offset) > Decimal("0.0005"):
            raise AuphonicError(
                f"Auphonic offset for track {track_id} could not be "
                "verified on re-inspection: expected "
                f"{expected_offset:.3f}s, found {track_state.get('offset')!r}"
            )
        if track_id in target_ids:
            track_states.append(track_state)
    record_auphonic_offsets_verified(
        db_file,
        session_uid=session.session_uid,
        status=verification_status,
        production_id=plan.production_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    return AuphonicOffsetResult(plan=plan, applied=True, track_states=track_states)


_AUPHONIC_RESETTABLE_STATUSES = {
    SessionStatus.AUPHONIC_PREPARED.value,
    SessionStatus.AUPHONIC_PREVIEWED.value,
}


def reset_session_auphonic_production(
    *,
    db_file: str,
    session: SessionRecord,
    logger: Optional[logging.Logger] = None,
) -> SessionRecord:
    """Abandon the prepared Auphonic production and requeue the session.

    Prepare wires preset tracks from roles at prepare time, so a production
    created before a role fix keeps the old wiring; offsets applied before a
    duration fix keep the old alignment. Resetting returns the session to
    queued for a fresh prepare/offset/preview cycle. The abandoned
    production is left untouched on Auphonic; editing states before start
    and failed prepares with an incomplete production are resettable. Once
    outputs are persisted a reset would orphan downstream artifacts.
    """
    failed_prepare = (
        session.status == SessionStatus.FAILED.value
        and session.failed_at_stage == "auphonic_prepare"
        and bool(session.auphonic_production_id)
    )
    if session.status not in _AUPHONIC_RESETTABLE_STATUSES and not failed_prepare:
        raise AuphonicError(
            f"Session {session.session_uid} must be auphonic_prepared or "
            "auphonic_previewed, or a failed auphonic_prepare with a stored "
            f"production to reset; status is {session.status}"
        )
    production_id = session.auphonic_production_id or "none"
    reset_session_to_queued(
        db_file,
        session_uid=session.session_uid,
        from_state=session.status,
        timestamp=datetime.now(timezone.utc).isoformat(),
        detail=f"abandoned Auphonic production {production_id} for rebuild",
    )
    _log(
        logger,
        "Reset %s to queued; abandoned Auphonic production %s",
        session.session_uid,
        production_id,
    )
    refreshed = get_session(db_file, session.session_uid)
    if refreshed is None:
        raise AuphonicError(
            f"Session {session.session_uid} was not found after reset"
        )
    return refreshed


def reconcile_production_to_session(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session: SessionRecord,
    production_id: str,
    client: Optional[AuphonicClientProtocol] = None,
    logger: Optional[logging.Logger] = None,
) -> SessionRecord:
    client = client or AuphonicClient(config)
    details = inspect_production(config, production_id, client=client)
    timestamp = datetime.now(timezone.utc).isoformat()
    current_state = session.status

    _log(
        logger,
        "Reconciling Auphonic production %s into session %s",
        production_id,
        session.session_uid,
    )

    if _is_done_status(details):
        production = AuphonicProduction(
            production_id=production_id,
            output_files=_extract_output_files(
                {"output_files": details["output_files"]}
            ),
        )
        _persist_outputs(
            client=client,
            config=config,
            runtime_paths=runtime_paths,
            db_file=db_file,
            session_uid=session.session_uid,
            production=production,
            production_details=details,
            logger=logger,
        )
        workspace = create_workspace(
            config=config,
            runtime_paths=runtime_paths,
            db_file=db_file,
            session_uid=session.session_uid,
            expected_auphonic_production_id=production_id,
        )
        update_session_status(
            db_file,
            session_uid=session.session_uid,
            from_state=current_state,
            to_state=SessionStatus.WAITING_FOR_FINALIZE.value,
            timestamp=timestamp,
            detail=f"reconciled completed Auphonic production; workspace at {workspace}",
            auphonic_production_id=production_id,
            failure_reason=None,
            failed_at_stage=None,
        )
    elif details.get("start_allowed"):
        try:
            _validate_attached_source_tracks(
                config,
                db_file,
                session.session_uid,
                details.get("multi_input_files") or [],
            )
        except AuphonicError as exc:
            if current_state == SessionStatus.FAILED.value:
                update_session_status(
                    db_file,
                    session_uid=session.session_uid,
                    from_state=current_state,
                    to_state=current_state,
                    timestamp=timestamp,
                    detail=(
                        "reconciled incomplete Auphonic prepare; reset is required"
                    ),
                    auphonic_production_id=production_id,
                    failure_reason=str(exc),
                    failed_at_stage="auphonic_prepare",
                )
            raise
        # Auphonic has no notion of "previewed"; both prepared and previewed
        # productions report start_allowed=True. Preserve the local previewed
        # marker so sync doesn't silently force a re-preview before start.
        reconciled_state = (
            SessionStatus.AUPHONIC_PREVIEWED.value
            if current_state == SessionStatus.AUPHONIC_PREVIEWED.value
            else SessionStatus.AUPHONIC_PREPARED.value
        )
        update_session_status(
            db_file,
            session_uid=session.session_uid,
            from_state=current_state,
            to_state=reconciled_state,
            timestamp=timestamp,
            detail="reconciled prepared Auphonic production",
            auphonic_production_id=production_id,
            failure_reason=None,
            failed_at_stage=None,
        )
    elif _is_failed_status(details):
        update_session_status(
            db_file,
            session_uid=session.session_uid,
            from_state=current_state,
            to_state=SessionStatus.FAILED.value,
            timestamp=timestamp,
            detail=f"reconciled failed Auphonic production: {details.get('error_message') or details.get('status_string')}",
            auphonic_production_id=production_id,
            failure_reason=details.get("error_message") or details.get("status_string"),
            failed_at_stage="auphonic",
        )
    else:
        update_session_status(
            db_file,
            session_uid=session.session_uid,
            from_state=current_state,
            to_state=SessionStatus.AUPHONIC_STARTED.value,
            timestamp=timestamp,
            detail="reconciled in-progress Auphonic production",
            auphonic_production_id=production_id,
            failure_reason=None,
            failed_at_stage=None,
        )

    from podline.repository import get_session

    updated = get_session(db_file, session.session_uid)
    if updated is None:
        raise AuphonicError(f"Reconciled session disappeared: {session.session_uid}")
    return updated


def _duration_from_ffprobe_packet_data(payload: Dict, *, source: Path) -> float:
    packets = payload.get("packets") if isinstance(payload, dict) else None
    if not isinstance(packets, list) or not packets:
        raise AuphonicError(f"ffprobe found no audio packets for {source}")

    last_packet = packets[-1]
    if not isinstance(last_packet, dict):
        raise AuphonicError(f"ffprobe last packet was malformed for {source}")

    pts_time = _parse_packet_time(last_packet.get("pts_time"))
    duration_time = _parse_packet_time(last_packet.get("duration_time"))
    if pts_time is None or duration_time is None:
        raise AuphonicError(
            f"ffprobe could not compute duration for {source}: last audio packet is missing pts_time or duration_time"
        )

    duration = pts_time + duration_time
    if duration <= 0:
        raise AuphonicError(
            f"ffprobe computed a non-positive duration for {source}: {duration}"
        )
    return float(duration)


def _parse_packet_time(value) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _resolve_artifact_duration(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    artifact: MediaArtifactRecord,
    duration_probe: DurationProbe,
) -> float:
    """Return the stored packet-derived duration, probing and persisting once
    for artifacts registered without one."""
    if artifact.duration_seconds is not None:
        return _validated_duration(
            artifact.duration_seconds, source=artifact.filename
        )

    try:
        path = materialize_artifact_record(
            config=config,
            runtime_paths=runtime_paths,
            artifact=artifact,
        )
    except Exception as exc:
        raise AuphonicError(
            f"Could not materialize {artifact.filename} to probe its duration: {exc}"
        ) from exc

    try:
        duration = duration_probe(path)
    except AuphonicError:
        raise
    except Exception as exc:
        raise AuphonicError(f"Could not compute duration for {path}: {exc}") from exc

    validated = _validated_duration(duration, source=str(path))
    update_media_artifact_duration(
        db_file,
        session_uid=artifact.session_uid,
        kind=artifact.kind,
        role=artifact.role,
        duration_seconds=validated,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    return validated


def _validated_duration(duration, *, source: str) -> float:
    try:
        duration_decimal = Decimal(str(duration))
    except (InvalidOperation, ValueError) as exc:
        raise AuphonicError(
            f"Could not compute duration for {source}: duration must be numeric"
        ) from exc
    if not duration_decimal.is_finite() or duration_decimal <= 0:
        raise AuphonicError(
            f"Could not compute duration for {source}: duration must be positive"
        )
    return float(duration_decimal)


def _validate_offset_production_state(details: Dict) -> None:
    production_id = details.get("production_id") or "unknown"
    status_summary = (
        f"status={details.get('status')} status_string={details.get('status_string')}"
    )
    if details.get("change_allowed") is not True:
        raise AuphonicError(
            f"Auphonic production {production_id} is not change_allowed; {status_summary}"
        )
    if _is_done_status(details) or _is_failed_status(details):
        raise AuphonicError(
            f"Auphonic production {production_id} cannot be offset after completion or failure; {status_summary}"
        )
    if details.get("start_allowed") is not True:
        raise AuphonicError(
            f"Auphonic production {production_id} is not start_allowed; it may already be started or processing; {status_summary}"
        )


def _find_multi_input_track_for_asset(
    multi_input_files: List[Dict], asset: MediaArtifactRecord
) -> Dict:
    target_basename = asset.filename.lower()
    matches = [
        track
        for track in multi_input_files
        if _input_file_basename(track.get("input_file")).lower() == target_basename
    ]
    if len(matches) == 1:
        return matches[0]

    inspected = _format_track_choices(multi_input_files)
    if not matches:
        raise AuphonicError(
            f"Could not match Auphonic track for {asset.filename}; inspected tracks: {inspected}"
        )
    raise AuphonicError(
        f"Ambiguous Auphonic track match for {asset.filename}; inspected tracks: {inspected}"
    )


def _find_multi_input_track_by_id(
    multi_input_files: List[Dict], track_id: str
) -> Optional[Dict]:
    for track in multi_input_files:
        if str(track.get("id") or "") == track_id:
            return track
    return None


def _input_file_basename(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    path_value = parsed.path if parsed.scheme or parsed.netloc else text
    return Path(unquote(path_value.split("?", 1)[0])).name


def _format_track_choices(multi_input_files: List[Dict]) -> str:
    if not multi_input_files:
        return "none"
    # input_file is normally a plain S3 key, but redact defensively in case a
    # production carries URL inputs (query strings hold credentials).
    return ", ".join(
        f"id={track.get('id') or 'none'} input_file={redact_url(str(track.get('input_file') or 'none'))}"
        for track in multi_input_files
    )


def _log(logger: Optional[logging.Logger], message: str, *args) -> None:
    if logger is not None:
        logger.info(message, *args)


def _debug_log(logger: Optional[logging.Logger], message: str, *args) -> None:
    if logger is not None:
        logger.debug(message, *args)
