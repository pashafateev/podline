from pathlib import Path

from sexdrunks_automation.auphonic import (
    AuphonicError,
    AuphonicOutputFile,
    AuphonicPreparedProduction,
    AuphonicProduction,
    _is_done_status,
    _is_failed_status,
    inspect_production,
    prepare_session_with_auphonic,
    preview_prepared_session_with_auphonic,
    process_session_with_auphonic,
    reconcile_production_to_session,
    resolve_preset_id,
    start_prepared_session_with_auphonic,
)
from sexdrunks_automation.config import (
    AppConfig,
    AuphonicConfig,
    CozytrackConfig,
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

    def create_production(
        self, *, session, assets, preset_id: str
    ) -> AuphonicPreparedProduction:
        self.created.append((session.session_uid, len(assets), preset_id))
        return AuphonicPreparedProduction(
            production_id="prod-123",
            upload_track_ids=["Jonathan", "Pasha"][: len(assets)],
            status_page="https://example.test/status/prod-123",
            edit_page="https://example.test/edit/prod-123",
        )

    def upload_assets(self, production_id: str, assets, upload_track_ids) -> None:
        self.uploaded.append((production_id, len(assets), list(upload_track_ids)))

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

    def get_production(self, production_id: str):
        return {
            "status_code": 200,
            "error_message": "",
            "data": {
                "uuid": production_id,
                "status": 10,
                "status_string": "Production Not Started Yet",
                "start_allowed": True,
                "change_allowed": True,
                "edit_page": f"https://example.test/edit/{production_id}",
                "status_page": f"https://example.test/status/{production_id}",
                "warning_message": "",
                "output_files": [
                    {
                        "format": "mp3",
                        "download_url": "https://example.test/master.mp3",
                        "ending": "mp3",
                    },
                    {
                        "format": "subtitle",
                        "download_url": "https://example.test/transcript.vtt",
                        "ending": "vtt",
                    },
                ],
                "multi_input_files": [
                    {"id": "Jonathan", "type": "multitrack", "input_file": "Tr1.WAV"},
                    {"id": "Pasha", "type": "multitrack", "input_file": "Tr2.WAV"},
                ],
            },
        }


class FakeDoneAuphonicClient(FakeAuphonicClient):
    def get_production(self, production_id: str):
        payload = super().get_production(production_id)
        payload["data"]["status"] = 3
        payload["data"]["status_string"] = "Done"
        return payload


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
    client = FakeDoneAuphonicClient()

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
    assert client.uploaded[0][2] == ["Jonathan", "Pasha"]
    assert client.started == ["prod-123"]

    reloaded = get_session(runtime_paths.db_file, session.session_uid)
    assert reloaded is not None
    assert reloaded.status == "waiting_for_finalize"


def test_prepare_preview_then_start_splits_the_auphonic_flow(
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
    assert client.uploaded[0][2] == ["Jonathan", "Pasha"]

    preview = preview_prepared_session_with_auphonic(
        config=config,
        db_file=runtime_paths.db_file,
        session=prepared,
        client=client,
    )

    assert preview.session.status == "auphonic_previewed"
    assert preview.details["edit_page"] == "https://example.test/edit/prod-123"
    assert client.started == []

    started = start_prepared_session_with_auphonic(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=preview.session,
        client=client,
    )

    assert started.status == "waiting_for_finalize"
    assert client.started == ["prod-123"]
    assert len(client.downloads) == 2


def test_split_start_requires_preview_first(
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

    try:
        start_prepared_session_with_auphonic(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=prepared,
            client=client,
        )
    except AuphonicError as exc:
        assert "auphonic_previewed" in str(exc)
    else:
        raise AssertionError("Expected AuphonicError before preview")

    assert client.started == []
    reloaded = get_session(runtime_paths.db_file, session.session_uid)
    assert reloaded is not None
    assert reloaded.status == "auphonic_prepared"


def test_preview_prepared_session_marks_session_previewed(
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

    preview = preview_prepared_session_with_auphonic(
        config=config,
        db_file=runtime_paths.db_file,
        session=prepared,
        client=client,
    )

    assert preview.session.status == "auphonic_previewed"
    assert preview.session.auphonic_production_id == "prod-123"
    assert preview.details["edit_page"] == "https://example.test/edit/prod-123"
    assert client.started == []
    assert client.downloads == []

    reloaded = get_session(runtime_paths.db_file, session.session_uid)
    assert reloaded is not None
    assert reloaded.status == "auphonic_previewed"


def test_start_accepts_previewed_auphonic_session(
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
    preview = preview_prepared_session_with_auphonic(
        config=config,
        db_file=runtime_paths.db_file,
        session=prepared,
        client=client,
    )

    started = start_prepared_session_with_auphonic(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=preview.session,
        client=client,
    )

    assert started.status == "waiting_for_finalize"
    assert client.started == ["prod-123"]
    assert len(client.downloads) == 2


def test_inspect_production_returns_verification_details(tmp_path: Path) -> None:
    config = _build_config(tmp_path)
    client = FakeDoneAuphonicClient()

    details = inspect_production(config, "prod-123", client=client)

    assert details["production_id"] == "prod-123"
    assert details["start_allowed"] is True
    assert details["multi_input_files"][0]["id"] == "Jonathan"


def test_done_status_is_not_treated_as_failed() -> None:
    data_node = {"status": 3, "status_string": "Done"}

    assert _is_done_status(data_node) is True
    assert _is_failed_status(data_node) is False


def test_audio_processing_status_is_not_treated_as_failed() -> None:
    data_node = {"status": 4, "status_string": "Audio Processing"}

    assert _is_done_status(data_node) is False
    assert _is_failed_status(data_node) is False


def test_error_status_is_treated_as_failed() -> None:
    data_node = {"status": 2, "status_string": "Error"}

    assert _is_failed_status(data_node) is True


def test_reconcile_done_production_downloads_outputs(
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
    client = FakeDoneAuphonicClient()

    updated = reconcile_production_to_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=session,
        production_id="prod-123",
        client=client,
    )

    assert updated.status == "waiting_for_finalize"
    assert updated.auphonic_production_id == "prod-123"
    assert Path(updated.pre_edit_master_path).exists()
    assert Path(updated.transcript_path).exists()


def test_reconcile_preserves_previewed_status(
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
    preview = preview_prepared_session_with_auphonic(
        config=config,
        db_file=runtime_paths.db_file,
        session=prepared,
        client=client,
    )
    assert preview.session.status == "auphonic_previewed"

    # Auphonic still reports start_allowed=True for a previewed-but-not-started
    # production; sync must not downgrade the local previewed marker.
    updated = reconcile_production_to_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=preview.session,
        production_id="prod-123",
        client=client,
    )

    assert updated.status == "auphonic_previewed"
    assert client.started == []
    reloaded = get_session(runtime_paths.db_file, session.session_uid)
    assert reloaded is not None
    assert reloaded.status == "auphonic_previewed"


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
        podbean=PodbeanConfig(
            enabled=False,
            client_id="",
            client_secret="",
            base_url="https://api.podbean.example/v1",
            podcast_id="",
        ),
        cozytrack=_default_cozytrack_config(),
        config_file=runtime_home / "config.yaml",
    )


def _default_cozytrack_config() -> CozytrackConfig:
    return CozytrackConfig(
        base_url="http://localhost:3000",
        api_key="",
        download_subdir="cozytrack",
        request_timeout_seconds=600,
    )
