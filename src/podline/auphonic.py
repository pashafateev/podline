from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
from time import sleep
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Tuple
from urllib.parse import quote, unquote, urlparse

import httpx

from podline.artifacts import (
    create_workspace,
    materialize_artifact_record,
    store_artifact_file,
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
    complete_transcript_refresh,
    discard_transcript_refresh_production,
    get_media_artifact,
    get_session,
    list_media_artifacts,
    record_transcript_refresh_production,
    update_media_artifact_duration,
    update_session_status,
)
from podline.scanner import recorder_track_number
from podline.storage import parse_s3_uri, redact_url


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
    reference_assets: List[MediaArtifactRecord]
    reference_asset: MediaArtifactRecord
    targets: List[AuphonicOffsetTarget]
    aligned_assets: List[MediaArtifactRecord]


@dataclass(frozen=True)
class AuphonicOffsetResult:
    plan: AuphonicOffsetPlan
    applied: bool
    track_states: List[Dict]


DurationProbe = Callable[[Path], float]


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
    prepared = prepare_session_with_auphonic(
        config=config,
        db_file=db_file,
        session=session,
        client=client,
        logger=logger,
    )
    return start_prepared_session_with_auphonic(
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        session=prepared,
        client=client,
        logger=logger,
        allow_unpreviewed=True,
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
            failed_at_stage="auphonic",
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

    client = client or AuphonicClient(config)
    production_id = session.auphonic_production_id

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
        )
        workspace = create_workspace(
            config=config,
            runtime_paths=runtime_paths,
            db_file=db_file,
            session_uid=session.session_uid,
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
            failed_at_stage="auphonic",
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

    if production_id:
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
        final_master = get_media_artifact(
            db_file,
            session.session_uid,
            kind=ArtifactKind.FINAL_MASTER.value,
        )
        if final_master is None:
            raise AuphonicError(
                f"Session {session.session_uid} has no final master artifact; run pod finalize first"
            )

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
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        client.start_production(production_id)
        production = client.wait_for_production(production_id)

    transcript_artifact = _persist_transcript_output(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        session_uid=session.session_uid,
        production=production,
    )

    complete_transcript_refresh(
        db_file,
        session_uid=session.session_uid,
        status=session.status,
        production_id=production_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
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
) -> Dict[str, MediaArtifactRecord]:
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
    return {"master": master_artifact, "transcript": transcript_artifact}


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


def _partition_cozytrack_offset_tracks(
    *,
    config: AppConfig,
    session: SessionRecord,
    asset_durations: List[AuphonicAssetDuration],
    cozytrack_client: Optional[CozytrackClientProtocol],
) -> Tuple[List[AuphonicAssetDuration], List[AuphonicAssetDuration]]:
    """Split host-local tracks from remote tracks using Cozytrack identity.

    Cozytrack starts every recorder sequentially and stops them together, so
    offsets come from duration shortfalls alone — local channels need them
    too. Topology still matters for sessions with more than two tracks: the
    alignment reference must be a host-local channel (the host machine
    records from session start), and a remote track outlasting every local
    channel means the registered assets do not match the Cozytrack session.
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
    local_durations = []
    remote_durations = []
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
        if is_host_participant(participant_id):
            local_durations.append(item)
        else:
            remote_durations.append(item)

    if not local_durations or not remote_durations:
        raise AuphonicError(
            f"Session {session.session_uid} must contain at least one host-local and one remote Cozytrack track to compute multitrack offsets; found {len(local_durations)} local and {len(remote_durations)} remote"
        )
    return local_durations, remote_durations


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
    if len(asset_durations) == 2:
        # Two-track sessions may have no Cozytrack topology (e.g. pod
        # import); with a shared stop time the longer track started first
        # and anchors the alignment.
        sorted_durations = sorted(
            asset_durations, key=lambda item: item.duration_seconds
        )
        local_durations = [sorted_durations[-1]]
    else:
        local_durations, _remote_durations = _partition_cozytrack_offset_tracks(
            config=config,
            session=session,
            asset_durations=asset_durations,
            cozytrack_client=cozytrack_client,
        )

    reference = max(local_durations, key=lambda item: item.duration_seconds)

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
        if offset_decimal < 0:
            raise AuphonicError(
                f"Asset {item.asset.filename} is not shorter than the local reference {reference.asset.filename}; no positive Auphonic offset can be computed"
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
        reference_assets=[item.asset for item in local_durations],
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

    for target in plan.targets:
        client.update_multi_input_file_offset(
            plan.production_id, target.track_id, target.offset_seconds
        )
    details = inspect_production(config, plan.production_id, client=client)
    track_states = []
    for target in plan.targets:
        track_state = _find_multi_input_track_by_id(
            details.get("multi_input_files") or [], target.track_id
        )
        if track_state is None:
            raise AuphonicError(
                f"Auphonic offset was applied but updated track {target.track_id} was not present on re-inspection"
            )
        track_states.append(track_state)
    return AuphonicOffsetResult(plan=plan, applied=True, track_states=track_states)


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
        )
        workspace = create_workspace(
            config=config,
            runtime_paths=runtime_paths,
            db_file=db_file,
            session_uid=session.session_uid,
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
