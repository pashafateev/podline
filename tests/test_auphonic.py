from pathlib import Path
import json

import httpx
import pytest

from podline.auphonic import (
    AuphonicClient,
    AuphonicError,
    AuphonicOutputFile,
    AuphonicPreparedProduction,
    AuphonicProduction,
    _duration_from_ffprobe_packet_data,
    _is_done_status,
    _is_failed_status,
    inspect_production,
    offset_session_auphonic_production,
    plan_session_auphonic_offset,
    prepare_session_with_auphonic,
    preview_prepared_session_with_auphonic,
    process_session_with_auphonic,
    reconcile_production_to_session,
    resolve_preset_id,
    start_prepared_session_with_auphonic,
)
from podline.config import (
    AppConfig,
    AuphonicConfig,
    CozytrackConfig,
    NotificationsConfig,
    PathsConfig,
    PodbeanConfig,
    PollingConfig,
)
from podline.db import connect, initialize_database
from podline.ingest import scan_and_ingest
from podline.paths import build_runtime_paths, ensure_runtime_dirs
from podline.repository import get_session, list_sessions


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


class FakeOffsetAuphonicClient:
    def __init__(
        self,
        *,
        change_allowed: bool = True,
        start_allowed: bool = True,
        tracks=None,
    ) -> None:
        self.change_allowed = change_allowed
        self.start_allowed = start_allowed
        self.tracks = tracks or [
            {"id": "Jonathan", "type": "multitrack", "input_file": "Tr1.webm"},
            {"id": "Pasha", "type": "multitrack", "input_file": "Tr2.webm"},
        ]
        self.offset_updates = []

    def get_production(self, production_id: str):
        return {
            "status_code": 200,
            "error_message": "",
            "data": {
                "uuid": production_id,
                "status": 10,
                "status_string": "Production Not Started Yet",
                "start_allowed": self.start_allowed,
                "change_allowed": self.change_allowed,
                "edit_page": f"https://example.test/edit/{production_id}",
                "status_page": f"https://example.test/status/{production_id}",
                "warning_message": "",
                "multi_input_files": [dict(track) for track in self.tracks],
                "output_files": [],
            },
        }

    def update_multi_input_file_offset(
        self, production_id: str, track_id: str, offset: float
    ):
        self.offset_updates.append((production_id, track_id, offset))
        for track in self.tracks:
            if track.get("id") == track_id:
                track["offset"] = offset
        return {"status_code": 200, "data": {"id": track_id, "offset": offset}}


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


def test_duration_from_ffprobe_packet_json_uses_last_packet() -> None:
    duration = _duration_from_ffprobe_packet_data(
        {
            "packets": [
                {"pts_time": "0.000000", "duration_time": "0.021333"},
                {"pts_time": "12.300000", "duration_time": "0.023000"},
            ]
        },
        source=Path("Tr2.webm"),
    )

    assert duration == pytest.approx(12.323)


def test_auphonic_client_updates_multi_input_file_offset(tmp_path: Path) -> None:
    config = _build_config(tmp_path)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"status_code": 200, "data": {"id": "Pasha"}})

    client = AuphonicClient(config)
    client._client = httpx.Client(
        base_url=config.auphonic.base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {config.auphonic.api_key}"},
        transport=httpx.MockTransport(handler),
    )

    client.update_multi_input_file_offset("prod-123", "Pasha", 1.801)

    assert seen["method"] == "POST"
    assert (
        seen["url"]
        == "https://auphonic.example/api/production/prod-123/multi_input_files/Pasha.json"
    )
    assert seen["body"] == {"type": "multitrack", "offset": 1.801}


def test_session_offset_plan_targets_shorter_asset(tmp_path: Path) -> None:
    config, runtime_paths, session = _build_offset_session(tmp_path)
    client = FakeOffsetAuphonicClient()

    plan = plan_session_auphonic_offset(
        config=config,
        db_file=runtime_paths.db_file,
        session=session,
        client=client,
        duration_probe=_duration_probe({"Tr1.webm": 101.234, "Tr2.webm": 99.433}),
    )

    assert plan.target_asset.filename == "Tr2.webm"
    assert plan.longer_asset.filename == "Tr1.webm"
    assert plan.target_track_id == "Pasha"
    assert plan.target_input_file == "Tr2.webm"
    assert plan.offset_seconds == pytest.approx(1.801)


