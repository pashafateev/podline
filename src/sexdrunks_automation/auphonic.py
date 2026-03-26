from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
from time import sleep
from typing import Dict, Iterable, List, Optional, Protocol

import httpx

from sexdrunks_automation.config import AppConfig
from sexdrunks_automation.models import (
    AssetRecord,
    RuntimePaths,
    SessionRecord,
    SessionStatus,
)
from sexdrunks_automation.repository import (
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
        logging.getLogger("sexdrunks_automation").debug(
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
        open_handles = []
        files = []
        try:
            for asset, upload_track_id in zip(assets, upload_track_ids):
                handle = open(asset.local_path, "rb")
                open_handles.append(handle)
                files.append(
                    (
                        upload_track_id,
                        (asset.filename, handle, "audio/wav"),
                    )
                )

            response = self._request_with_retries(
                "POST",
                f"/production/{production_id}/upload.json",
                files=files,
            )
            payload = response.json() if response.content else {}
            data_node = payload.get("data", payload)
            logging.getLogger("sexdrunks_automation").debug(
                "Auphonic upload details production_id=%s attached=%s start_allowed=%s multi_input_files=%s",
                production_id,
                _upload_attachment_summary(data_node),
                data_node.get("start_allowed") if isinstance(data_node, dict) else None,
                data_node.get("multi_input_files")
                if isinstance(data_node, dict)
                else None,
            )
            if isinstance(data_node, dict) and not _upload_looks_attached(
                data_node, upload_track_ids
            ):
                raise AuphonicError(
                    f"Auphonic upload for production {production_id} did not attach all tracks"
                )
        finally:
            for handle in open_handles:
                handle.close()

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

            if _is_failed_status(data_node):
                raise AuphonicError(f"Auphonic production {production_id} failed")
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
        with self._client.stream("GET", url) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)

    def _request_with_retries(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_error: Optional[Exception] = None
        for _attempt in range(3):
            try:
                logging.getLogger("sexdrunks_automation").debug(
                    "Auphonic request method=%s url=%s attempt=%s",
                    method,
                    url,
                    _attempt + 1,
                )
                response = self._client.request(method, url, **kwargs)
                logging.getLogger("sexdrunks_automation").debug(
                    "Auphonic response method=%s url=%s status=%s",
                    method,
                    url,
                    response.status_code,
                )
                if response.status_code >= 400:
                    body_preview = response.text[:1000]
                    logging.getLogger("sexdrunks_automation").debug(
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
                logging.getLogger("sexdrunks_automation").debug(
                    "Auphonic retry method=%s url=%s error=%s",
                    method,
                    url,
                    exc,
                )
                sleep(1)
            except (httpx.HTTPError, httpx.TransportError) as exc:
                last_error = exc
                logging.getLogger("sexdrunks_automation").debug(
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

    from sexdrunks_automation.repository import get_session

    updated = get_session(db_file, session.session_uid)
    if updated is None:
        raise AuphonicError(f"Processed session disappeared: {session.session_uid}")
    return updated


def start_prepared_session_with_auphonic(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session: SessionRecord,
    client: Optional[AuphonicClientProtocol] = None,
    logger: Optional[logging.Logger] = None,
) -> SessionRecord:
    if session.status != SessionStatus.AUPHONIC_PREPARED.value:
        raise AuphonicError(
            f"Session {session.session_uid} must be auphonic_prepared before start"
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

    from sexdrunks_automation.repository import get_session

    updated = get_session(db_file, session.session_uid)
    if updated is None:
        raise AuphonicError(f"Processed session disappeared: {session.session_uid}")
    return updated


def resolve_preset_id(config: AppConfig, speaker_count: int) -> str:
    validate_auphonic_config(config)
    key = f"speakers_{speaker_count}"
    preset_id = config.auphonic.preset_ids.get(key, "").strip()
    if not preset_id:
        raise AuphonicError(f"Missing Auphonic preset id for {speaker_count} speakers")
    return preset_id


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
    return status in {"4", "failed", "error"} or status_string in {
        "failed",
        "error",
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
