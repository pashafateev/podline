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


class AuphonicClientProtocol(Protocol):
    def create_production(
        self, *, session: SessionRecord, assets: List[AssetRecord], preset_id: str
    ) -> str: ...

    def upload_assets(self, production_id: str, assets: List[AssetRecord]) -> None: ...

    def start_production(self, production_id: str) -> None: ...

    def wait_for_production(self, production_id: str) -> AuphonicProduction: ...

    def download_file(self, url: str, destination: Path) -> None: ...


class AuphonicClient:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._client = httpx.Client(
            base_url=config.auphonic.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {config.auphonic.api_key}"},
            timeout=60.0,
        )

    def create_production(
        self,
        *,
        session: SessionRecord,
        assets: List[AssetRecord],
        preset_id: str,
    ) -> str:
        payload = {
            "preset": preset_id,
            "metadata": {"title": session.source_basename or session.session_uid},
        }
        if len(assets) > 1:
            payload["is_multitrack"] = True
            payload["multi_input_files"] = [
                {"type": "multitrack", "id": _asset_upload_key(asset)}
                for asset in assets
            ]

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
        return str(production_id)

    def upload_assets(self, production_id: str, assets: List[AssetRecord]) -> None:
        open_handles = []
        files = []
        try:
            for asset in assets:
                handle = open(asset.local_path, "rb")
                open_handles.append(handle)
                files.append(
                    (
                        _asset_upload_key(asset),
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
            if isinstance(data_node, dict) and not _upload_looks_attached(
                data_node, assets
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
                response = self._client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.HTTPError, httpx.TransportError) as exc:
                last_error = exc
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
        production_id = client.create_production(
            session=session,
            assets=assets,
            preset_id=preset_id,
        )
        _log(
            logger,
            "Created Auphonic production %s for %s",
            production_id,
            session.session_uid,
        )
        client.upload_assets(production_id, assets)
        _log(
            logger,
            "Uploaded %s assets to Auphonic production %s",
            len(assets),
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


def _upload_looks_attached(data_node: Dict, assets: List[AssetRecord]) -> bool:
    multi_input_files = data_node.get("multi_input_files")
    if isinstance(multi_input_files, list) and multi_input_files:
        attached_ids = {
            str(item.get("id")) for item in multi_input_files if item.get("input_file")
        }
        expected_ids = {_asset_upload_key(asset) for asset in assets}
        return expected_ids.issubset(attached_ids)

    input_file = data_node.get("input_file")
    return len(assets) == 1 and bool(input_file)


def _log(logger: Optional[logging.Logger], message: str, *args) -> None:
    if logger is not None:
        logger.info(message, *args)