def test_session_offset_dry_run_does_not_update_auphonic(tmp_path: Path) -> None:
    config, runtime_paths, session = _build_offset_session(tmp_path)
    client = FakeOffsetAuphonicClient()

    result = offset_session_auphonic_production(
        config=config,
        db_file=runtime_paths.db_file,
        session=session,
        apply=False,
        client=client,
        duration_probe=_duration_probe({"Tr1.webm": 101.234, "Tr2.webm": 99.433}),
    )

    assert result.applied is False
    assert result.track_state is None
    assert client.offset_updates == []


def test_session_offset_apply_updates_matched_track_id(tmp_path: Path) -> None:
    config, runtime_paths, session = _build_offset_session(tmp_path)
    client = FakeOffsetAuphonicClient()

    result = offset_session_auphonic_production(
        config=config,
        db_file=runtime_paths.db_file,
        session=session,
        apply=True,
        client=client,
        duration_probe=_duration_probe({"Tr1.webm": 101.234, "Tr2.webm": 99.433}),
    )

    assert result.applied is True
    assert client.offset_updates == [("prod-123", "Pasha", pytest.approx(1.801))]
    assert result.track_state is not None
    assert result.track_state["id"] == "Pasha"
    assert result.track_state["offset"] == pytest.approx(1.801)


def test_session_offset_fails_when_production_is_not_change_allowed(
    tmp_path: Path,
) -> None:
    config, runtime_paths, session = _build_offset_session(tmp_path)
    client = FakeOffsetAuphonicClient(change_allowed=False)

    with pytest.raises(AuphonicError, match="change_allowed"):
        plan_session_auphonic_offset(
            config=config,
            db_file=runtime_paths.db_file,
            session=session,
            client=client,
            duration_probe=_duration_probe({"Tr1.webm": 101.234, "Tr2.webm": 99.433}),
        )


def test_session_offset_fails_when_track_matching_is_ambiguous(
    tmp_path: Path,
) -> None:
    config, runtime_paths, session = _build_offset_session(tmp_path)
    client = FakeOffsetAuphonicClient(
        tracks=[
            {"id": "Pasha-a", "type": "multitrack", "input_file": "Tr2.webm"},
            {"id": "Pasha-b", "type": "multitrack", "input_file": "Tr2.webm"},
        ]
    )

    with pytest.raises(AuphonicError) as exc_info:
        plan_session_auphonic_offset(
            config=config,
            db_file=runtime_paths.db_file,
            session=session,
            client=client,
            duration_probe=_duration_probe({"Tr1.webm": 101.234, "Tr2.webm": 99.433}),
        )

    assert "Ambiguous" in str(exc_info.value)
    assert "Pasha-a" in str(exc_info.value)
    assert "Pasha-b" in str(exc_info.value)


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


def _build_offset_session(tmp_path: Path):
    config = _build_config(tmp_path)
    runtime_paths = build_runtime_paths(config)
    ensure_runtime_dirs(runtime_paths)
    initialize_database(runtime_paths.db_file)

    source_dir = tmp_path / "raw" / "cozytrack-session"
    source_dir.mkdir(parents=True)
    tr1 = source_dir / "Tr1.webm"
    tr2 = source_dir / "Tr2.webm"
    tr1.write_bytes(b"track-one")
    tr2.write_bytes(b"track-two")

    with connect(runtime_paths.db_file) as connection:
        connection.execute(
            "INSERT INTO sessions (session_uid, source_path, fingerprint, status, auphonic_production_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "session-offset-1",
                str(source_dir),
                "fingerprint-offset-1",
                "auphonic_prepared",
                "prod-123",
                "now",
                "now",
            ),
        )
        connection.execute(
            "INSERT INTO assets (session_uid, role, filename, file_size, local_path) VALUES (?, ?, ?, ?, ?)",
            ("session-offset-1", "host1", "Tr1.webm", 9, str(tr1)),
        )
        connection.execute(
            "INSERT INTO assets (session_uid, role, filename, file_size, local_path) VALUES (?, ?, ?, ?, ?)",
            ("session-offset-1", "host2", "Tr2.webm", 9, str(tr2)),
        )
        connection.commit()

    session = get_session(runtime_paths.db_file, "session-offset-1")
    assert session is not None
    return config, runtime_paths, session


def _duration_probe(durations):
    def probe(path: Path) -> float:
        return durations[path.name]

    return probe
