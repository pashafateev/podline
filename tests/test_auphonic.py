from pathlib import Path
import json

import httpx
import pytest

from podline.artifacts import workspace_path_for
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
    refresh_transcript_with_auphonic,
    resolve_auphonic_service_id,
    resolve_preset_id,
    start_prepared_session_with_auphonic,
)
from podline.repository import (
    get_episode,
    get_media_artifact,
    get_session,
    upsert_episode_metadata,
)


class FakeAuphonicClient:
    def __init__(self) -> None:
        self.created = []
        self.attached = []
        self.single_created = []
        self.started = []
        self.downloads = []
        self.tracks = []
        self._status = 10
        self._status_string = "Production Not Started Yet"

    def create_production(
        self, *, title: str, track_count: int, preset_id: str
    ) -> AuphonicPreparedProduction:
        self.created.append((title, track_count, preset_id))
        return AuphonicPreparedProduction(
            production_id="prod-123",
            upload_track_ids=["Jonathan", "Pasha"][:track_count],
            status_page="https://example.test/status/prod-123",
            edit_page="https://example.test/edit/prod-123",
        )

    def attach_service_input(
        self, production_id: str, track_id: str, service_id: str, input_key: str
    ):
        self.attached.append((production_id, track_id, service_id, input_key))
        self.tracks.append(
            {"id": track_id, "type": "multitrack", "input_file": input_key}
        )
        return {"status_code": 200}

    def create_single_input_production(
        self, *, title: str, preset_id: str, service_id: str, input_key: str
    ) -> str:
        self.single_created.append((title, preset_id, service_id, input_key))
        return "prod-refresh"

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
                "status": self._status,
                "status_string": self._status_string,
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
                "multi_input_files": [dict(track) for track in self.tracks],
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
            {"id": "Jonathan", "type": "multitrack", "input_file": "sessions/s/raw_track/aaa/Tr1.webm"},
            {"id": "Pasha", "type": "multitrack", "input_file": "sessions/s/raw_track/bbb/Tr2.webm"},
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


class FakeTranscriptRefreshClient(FakeAuphonicClient):
    def wait_for_production(self, production_id: str) -> AuphonicProduction:
        assert production_id == "prod-refresh"
        return AuphonicProduction(
            production_id=production_id,
            output_files=[
                AuphonicOutputFile(
                    kind="vtt",
                    download_url="https://example.test/final-transcript.vtt",
                    extension="vtt",
                ),
            ],
        )

    def download_file(self, url: str, destination: Path) -> None:
        self.downloads.append((url, destination))
        destination.write_text(
            "WEBVTT\n\n00:00.000 --> 00:01.000\nFinal cut transcript\n",
            encoding="utf-8",
        )


class FakeResumableTranscriptRefreshClient(FakeTranscriptRefreshClient):
    def __init__(self) -> None:
        super().__init__()
        self.wait_attempts = 0
        self.remote_done = False

    def wait_for_production(self, production_id: str) -> AuphonicProduction:
        self.wait_attempts += 1
        if self.wait_attempts == 1:
            self.remote_done = True
            raise AuphonicError("poll interrupted after remote start")
        return super().wait_for_production(production_id)

    def get_production(self, production_id: str):
        assert self.remote_done is True
        return {
            "status_code": 200,
            "data": {
                "uuid": production_id,
                "status": 3,
                "status_string": "Done",
                "start_allowed": False,
                "output_files": [
                    {
                        "format": "subtitle",
                        "download_url": "https://example.test/final-transcript.vtt",
                        "ending": "vtt",
                    }
                ],
            },
        }


