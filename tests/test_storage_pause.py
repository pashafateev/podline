"""Contract tests for the ingest-only storage kill switch.

storage.enabled=false pauses NEW session intake and nothing else: sessions
already registered in S3 must keep materializing, attaching to Auphonic,
and finalizing while the switch is off.
"""

from dataclasses import replace
from pathlib import Path

import boto3
import pytest

from podline.artifacts import clear_cache, materialize_artifact, store_artifact_file
from podline.auphonic import AuphonicPreparedProduction, prepare_session_with_auphonic
from podline.config import AppConfig
from podline.finalize import finalize_session
from podline.ingest import IngestError, import_local_folder
from podline.models import ArtifactKind
from podline.repository import get_media_artifact, get_session
from podline.storage import parse_s3_uri

from conftest import TEST_BUCKET


def _paused(config: AppConfig) -> AppConfig:
    """Flip enabled off while preserving bucket, region, prefix, and profile."""
    return replace(config, storage=replace(config.storage, enabled=False))


class _MinimalFakeAuphonicClient:
    def __init__(self) -> None:
        self.attached = []
        self._tracks = []

    def create_production(self, *, title: str, track_count: int, preset_id: str):
        return AuphonicPreparedProduction(
            production_id="prod-paused",
            upload_track_ids=[f"Track {i}" for i in range(1, track_count + 1)],
            status_page=None,
            edit_page=None,
        )

    def attach_service_input(
        self, production_id: str, track_id: str, service_id: str, input_key: str
    ):
        self.attached.append((track_id, service_id, input_key))
        self._tracks.append({"id": track_id, "input_file": input_key})
        return {"status_code": 200}

    def get_production(self, production_id: str):
        return {
            "status_code": 200,
            "data": {
                "uuid": production_id,
                "status": 10,
                "status_string": "Production Not Started Yet",
                "start_allowed": True,
                "change_allowed": True,
                "multi_input_files": [dict(track) for track in self._tracks],
                "output_files": [],
            },
        }


def test_paused_storage_refuses_new_imports(
    app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    folder = tmp_path / "riverside" / "2026-05-01_demo"
    folder.mkdir(parents=True)
    (folder / "Tr1.webm").write_bytes(b"webm-track-1-data" * 32)
    (folder / "Tr2.webm").write_bytes(b"webm-track-2-data" * 32)

    with pytest.raises(IngestError, match="paused"):
        import_local_folder(
            config=_paused(config),
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            folder=folder,
            duration_probe=lambda path: 60.0,
        )


def test_paused_storage_still_materializes_cold_artifacts(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    source = tmp_path / "src" / "final.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"final-master-bytes")
    store_artifact_file(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session_uid="session-live",
        kind=ArtifactKind.FINAL_MASTER.value,
        source_path=source,
        content_type="audio/mpeg",
    )
    assert clear_cache(runtime_paths) > 0

    materialized = materialize_artifact(
        config=_paused(config),
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session_uid="session-live",
        kind=ArtifactKind.FINAL_MASTER.value,
    )

    assert materialized.read_bytes() == b"final-master-bytes"


def test_paused_storage_still_prepares_auphonic(
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path: Path,
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = session_factory(runtime_paths.db_file, status="queued")
    for role, filename in (("host1", "Tr1.webm"), ("host2", "Tr2.webm")):
        path = tmp_path / "tracks" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"opus-{role}".encode("utf-8"))
        artifact_registrar(
            runtime_paths.db_file,
            runtime_paths,
            session_uid=session.session_uid,
            kind=ArtifactKind.RAW_TRACK.value,
            role=role,
            path=path,
            duration_seconds=60.0,
        )
    fake = _MinimalFakeAuphonicClient()

    updated = prepare_session_with_auphonic(
        config=_paused(config),
        db_file=runtime_paths.db_file,
        session=session,
        client=fake,
    )

    assert updated.status == "auphonic_prepared"
    assert [track_id for track_id, _service, _key in fake.attached] == [
        "Track 1",
        "Track 2",
    ]
    assert all(service == "svc-podline" for _t, service, _k in fake.attached)


def test_paused_storage_still_finalizes_edited_master(
    moto_s3,
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path: Path,
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = session_factory(
        runtime_paths.db_file, status="waiting_for_finalize"
    )
    for kind, filename, payload in (
        (ArtifactKind.PRE_EDIT_MASTER.value, "pre_edit.mp3", b"pre-edit"),
        (ArtifactKind.TRANSCRIPT.value, "ep.vtt", b"WEBVTT"),
    ):
        path = tmp_path / "artifacts" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        artifact_registrar(
            runtime_paths.db_file,
            runtime_paths,
            session_uid=session.session_uid,
            kind=kind,
            path=path,
        )
    master = tmp_path / "edited" / "sexdrunks_ep99_master.mp3"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"edited-master-bytes")

    updated = finalize_session(
        config=_paused(config),
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=session,
        master_path=master,
    )

    assert updated.status == "ready_for_draft"
    artifact = get_media_artifact(
        runtime_paths.db_file,
        session.session_uid,
        kind=ArtifactKind.FINAL_MASTER.value,
    )
    assert artifact is not None
    _bucket, key = parse_s3_uri(artifact.s3_uri)
    stored = boto3.client("s3", region_name="us-east-1").get_object(
        Bucket=TEST_BUCKET, Key=key
    )
    assert stored["Body"].read() == b"edited-master-bytes"
    assert get_session(runtime_paths.db_file, session.session_uid).status == (
        "ready_for_draft"
    )
