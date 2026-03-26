from pathlib import Path

from sexdrunks_automation.auphonic import (
    AuphonicError,
    AuphonicOutputFile,
    AuphonicProduction,
    prepare_session_with_auphonic,
    process_session_with_auphonic,
    resolve_preset_id,
    start_prepared_session_with_auphonic,
)
from sexdrunks_automation.config import (
    AppConfig,
    AuphonicConfig,
    NotificationsConfig,
    PathsConfig,
    PodbeanConfig,
    PollingConfig,
)
from sexdrunks_automation.db import initialize_database
from sexdrunks_automation.ingest import scan_and_ingest
from sexdrunks_automation.paths import build_runtime_paths, ensure_runtime_dirs
from sexdrunks_automation.repository import get_session, list_sessions


class FakeAuphonicClient:
    def __init__(self) -> None:
        self.created = []
        self.uploaded = []
        self.started = []
        self.downloads = []

    def create_production(self, *, session, assets, preset_id: str) -> str:
        self.created.append((session.session_uid, len(assets), preset_id))
        return "prod-123"

    def upload_assets(self, production_id: str, assets) -> None:
        self.uploaded.append((production_id, len(assets)))

    def start_production(self, production_id: str) -> None:
        self.started.append(production_id)

    def wait_for_production(self, production_id: str) -> AuphonicProduction:
        assert production_id == "prod-123"
        return AuphonicProduction(
            production_id=production_id,
            output_files=[
                AuphonicOutputFile(
                    kind="mp3",
                    download_url="https://example.test/master.mp3",
                    extension="mp3",
                ),
                AuphonicOutputFile(
                    kind="vtt",
                    download_url="https://example.test/transcript.vtt",
                    extension="vtt",
                ),
            ],
        )

    def download_file(self, url: str, destination: Path) -> None:
        self.downloads.append((url, destination))
        if url.endswith(".mp3"):
            destination.write_bytes(b"fake-mp3")
        else:
            destination.write_text(
                "WEBVTT\n\n00:00.000 --> 00:01.000\nHello\n", encoding="utf-8"
            )


def test_resolve_preset_id_validates_config() -> None:
    config = _build_config(Path("/tmp/runtime"), enabled=False)

    try:
        resolve_preset_id(config, 2)
    except AuphonicError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("Expected AuphonicError when config is disabled")


def test_process_session_with_auphonic_downloads_outputs(
    fixture_raw_root, tmp_path: Path
) -> None:
    config = _build_config(tmp_path)
    runtime_paths = build_runtime_paths(config)
    ensure_runtime_dirs(runtime_paths)
    initialize_database(runtime_paths.db_file)

    outcome = scan_and_ingest(fixture_raw_root, runtime_paths.db_file)
    assert outcome.created == 2
    session = next(
        item
        for item in list_sessions(runtime_paths.db_file)
        if item.source_basename == "010124_013143"
    )
    client = FakeAuphonicClient()

    updated = process_session_with_auphonic(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=session,
        client=client,
    )

    assert updated.status == "waiting_for_finalize"
    assert updated.auphonic_production_id == "prod-123"
    assert updated.pre_edit_master_path is not None
    assert updated.transcript_path is not None
    assert Path(updated.pre_edit_master_path).exists()
    assert Path(updated.transcript_path).exists()
    assert client.created[0][1] == 2
    assert client.created[0][2] == "preset-2"
    assert client.uploaded[0][1] == 2
    assert client.started == ["prod-123"]

    reloaded = get_session(runtime_paths.db_file, session.session_uid)
    assert reloaded is not None
    assert reloaded.status == "waiting_for_finalize"


def test_prepare_then_start_splits_the_auphonic_flow(
    fixture_raw_root, tmp_path: Path
) -> None:
    config = _build_config(tmp_path)
    runtime_paths = build_runtime_paths(config)
    ensure_runtime_dirs(runtime_paths)
    initialize_database(runtime_paths.db_file)

    scan_and_ingest(fixture_raw_root, runtime_paths.db_file)
    session = next(
        item
        for item in list_sessions(runtime_paths.db_file)
        if item.source_basename == "010124_013143"
    )
    client = FakeAuphonicClient()

    prepared = prepare_session_with_auphonic(
        config=config,
        db_file=runtime_paths.db_file,
        session=session,
        client=client,
    )

    assert prepared.status == "auphonic_prepared"
    assert prepared.auphonic_production_id == "prod-123"
    assert prepared.pre_edit_master_path is None
    assert prepared.transcript_path is None
    assert client.started == []
    assert client.downloads == []

    started = start_prepared_session_with_auphonic(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=prepared,
        client=client,
    )

    assert started.status == "waiting_for_finalize"
    assert client.started == ["prod-123"]
    assert len(client.downloads) == 2


def _build_config(tmp_path: Path, enabled: bool = True) -> AppConfig:
    runtime_home = tmp_path / "runtime"
    deliverables_root = tmp_path / "deliverables"
    return AppConfig(
        runtime_home=runtime_home,
        paths=PathsConfig(
            raw_root=tmp_path / "raw",
            deliverables_root=deliverables_root,
        ),
        polling=PollingConfig(interval_seconds=30),
        notifications=NotificationsConfig(enabled=False),
        auphonic=AuphonicConfig(
            enabled=enabled,
            api_key="secret-token" if enabled else "",
            base_url="https://auphonic.example/api",
            request_timeout_seconds=600,
            poll_interval_seconds=0,
            max_poll_attempts=2,
            preset_ids={
                "speakers_2": "preset-2",
                "speakers_3": "preset-3",
                "speakers_4": "preset-4",
            },
        ),
        podbean=PodbeanConfig(enabled=False, client_id="", client_secret=""),
        config_file=runtime_home / "config.yaml",
    )