def _queued_session_with_tracks(
    tmp_path, runtime_paths, session_factory, artifact_registrar, *, status="queued"
):
    session = session_factory(
        runtime_paths.db_file,
        session_uid="session-auphonic-1",
        status=status,
        auphonic_production_id=(
            "prod-123" if status not in {"queued"} else None
        ),
    )
    tr1 = tmp_path / "Tr1.webm"
    tr1.write_bytes(b"track-one-bytes")
    tr2 = tmp_path / "Tr2.webm"
    tr2.write_bytes(b"track-two-bytes!")
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid=session.session_uid,
        kind="raw_track",
        role="host1",
        path=tr1,
        duration_seconds=101.234,
    )
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid=session.session_uid,
        kind="raw_track",
        role="host2",
        path=tr2,
        duration_seconds=99.433,
    )
    return session


def test_resolve_preset_id_validates_config(app_config_factory, tmp_path) -> None:
    config = app_config_factory(tmp_path, auphonic_enabled=False)

    with pytest.raises(AuphonicError, match="disabled"):
        resolve_preset_id(config, 2)


def test_resolve_auphonic_service_id_requires_configuration(
    app_config_factory, tmp_path
) -> None:
    config = app_config_factory(tmp_path, s3_service_id="")
    with pytest.raises(AuphonicError, match="s3_service_id"):
        resolve_auphonic_service_id(config)


def test_resolve_auphonic_service_id_works_while_ingest_is_paused(
    app_config_factory, tmp_path
) -> None:
    config = app_config_factory(tmp_path, storage_enabled=False)
    assert resolve_auphonic_service_id(config)


def _raw_track(role: str, filename: str):
    from podline.models import MediaArtifactRecord

    return MediaArtifactRecord(
        session_uid="s1",
        kind="raw_track",
        role=role,
        filename=filename,
        content_type="audio/webm",
        byte_size=1,
        sha256="a" * 64,
        duration_seconds=1.0,
        s3_uri=f"s3://bucket/sessions/s1/raw_track/{'a' * 64}/{filename}",
        source_provider=None,
        source_id=None,
        created_at="t",
        updated_at="t",
    )


def test_pair_tracks_matches_preset_ids_by_name_when_possible() -> None:
    from podline.auphonic import _pair_tracks_with_preset_ids

    tracks = [_raw_track("host1", "Tr1.webm"), _raw_track("host2", "Tr2.webm")]
    pairs = _pair_tracks_with_preset_ids(["Host2", "Host1"], tracks)

    assert [(track_id, artifact.role) for track_id, artifact in pairs] == [
        ("Host2", "host2"),
        ("Host1", "host1"),
    ]


def test_pair_tracks_falls_back_to_natural_role_order() -> None:
    from podline.auphonic import _pair_tracks_with_preset_ids

    tracks = [
        _raw_track("host1", "Tr1.webm"),
        _raw_track("host10", "Tr10.webm"),
        _raw_track("host2", "Tr2.webm"),
    ]
    pairs = _pair_tracks_with_preset_ids(["Pasha", "Sam", "Guest"], tracks)

    # host10 must sort after host2, not between host1 and host2.
    assert [(track_id, artifact.role) for track_id, artifact in pairs] == [
        ("Pasha", "host1"),
        ("Sam", "host2"),
        ("Guest", "host10"),
    ]


def test_pair_tracks_rejects_count_mismatch() -> None:
    from podline.auphonic import _pair_tracks_with_preset_ids

    tracks = [_raw_track("host1", "Tr1.webm")]
    with pytest.raises(AuphonicError, match="track count"):
        _pair_tracks_with_preset_ids(["a", "b"], tracks)


