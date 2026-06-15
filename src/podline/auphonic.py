from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
from pathlib import Path
import shutil
import subprocess
from time import sleep
from typing import Callable, Dict, Iterable, List, Optional, Protocol
from urllib.parse import quote, unquote, urlparse

import httpx

from podline.config import AppConfig
from podline.models import (
    AssetRecord,
    RuntimePaths,
    SessionRecord,
    SessionStatus,
)
from podline.repository import (
    get_session,
    list_assets_for_session,
    update_session_status,
)


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
    asset: AssetRecord
    duration_seconds: float


@dataclass(frozen=True)
class AuphonicOffsetPlan:
    production_id: str
    asset_durations: List[AuphonicAssetDuration]
    target_asset: AssetRecord
    longer_asset: AssetRecord
    target_track_id: str
    target_input_file: str
    offset_seconds: float


@dataclass(frozen=True)
class AuphonicOffsetResult:
    plan: AuphonicOffsetPlan
    applied: bool
    track_state: Optional[Dict]


DurationProbe = Callable[[Path], float]


class AuphonicClientProtocol(Protocol):
    def create_production(
        self, *, session: SessionRecord, assets: List[AssetRecord], preset_id: str
    ) -> AuphonicPreparedProduction: ...

    def upload_assets(
        self, production_id: str, assets: List[AssetRecord], upload_track_ids: List[str]
    ) -> None: ...

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
        session: SessionRecord,
        assets: List[AssetRecord],
        preset_id: str,
    ) -> AuphonicPreparedProduction:
        payload = {
            "preset": preset_id,
            "metadata": {"title": session.source_basename or session.session_uid},
        }
        if len(assets) > 1:
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
        upload_track_ids = _resolve_upload_track_ids(data_node, assets)
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

    def upload_assets(
        self, production_id: str, assets: List[AssetRecord], upload_track_ids: List[str]
    ) -> None:
        payload = self._upload_assets_with_curl(
            production_id=production_id,
            assets=assets,
            upload_track_ids=upload_track_ids,
        )
        data_node = payload.get("data", payload)
        logging.getLogger("podline").debug(
            "Auphonic upload details production_id=%s attached=%s start_allowed=%s multi_input_files=%s",
            production_id,
            _upload_attachment_summary(data_node),
            data_node.get("start_allowed") if isinstance(data_node, dict) else None,
            data_node.get("multi_input_files") if isinstance(data_node, dict) else None,
        )
        if isinstance(data_node, dict) and not _upload_looks_attached(
            data_node, upload_track_ids
        ):
            raise AuphonicError(
                f"Auphonic upload for production {production_id} did not attach all tracks"
            )

    def _upload_assets_with_curl(
        self,
        *,
        production_id: str,
        assets: List[AssetRecord],
        upload_track_ids: List[str],
    ) -> Dict:
        curl_binary = _resolve_curl_binary()
        command = [
            curl_binary,
            "-sS",
            "--http1.1",
            "--tlsv1.2",
            "--no-alpn",
            "--limit-rate",
            "8M",
            "--connect-timeout",
            "30",
            "-X",
            "POST",
            f"{self._config.auphonic.base_url.rstrip('/')}/production/{production_id}/upload.json?bearer_token={self._config.auphonic.api_key}",
            "--max-time",
            str(self._config.auphonic.request_timeout_seconds),
        ]
        for asset, upload_track_id in zip(assets, upload_track_ids):
            content_type = _audio_content_type_for(Path(asset.local_path))
            command.extend(
                [
                    "-F",
                    f"{upload_track_id}=@{asset.local_path};type={content_type}",
                ]
            )

        logging.getLogger("podline").debug(
            "Auphonic curl upload command production_id=%s curl=%s track_ids=%s",
            production_id,
            curl_binary,
            upload_track_ids,
        )
        result = None
        success_attempt = None
        for attempt in range(3):
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode == 0:
                success_attempt = attempt + 1
                break

            stderr_preview = result.stderr[:300]
            logging.getLogger("podline").debug(
                "Auphonic curl upload transient failure production_id=%s attempt=%s exit_code=%s will_retry=%s stderr=%s",
                production_id,
                attempt + 1,
                result.returncode,
                attempt < 2,
                stderr_preview,
            )
            if result.returncode not in {55, 56}:
                raise AuphonicError(
                    f"Auphonic curl upload failed with exit code {result.returncode}: {stderr_preview}"
                )
            if attempt < 2:
                sleep(5)

        if result is None or result.returncode != 0:
            raise AuphonicError(
                f"Auphonic curl upload failed with exit code {result.returncode if result else 'unknown'}: {(result.stderr[:300] if result else 'no stderr')}"
            )
        if success_attempt and success_attempt > 1:
            logging.getLogger("podline").debug(
                "Auphonic curl upload recovered after retry production_id=%s success_attempt=%s",
                production_id,
                success_attempt,
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AuphonicError(
                f"Auphonic curl upload returned non-JSON response: {result.stdout[:300]}"
            ) from exc

        if payload.get("status_code", 200) >= 400:
            raise AuphonicError(
                f"Auphonic upload failed with status {payload.get('status_code')}: {payload.get('error_message') or payload.get('form_errors') or payload}"
            )
        return payload

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

    assets = list_assets_for_session(db_file, session.session_uid)
    if not assets:
        raise AuphonicError(f"No assets found for session {session.session_uid}")

    preset_id = resolve_preset_id(config, len(assets))
    client = client or AuphonicClient(config)
    timestamp = datetime.now(timezone.utc).isoformat()
    production_id = None

    try:
        _log(logger, "Preparing Auphonic production for %s", session.session_uid)
        _debug_log(
            logger,
            "Auphonic preset=%s timeout=%ss track_ids=%s asset_paths=%s",
            preset_id,
            config.auphonic.request_timeout_seconds,
            [_asset_upload_key(asset) for asset in assets],
            [asset.local_path for asset in assets],
        )
        prepared_production = client.create_production(
            session=session,
            assets=assets,
            preset_id=preset_id,
        )
        production_id = prepared_production.production_id
        _log(
            logger,
            "Created Auphonic production %s for %s",
            production_id,
            session.session_uid,
        )
        _debug_log(
            logger,
            "Using Auphonic upload track ids=%s",
            prepared_production.upload_track_ids,
        )
        client.upload_assets(
            production_id, assets, prepared_production.upload_track_ids
        )
        _log(
            logger,
            "Uploaded %s assets to Auphonic production %s",
            len(assets),
            production_id,
        )
        _debug_log(
            logger,
            "Uploaded track ids=%s to production %s",
            prepared_production.upload_track_ids,
            production_id,
        )
        update_session_status(
            db_file,
            session_uid=session.session_uid,
            from_state=session.status,
            to_state=SessionStatus.AUPHONIC_PREPARED.value,
            timestamp=timestamp,
            detail="created Auphonic production and uploaded assets",
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
            "Auphonic production %s finished; downloading outputs",
            production_id,
        )
        paths = _download_and_copy_outputs(
            client=client,
            runtime_paths=runtime_paths,
            deliverables_root=config.paths.deliverables_root,
            session_uid=session.session_uid,
            production=production,
        )

        update_session_status(
            db_file,
            session_uid=session.session_uid,
            from_state=SessionStatus.AUPHONIC_STARTED.value,
            to_state=SessionStatus.WAITING_FOR_FINALIZE.value,
            timestamp=datetime.now(timezone.utc).isoformat(),
            detail="downloaded Auphonic outputs and copied deliverables",
            auphonic_production_id=production.production_id,
            pre_edit_master_path=str(paths["master"]),
            transcript_path=str(paths["transcript"]),
        )
        _log(
            logger,
            "Stored Auphonic outputs for %s master=%s transcript=%s",
            session.session_uid,
            paths["master"],
            paths["transcript"],
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
    if not session.final_master_path:
        raise AuphonicError(f"Session {session.session_uid} has no final master path")

    final_master = Path(session.final_master_path)
    if not final_master.exists():
        raise AuphonicError(f"Final master file not found: {final_master}")

    resolved_preset_id = resolve_transcript_refresh_preset_id(config, preset_id)
    asset = AssetRecord(
        role="final_master",
        filename=final_master.name,
        file_size=final_master.stat().st_size,
        local_path=str(final_master),
    )
    client = client or AuphonicClient(config)

    _log(
        logger,
        "Refreshing transcript for %s from %s",
        session.session_uid,
        final_master,
    )
    prepared_production = client.create_production(
        session=session,
        assets=[asset],
        preset_id=resolved_preset_id,
    )
    production_id = prepared_production.production_id
    _log(
        logger,
        "Created Auphonic transcript refresh production %s for %s",
        production_id,
        session.session_uid,
    )
    client.upload_assets(production_id, [asset], prepared_production.upload_track_ids)
    _log(logger, "Uploaded final master to Auphonic production %s", production_id)
    client.start_production(production_id)
    production = client.wait_for_production(production_id)
    transcript_path = _download_and_copy_transcript_output(
        client=client,
        runtime_paths=runtime_paths,
        deliverables_root=config.paths.deliverables_root,
        session_uid=session.session_uid,
        production=production,
    )

    update_session_status(
        db_file,
        session_uid=session.session_uid,
        from_state=session.status,
        to_state=session.status,
        timestamp=datetime.now(timezone.utc).isoformat(),
        detail=f"refreshed transcript from final master using Auphonic production {production_id}",
        transcript_path=str(transcript_path),
        failure_reason=None,
        failed_at_stage=None,
    )
    _log(
        logger,
        "Stored refreshed transcript for %s transcript=%s",
        session.session_uid,
        transcript_path,
    )

    updated = get_session(db_file, session.session_uid)
    if updated is None:
        raise AuphonicError(
            f"Transcript refreshed session disappeared: {session.session_uid}"
        )
    return updated


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


def _download_and_copy_outputs(
    *,
    client: AuphonicClientProtocol,
    runtime_paths: RuntimePaths,
    deliverables_root: Path,
    session_uid: str,
    production: AuphonicProduction,
) -> Dict[str, Path]:
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

    staged_master = staging_dir / "pre_edit_master.mp3"
    staged_transcript = staging_dir / "transcript.vtt"
    client.download_file(master_output.download_url, staged_master)
    client.download_file(transcript_output.download_url, staged_transcript)

    deliverables_staging_dir = deliverables_root / "staging"
    deliverables_transcripts_dir = deliverables_root / "transcripts"
    deliverables_staging_dir.mkdir(parents=True, exist_ok=True)
    deliverables_transcripts_dir.mkdir(parents=True, exist_ok=True)

    master_target = deliverables_staging_dir / f"{session_uid}_pre_edit.mp3"
    transcript_target = deliverables_transcripts_dir / f"{session_uid}.vtt"
    master_target.write_bytes(staged_master.read_bytes())
    transcript_target.write_bytes(staged_transcript.read_bytes())

    return {"master": master_target, "transcript": transcript_target}


def _download_and_copy_transcript_output(
    *,
    client: AuphonicClientProtocol,
    runtime_paths: RuntimePaths,
    deliverables_root: Path,
    session_uid: str,
    production: AuphonicProduction,
) -> Path:
    staging_dir = Path(runtime_paths.staging_dir) / session_uid
    staging_dir.mkdir(parents=True, exist_ok=True)

    transcript_output = next(
        (item for item in production.output_files if item.extension == "vtt"),
        None,
    )
    if transcript_output is None:
        raise AuphonicError("Auphonic outputs did not include a vtt transcript")

    staged_transcript = staging_dir / "transcript_refresh.vtt"
    client.download_file(transcript_output.download_url, staged_transcript)

    deliverables_transcripts_dir = deliverables_root / "transcripts"
    deliverables_transcripts_dir.mkdir(parents=True, exist_ok=True)
    transcript_target = deliverables_transcripts_dir / f"{session_uid}.vtt"
    transcript_target.write_bytes(staged_transcript.read_bytes())
    return transcript_target


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


def _asset_upload_key(asset: AssetRecord) -> str:
    return asset.role or Path(asset.filename).stem


def _upload_looks_attached(data_node: Dict, upload_track_ids: List[str]) -> bool:
    multi_input_files = data_node.get("multi_input_files")
    if isinstance(multi_input_files, list) and multi_input_files:
        attached_ids = {
            str(item.get("id")) for item in multi_input_files if item.get("input_file")
        }
        expected_ids = set(upload_track_ids)
        return expected_ids.issubset(attached_ids)

    input_file = data_node.get("input_file")
    return len(upload_track_ids) == 1 and bool(input_file)


def _resolve_upload_track_ids(data_node: Dict, assets: List[AssetRecord]) -> List[str]:
    multi_input_files = (
        data_node.get("multi_input_files") if isinstance(data_node, dict) else None
    )
    if isinstance(multi_input_files, list) and multi_input_files:
        preset_track_ids = [
            str(item.get("id")) for item in multi_input_files if item.get("id")
        ]
        if len(preset_track_ids) == len(assets):
            return preset_track_ids
        if len(preset_track_ids) > len(assets):
            raise AuphonicError(
                "Auphonic preset track count does not match the scanned speaker count"
            )

    return [_asset_upload_key(asset) for asset in assets]


def _resolve_curl_binary() -> str:
    for candidate in (
        "/opt/homebrew/opt/curl/bin/curl",
        "/usr/local/opt/curl/bin/curl",
    ):
        if Path(candidate).exists():
            return candidate

    resolved = shutil.which("curl")
    if resolved is None:
        raise AuphonicError("curl is required for Auphonic uploads but was not found")
    return resolved


def _audio_content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".m4a":
        return "audio/mp4"
    if suffix == ".webm":
        return "audio/webm"
    if suffix == ".opus":
        return "audio/ogg"
    if suffix in {".aif", ".aiff"}:
        return "audio/aiff"
    return "application/octet-stream"


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


def plan_session_auphonic_offset(
    *,
    config: AppConfig,
    db_file: str,
    session: SessionRecord,
    client: Optional[AuphonicClientProtocol] = None,
    duration_probe: DurationProbe = probe_audio_duration_from_packets,
) -> AuphonicOffsetPlan:
    assets = list_assets_for_session(db_file, session.session_uid)
    if len(assets) != 2:
        raise AuphonicError(
            f"Session {session.session_uid} must have exactly 2 audio assets to compute an Auphonic offset; found {len(assets)}"
        )
    if not session.auphonic_production_id:
        raise AuphonicError(
            f"Session {session.session_uid} has no stored Auphonic production id"
        )

    asset_durations = [
        AuphonicAssetDuration(
            asset=asset,
            duration_seconds=_probe_asset_duration(asset, duration_probe),
        )
        for asset in assets
    ]
    sorted_durations = sorted(asset_durations, key=lambda item: item.duration_seconds)
    shorter = sorted_durations[0]
    longer = sorted_durations[-1]
    offset_decimal = Decimal(str(longer.duration_seconds)) - Decimal(
        str(shorter.duration_seconds)
    )
    if offset_decimal <= 0:
        raise AuphonicError(
            f"Session {session.session_uid} assets have equal durations; no positive Auphonic offset can be computed"
        )

    client = client or AuphonicClient(config)
    details = inspect_production(config, session.auphonic_production_id, client=client)
    _validate_offset_production_state(details)
    target_track = _find_multi_input_track_for_asset(
        details.get("multi_input_files") or [], shorter.asset
    )
    target_track_id = str(target_track.get("id") or "")
    if not target_track_id:
        raise AuphonicError(
            f"Matched Auphonic track for {shorter.asset.filename} has no id; inspected tracks: {_format_track_choices(details.get('multi_input_files') or [])}"
        )

    return AuphonicOffsetPlan(
        production_id=session.auphonic_production_id,
        asset_durations=asset_durations,
        target_asset=shorter.asset,
        longer_asset=longer.asset,
        target_track_id=target_track_id,
        target_input_file=str(target_track.get("input_file") or ""),
        offset_seconds=float(offset_decimal),
    )


def offset_session_auphonic_production(
    *,
    config: AppConfig,
    db_file: str,
    session: SessionRecord,
    apply: bool = False,
    client: Optional[AuphonicClientProtocol] = None,
    duration_probe: DurationProbe = probe_audio_duration_from_packets,
) -> AuphonicOffsetResult:
    client = client or AuphonicClient(config)
    plan = plan_session_auphonic_offset(
        config=config,
        db_file=db_file,
        session=session,
        client=client,
        duration_probe=duration_probe,
    )

    if not apply:
        return AuphonicOffsetResult(plan=plan, applied=False, track_state=None)

    client.update_multi_input_file_offset(
        plan.production_id, plan.target_track_id, plan.offset_seconds
    )
    details = inspect_production(config, plan.production_id, client=client)
    track_state = _find_multi_input_track_by_id(
        details.get("multi_input_files") or [], plan.target_track_id
    )
    if track_state is None:
        raise AuphonicError(
            f"Auphonic offset was applied but updated track {plan.target_track_id} was not present on re-inspection"
        )
    return AuphonicOffsetResult(plan=plan, applied=True, track_state=track_state)


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
        paths = _download_and_copy_outputs(
            client=client,
            runtime_paths=runtime_paths,
            deliverables_root=config.paths.deliverables_root,
            session_uid=session.session_uid,
            production=production,
        )
        update_session_status(
            db_file,
            session_uid=session.session_uid,
            from_state=current_state,
            to_state=SessionStatus.WAITING_FOR_FINALIZE.value,
            timestamp=timestamp,
            detail="reconciled completed Auphonic production and downloaded outputs",
            auphonic_production_id=production_id,
            pre_edit_master_path=str(paths["master"]),
            transcript_path=str(paths["transcript"]),
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


def _probe_asset_duration(asset: AssetRecord, duration_probe: DurationProbe) -> float:
    path = Path(asset.local_path)
    try:
        duration = duration_probe(path)
    except AuphonicError:
        raise
    except Exception as exc:
        raise AuphonicError(f"Could not compute duration for {path}: {exc}") from exc

    try:
        duration_decimal = Decimal(str(duration))
    except (InvalidOperation, ValueError) as exc:
        raise AuphonicError(
            f"Could not compute duration for {path}: duration must be numeric"
        ) from exc
    if not duration_decimal.is_finite() or duration_decimal <= 0:
        raise AuphonicError(
            f"Could not compute duration for {path}: duration must be positive"
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
    multi_input_files: List[Dict], asset: AssetRecord
) -> Dict:
    target_basenames = {
        Path(asset.local_path).name.lower(),
        asset.filename.lower(),
    }
    matches = [
        track
        for track in multi_input_files
        if _input_file_basename(track.get("input_file")).lower() in target_basenames
    ]
    if len(matches) == 1:
        return matches[0]

    target_name = Path(asset.local_path).name or asset.filename
    inspected = _format_track_choices(multi_input_files)
    if not matches:
        raise AuphonicError(
            f"Could not match Auphonic track for {target_name}; inspected tracks: {inspected}"
        )
    raise AuphonicError(
        f"Ambiguous Auphonic track match for {target_name}; inspected tracks: {inspected}"
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
    return ", ".join(
        f"id={track.get('id') or 'none'} input_file={track.get('input_file') or 'none'}"
        for track in multi_input_files
    )


def _upload_attachment_summary(data_node: Dict) -> Dict[str, object]:
    if not isinstance(data_node, dict):
        return {}

    multi_input_files = data_node.get("multi_input_files")
    if isinstance(multi_input_files, list):
        return {
            "multi_input_ids": [item.get("id") for item in multi_input_files],
            "attached_inputs": [
                item.get("input_file")
                for item in multi_input_files
                if item.get("input_file")
            ],
        }

    return {"input_file": data_node.get("input_file")}


def _log(logger: Optional[logging.Logger], message: str, *args) -> None:
    if logger is not None:
        logger.info(message, *args)


def _debug_log(logger: Optional[logging.Logger], message: str, *args) -> None:
    if logger is not None:
        logger.debug(message, *args)