def test_prepare_attaches_s3_tracks_through_service(
    moto_s3,
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path,
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _queued_session_with_tracks(
        tmp_path, runtime_paths, session_factory, artifact_registrar
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
    assert client.created == [("010124_013143", 2, "preset-2")]
    assert [
        (track_id, service_id) for _pid, track_id, service_id, _key in client.attached
    ] == [("Jonathan", "svc-podline"), ("Pasha", "svc-podline")]
    attached_keys = [key for _pid, _tid, _svc, key in client.attached]
    assert attached_keys[0].endswith("/Tr1.webm")
    assert attached_keys[1].endswith("/Tr2.webm")
    assert all(key.startswith("sessions/") for key in attached_keys)
    assert client.started == []
    assert client.downloads == []


def test_prepare_fails_when_artifact_lives_in_wrong_bucket(
    moto_s3,
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path,
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = session_factory(runtime_paths.db_file, status="queued")
    tr1 = tmp_path / "Tr1.webm"
    tr1.write_bytes(b"track-one")
    tr2 = tmp_path / "Tr2.webm"
    tr2.write_bytes(b"track-two")
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid=session.session_uid,
        kind="raw_track",
        role="host1",
        path=tr1,
        duration_seconds=10.0,
        bucket="some-other-bucket",
    )
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid=session.session_uid,
        kind="raw_track",
        role="host2",
        path=tr2,
        duration_seconds=11.0,
        bucket="some-other-bucket",
    )

    with pytest.raises(AuphonicError, match="backed by bucket"):
        prepare_session_with_auphonic(
            config=config,
            db_file=runtime_paths.db_file,
            session=session,
            client=FakeAuphonicClient(),
        )


def test_prepare_preview_then_start_splits_the_auphonic_flow(
    moto_s3,
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path,
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _queued_session_with_tracks(
        tmp_path, runtime_paths, session_factory, artifact_registrar
    )
    client = FakeAuphonicClient()

    prepared = prepare_session_with_auphonic(
        config=config,
        db_file=runtime_paths.db_file,
        session=session,
        client=client,
    )
    assert prepared.status == "auphonic_prepared"
    assert client.started == []

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

    master = get_media_artifact(
        runtime_paths.db_file, session.session_uid, kind="pre_edit_master"
    )
    transcript = get_media_artifact(
        runtime_paths.db_file, session.session_uid, kind="transcript"
    )
    assert master is not None and master.source_id == "prod-123"
    assert transcript is not None and transcript.source_provider == "auphonic"

    workspace = workspace_path_for(runtime_paths, session.session_uid)
    assert (workspace / master.filename).exists()
    assert (workspace / transcript.filename).exists()
    assert (workspace / "manifest.json").exists()


def test_split_start_requires_preview_first(
    moto_s3,
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path,
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _queued_session_with_tracks(
        tmp_path, runtime_paths, session_factory, artifact_registrar
    )
    client = FakeAuphonicClient()
    prepared = prepare_session_with_auphonic(
        config=config,
        db_file=runtime_paths.db_file,
        session=session,
        client=client,
    )

    with pytest.raises(AuphonicError, match="auphonic_previewed"):
        start_prepared_session_with_auphonic(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=prepared,
            client=client,
        )

    assert client.started == []
    reloaded = get_session(runtime_paths.db_file, session.session_uid)
    assert reloaded is not None
    assert reloaded.status == "auphonic_prepared"


def test_process_session_with_auphonic_runs_end_to_end(
    moto_s3,
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path,
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _queued_session_with_tracks(
        tmp_path, runtime_paths, session_factory, artifact_registrar
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
    assert client.started == ["prod-123"]
    workspace = workspace_path_for(runtime_paths, session.session_uid)
    assert workspace.exists()


def test_inspect_production_returns_verification_details(
    app_config_factory, tmp_path
) -> None:
    config = app_config_factory(tmp_path)
    client = FakeDoneAuphonicClient()
    client.tracks = [
        {"id": "Jonathan", "type": "multitrack", "input_file": "sessions/x/Tr1.webm"}
    ]

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


def test_auphonic_client_updates_multi_input_file_offset(
    app_config_factory, tmp_path
) -> None:
    config = app_config_factory(tmp_path)
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


def test_auphonic_client_attaches_service_inputs(
    app_config_factory, tmp_path
) -> None:
    config = app_config_factory(tmp_path)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"status_code": 200, "data": {}})

    client = AuphonicClient(config)
    client._client = httpx.Client(
        base_url=config.auphonic.base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {config.auphonic.api_key}"},
        transport=httpx.MockTransport(handler),
    )

    client.attach_service_input(
        "prod-123", "Jonathan", "svc-podline", "sessions/s/raw_track/aaa/Tr1.webm"
    )

    assert seen["url"].endswith("/production/prod-123/multi_input_files/Jonathan.json")
    assert seen["body"] == {
        "type": "multitrack",
        "service": "svc-podline",
        "input_file": "sessions/s/raw_track/aaa/Tr1.webm",
    }


def test_auphonic_client_creates_single_input_production(
    app_config_factory, tmp_path
) -> None:
    config = app_config_factory(tmp_path)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"data": {"uuid": "prod-refresh"}})

    client = AuphonicClient(config)
    client._client = httpx.Client(
        base_url=config.auphonic.base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {config.auphonic.api_key}"},
        transport=httpx.MockTransport(handler),
    )

    production_id = client.create_single_input_production(
        title="refresh",
        preset_id="transcript-preset",
        service_id="svc-podline",
        input_key="sessions/s/final_master/ccc/final.mp3",
    )

    assert production_id == "prod-refresh"
    assert seen["url"].endswith("/productions.json")
    assert seen["body"] == {
        "preset": "transcript-preset",
        "metadata": {"title": "refresh"},
        "service": "svc-podline",
        "input_file": "sessions/s/final_master/ccc/final.mp3",
    }


def _offset_session(
    tmp_path, runtime_paths, session_factory, artifact_registrar, *, durations=True
):
    session = session_factory(
        runtime_paths.db_file,
        session_uid="session-offset-1",
        status="auphonic_prepared",
        auphonic_production_id="prod-123",
    )
    tr1 = tmp_path / "Tr1.webm"
    tr1.write_bytes(b"track-one")
    tr2 = tmp_path / "Tr2.webm"
    tr2.write_bytes(b"track-two")
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid=session.session_uid,
        kind="raw_track",
        role="host1",
        path=tr1,
        duration_seconds=101.234 if durations else None,
    )
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid=session.session_uid,
        kind="raw_track",
        role="host2",
        path=tr2,
        duration_seconds=99.433 if durations else None,
    )
    return session


def test_session_offset_plan_uses_stored_durations(
    app_config_factory, runtime_factory, session_factory, artifact_registrar, tmp_path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _offset_session(
        tmp_path, runtime_paths, session_factory, artifact_registrar
    )
    client = FakeOffsetAuphonicClient()

    def exploding_probe(path: Path) -> float:
        raise AssertionError("stored durations must be used without probing")

    plan = plan_session_auphonic_offset(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=session,
        client=client,
        duration_probe=exploding_probe,
    )

    assert plan.target_asset.filename == "Tr2.webm"
    assert plan.longer_asset.filename == "Tr1.webm"
    assert plan.target_track_id == "Pasha"
    assert plan.offset_seconds == pytest.approx(1.801)


def test_session_offset_probes_and_persists_missing_durations(
    app_config_factory, runtime_factory, session_factory, artifact_registrar, tmp_path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _offset_session(
        tmp_path,
        runtime_paths,
        session_factory,
        artifact_registrar,
        durations=False,
    )
    client = FakeOffsetAuphonicClient()
    probed = []

    def probe(path: Path) -> float:
        probed.append(path.name)
        return {"Tr1.webm": 101.234, "Tr2.webm": 99.433}[path.name]

    plan = plan_session_auphonic_offset(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=session,
        client=client,
        duration_probe=probe,
    )

    assert sorted(probed) == ["Tr1.webm", "Tr2.webm"]
    assert plan.offset_seconds == pytest.approx(1.801)
    persisted = get_media_artifact(
        runtime_paths.db_file, session.session_uid, kind="raw_track", role="host1"
    )
    assert persisted is not None
    assert persisted.duration_seconds == pytest.approx(101.234)


def test_session_offset_dry_run_does_not_update_auphonic(
    app_config_factory, runtime_factory, session_factory, artifact_registrar, tmp_path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _offset_session(
        tmp_path, runtime_paths, session_factory, artifact_registrar
    )
    client = FakeOffsetAuphonicClient()

    result = offset_session_auphonic_production(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=session,
        apply=False,
        client=client,
    )

    assert result.applied is False
    assert result.track_state is None
    assert client.offset_updates == []


def test_session_offset_apply_updates_matched_track_id(
    app_config_factory, runtime_factory, session_factory, artifact_registrar, tmp_path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _offset_session(
        tmp_path, runtime_paths, session_factory, artifact_registrar
    )
    client = FakeOffsetAuphonicClient()

    result = offset_session_auphonic_production(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=session,
        apply=True,
        client=client,
    )

    assert result.applied is True
    assert client.offset_updates == [("prod-123", "Pasha", pytest.approx(1.801))]
    assert result.track_state is not None
    assert result.track_state["id"] == "Pasha"
    assert result.track_state["offset"] == pytest.approx(1.801)


def test_session_offset_fails_when_production_is_not_change_allowed(
    app_config_factory, runtime_factory, session_factory, artifact_registrar, tmp_path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _offset_session(
        tmp_path, runtime_paths, session_factory, artifact_registrar
    )
    client = FakeOffsetAuphonicClient(change_allowed=False)

    with pytest.raises(AuphonicError, match="change_allowed"):
        plan_session_auphonic_offset(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            client=client,
        )


def test_session_offset_fails_when_track_matching_is_ambiguous(
    app_config_factory, runtime_factory, session_factory, artifact_registrar, tmp_path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _offset_session(
        tmp_path, runtime_paths, session_factory, artifact_registrar
    )
    client = FakeOffsetAuphonicClient(
        tracks=[
            {"id": "Pasha-a", "type": "multitrack", "input_file": "Tr2.webm"},
            {"id": "Pasha-b", "type": "multitrack", "input_file": "Tr2.webm"},
        ]
    )

    with pytest.raises(AuphonicError) as exc_info:
        plan_session_auphonic_offset(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            client=client,
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


def test_reconcile_done_production_persists_outputs(
    moto_s3,
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path,
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _queued_session_with_tracks(
        tmp_path, runtime_paths, session_factory, artifact_registrar
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
    master = get_media_artifact(
        runtime_paths.db_file, session.session_uid, kind="pre_edit_master"
    )
    assert master is not None
    assert workspace_path_for(runtime_paths, session.session_uid).exists()


def test_reconcile_preserves_previewed_status(
    moto_s3,
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path,
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _queued_session_with_tracks(
        tmp_path, runtime_paths, session_factory, artifact_registrar
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


def _ready_for_draft_session(
    tmp_path, runtime_paths, session_factory, artifact_registrar
):
    session = session_factory(
        runtime_paths.db_file,
        session_uid="session-refresh-1",
        status="ready_for_draft",
        auphonic_production_id="prod-123",
    )
    old_transcript = tmp_path / "old-transcript.vtt"
    old_transcript.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nPre-edit transcript\n",
        encoding="utf-8",
    )
    final_master = tmp_path / "final-master.mp3"
    final_master.write_bytes(b"final-master")
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid=session.session_uid,
        kind="transcript",
        path=old_transcript,
    )
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid=session.session_uid,
        kind="final_master",
        path=final_master,
    )
    return session


def test_refresh_transcript_replaces_transcript_artifact(
    moto_s3,
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path,
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _ready_for_draft_session(
        tmp_path, runtime_paths, session_factory, artifact_registrar
    )
    old = get_media_artifact(
        runtime_paths.db_file, session.session_uid, kind="transcript"
    )
    upsert_episode_metadata(
        runtime_paths.db_file,
        session_uid=session.session_uid,
        title="Stale title",
        description="Stale description from the pre-edit transcript",
    )
    client = FakeTranscriptRefreshClient()

    updated = refresh_transcript_with_auphonic(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=session,
        client=client,
    )

    assert updated.status == "ready_for_draft"
    refreshed = get_media_artifact(
        runtime_paths.db_file, session.session_uid, kind="transcript"
    )
    assert refreshed is not None
    assert refreshed.sha256 != old.sha256
    assert refreshed.source_id == "prod-refresh"
    assert client.single_created == [
        (
            "010124_013143 transcript refresh",
            "transcript-preset",
            "svc-podline",
            client.single_created[0][3],
        )
    ]
    assert client.single_created[0][3].endswith("/final-master.mp3")
    assert client.started == ["prod-refresh"]
    assert get_episode(runtime_paths.db_file, session.session_uid) is None
    assert updated.transcript_refresh_production_id is None


def test_refresh_transcript_requires_final_master_artifact(
    moto_s3,
    app_config_factory,
    runtime_factory,
    session_factory,
    tmp_path,
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = session_factory(
        runtime_paths.db_file,
        session_uid="session-refresh-2",
        status="ready_for_draft",
    )

    with pytest.raises(AuphonicError, match="no final master artifact"):
        refresh_transcript_with_auphonic(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            client=FakeTranscriptRefreshClient(),
        )


def test_refresh_transcript_resumes_started_production_after_local_failure(
    moto_s3,
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path,
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _ready_for_draft_session(
        tmp_path, runtime_paths, session_factory, artifact_registrar
    )
    client = FakeResumableTranscriptRefreshClient()

    with pytest.raises(AuphonicError, match="poll interrupted"):
        refresh_transcript_with_auphonic(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            client=client,
        )

    interrupted = get_session(runtime_paths.db_file, session.session_uid)
    assert interrupted is not None
    assert interrupted.transcript_refresh_production_id == "prod-refresh"

    updated = refresh_transcript_with_auphonic(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=interrupted,
        client=client,
    )

    assert len(client.single_created) == 1
    assert client.started == ["prod-refresh"]
    assert updated.transcript_refresh_production_id is None


def test_pair_tracks_orders_fallback_by_recorder_position() -> None:
    from types import SimpleNamespace

    from podline.auphonic import _pair_tracks_with_preset_ids

    def _artifact(role: str, filename: str):
        return SimpleNamespace(role=role, filename=filename)

    tracks = [
        _artifact("guest1", "Tr3.webm"),
        _artifact("guest2", "Tr4.webm"),
        _artifact("host1", "Tr1.webm"),
        _artifact("host2", "Tr2.webm"),
    ]
    preset_ids = ["Pasha", "Jonathan", "Guest A", "Guest B"]

    pairs = _pair_tracks_with_preset_ids(preset_ids, tracks)

    assert [(track_id, artifact.role) for track_id, artifact in pairs] == [
        ("Pasha", "host1"),
        ("Jonathan", "host2"),
        ("Guest A", "guest1"),
        ("Guest B", "guest2"),
    ]


class MultiSpeakerFakeAuphonicClient(FakeAuphonicClient):
    """Preset with named tracks that do not match session roles."""

    def create_production(
        self, *, title: str, track_count: int, preset_id: str
    ) -> AuphonicPreparedProduction:
        self.created.append((title, track_count, preset_id))
        return AuphonicPreparedProduction(
            production_id="prod-123",
            upload_track_ids=[
                f"Preset Track {i}" for i in range(1, track_count + 1)
            ],
            status_page="https://example.test/status/prod-123",
            edit_page="https://example.test/edit/prod-123",
        )


@pytest.mark.parametrize(
    "roles",
    [
        [("host1", "Tr1.webm"), ("host2", "Tr2.webm"), ("guest1", "Tr3.webm")],
        [
            ("host1", "Tr1.webm"),
            ("host2", "Tr2.webm"),
            ("guest1", "Tr3.webm"),
            ("guest2", "Tr4.webm"),
        ],
    ],
    ids=["three-speakers", "four-speakers"],
)
def test_prepare_pairs_multi_speaker_roles_in_recorder_order(
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path,
    roles,
) -> None:
    """Recorder order is the contract: preset track N gets recorder track N.

    Roles are host1, host2, guest1, guest2 — any role-name sort (alphabetical
    or natural) puts the guests first and silently maps Tr3/Tr4 onto the
    first preset tracks.
    """
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = session_factory(runtime_paths.db_file, status="queued")
    for role, filename in roles:
        path = tmp_path / "tracks" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"opus-{role}".encode("utf-8"))
        artifact_registrar(
            runtime_paths.db_file,
            runtime_paths,
            session_uid=session.session_uid,
            kind="raw_track",
            role=role,
            path=path,
            duration_seconds=60.0,
        )
    client = MultiSpeakerFakeAuphonicClient()

    prepare_session_with_auphonic(
        config=config,
        db_file=runtime_paths.db_file,
        session=session,
        client=client,
    )

    attached = [
        (track_id, input_key.rsplit("/", 1)[-1])
        for (_production, track_id, _service, input_key) in client.attached
    ]
    assert attached == [
        (f"Preset Track {index + 1}", filename)
        for index, (_role, filename) in enumerate(roles)
    ]


def test_start_failure_after_persist_recovers_via_reconcile_without_restart(
    moto_s3,
    app_config_factory,
    runtime_factory,
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    """Crash between output persistence and the state transition is recoverable.

    The remote production must not be started a second time, and recovery
    must leave exactly one current artifact per kind plus a valid workspace.
    """
    import json

    from podline.artifacts import ArtifactError, workspace_path_for
    from podline.auphonic import reconcile_production_to_session
    from podline.repository import get_session as get_session_record
    from podline.repository import list_media_artifacts
    from podline.storage import sha256_of_file

    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = session_factory(
        runtime_paths.db_file,
        status="auphonic_previewed",
        auphonic_production_id="prod-123",
    )
    client = FakeAuphonicClient()

    def _local_failure(**_kwargs):
        raise ArtifactError("simulated local failure after outputs persisted")

    # monkeypatch.undo() would also undo the autouse AWS env fixture (one
    # MonkeyPatch instance per test), so scope this patch with a context.
    with monkeypatch.context() as patcher:
        patcher.setattr("podline.auphonic.create_workspace", _local_failure)
        with pytest.raises(ArtifactError, match="simulated local failure"):
            start_prepared_session_with_auphonic(
                config=config,
                runtime_paths=runtime_paths,
                db_file=runtime_paths.db_file,
                session=session,
                client=client,
            )

    failed = get_session_record(runtime_paths.db_file, session.session_uid)
    assert failed is not None
    assert failed.status == "failed"
    assert client.started == ["prod-123"]

    recovery_client = FakeDoneAuphonicClient()
    recovered = reconcile_production_to_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=failed,
        production_id="prod-123",
        client=recovery_client,
    )

    assert recovered.status == "waiting_for_finalize"
    assert recovery_client.started == []
    masters = list_media_artifacts(
        runtime_paths.db_file, session.session_uid, kind="pre_edit_master"
    )
    transcripts = list_media_artifacts(
        runtime_paths.db_file, session.session_uid, kind="transcript"
    )
    assert len(masters) == 1
    assert len(transcripts) == 1

    workspace = workspace_path_for(runtime_paths, session.session_uid)
    manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
    for kind, artifact in (
        ("pre_edit_master", masters[0]),
        ("transcript", transcripts[0]),
    ):
        entry = manifest["artifacts"][kind]
        copy = workspace / entry["filename"]
        assert copy.exists()
        assert entry["sha256"] == artifact.sha256
        assert sha256_of_file(copy) == artifact.sha256
