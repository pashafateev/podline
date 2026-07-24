import struct
import shutil
import wave
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from podline.artifacts import seed_cache
from podline.config import (
    AppConfig,
    AuphonicConfig,
    CozytrackConfig,
    NotificationsConfig,
    PathsConfig,
    PodbeanConfig,
    PollingConfig,
    StorageConfig,
)
from podline.db import connect, initialize_database
from podline.paths import build_runtime_paths, ensure_runtime_dirs
from podline.repository import get_session, register_media_artifact
from podline.storage import sha256_of_file

TEST_BUCKET = "test-podline-media"


@pytest.fixture(autouse=True)
def _aws_test_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture()
def moto_s3():
    """Activate moto and create the test bucket for the duration of a test."""
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=TEST_BUCKET)
        yield


@pytest.fixture()
def app_config_factory():
    def _build(
        tmp_path: Path,
        *,
        auphonic_enabled: bool = True,
        storage_enabled: bool = True,
        podbean_enabled: bool = False,
        cozytrack_api_key: str = "",
        s3_service_id: str = "svc-podline",
    ) -> AppConfig:
        runtime_home = tmp_path / "runtime"
        return AppConfig(
            runtime_home=runtime_home,
            paths=PathsConfig(
                raw_root=tmp_path / "raw",
                deliverables_root=tmp_path / "deliverables",
            ),
            polling=PollingConfig(interval_seconds=30),
            notifications=NotificationsConfig(enabled=False),
            auphonic=AuphonicConfig(
                enabled=auphonic_enabled,
                api_key="secret-token" if auphonic_enabled else "",
                base_url="https://auphonic.example/api",
                request_timeout_seconds=600,
                poll_interval_seconds=0,
                max_poll_attempts=2,
                preset_ids={
                    "transcript_refresh": "transcript-preset",
                    "speakers_2": "preset-2",
                    "speakers_3": "preset-3",
                    "speakers_4": "preset-4",
                },
                s3_service_id=s3_service_id,
            ),
            podbean=PodbeanConfig(
                enabled=podbean_enabled,
                client_id="id" if podbean_enabled else "",
                client_secret="secret" if podbean_enabled else "",
                base_url="https://api.podbean.example/v1",
                podcast_id="",
            ),
            cozytrack=CozytrackConfig(
                base_url="https://cozytrack.test",
                api_key=cozytrack_api_key,
                download_subdir="cozytrack",
                request_timeout_seconds=60,
            ),
            storage=StorageConfig(
                enabled=storage_enabled,
                bucket=TEST_BUCKET if storage_enabled else "",
                region="us-east-1",
                key_prefix="",
            ),
            config_file=runtime_home / "config.yaml",
        )

    return _build


@pytest.fixture()
def runtime_factory():
    def _build(config: AppConfig):
        runtime_paths = build_runtime_paths(config)
        ensure_runtime_dirs(runtime_paths)
        initialize_database(runtime_paths.db_file)
        return runtime_paths

    return _build


@pytest.fixture()
def session_factory():
    def _create(
        db_file: str,
        *,
        session_uid: str = "session-test-1",
        status: str = "queued",
        source_basename: str = "010124_013143",
        auphonic_production_id=None,
        auphonic_offsets_verified_production_id=None,
        transcript_refresh_production_id=None,
        transcript_refreshed_from_sha256=None,
        cozytrack_session_id=None,
    ):
        with connect(db_file) as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_uid, source_basename, source_path, fingerprint,
                    status, auphonic_production_id,
                    auphonic_offsets_verified_production_id,
                    transcript_refresh_production_id,
                    transcript_refreshed_from_sha256,
                    cozytrack_session_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_uid,
                    source_basename,
                    f"/ingested/{session_uid}",
                    f"fingerprint-{session_uid}",
                    status,
                    auphonic_production_id,
                    auphonic_offsets_verified_production_id,
                    transcript_refresh_production_id,
                    transcript_refreshed_from_sha256,
                    cozytrack_session_id,
                    "now",
                    "now",
                ),
            )
            connection.commit()
        session = get_session(db_file, session_uid)
        assert session is not None
        return session

    return _create


@pytest.fixture()
def artifact_registrar():
    """Register an artifact row backed by a local file and seed the cache.

    The seeded cache entry means materialize_artifact resolves without any
    S3 access, so consumer tests stay hermetic.
    """

    def _register(
        db_file: str,
        runtime_paths,
        *,
        session_uid: str,
        kind: str,
        path: Path,
        role: str = "",
        duration_seconds=None,
        bucket: str = TEST_BUCKET,
        source_provider: str = "test",
        source_id=None,
    ):
        sha256 = sha256_of_file(path)
        record = register_media_artifact(
            db_file,
            session_uid=session_uid,
            kind=kind,
            role=role,
            filename=path.name,
            content_type="application/octet-stream",
            byte_size=path.stat().st_size,
            sha256=sha256,
            duration_seconds=duration_seconds,
            s3_uri=f"s3://{bucket}/sessions/{session_uid}/{kind}/{sha256}/{path.name}",
            source_provider=source_provider,
            source_id=source_id,
            timestamp="now",
        )
        seed_cache(runtime_paths, source_path=path, sha256=sha256)
        return record

    return _register


@pytest.fixture()
def fixture_raw_root(tmp_path: Path) -> Path:
    source_root = Path(__file__).parent / "fixtures" / "raw"
    flat_root = tmp_path / "raw"
    flat_root.mkdir()

    for date_dir in source_root.iterdir():
        if not date_dir.is_dir():
            continue
        for session_dir in date_dir.iterdir():
            if session_dir.is_dir():
                shutil.copytree(session_dir, flat_root / session_dir.name)

    return flat_root


@pytest.fixture()
def wav_writer():
    def _write(path: Path, samples) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(44100)
            frames = b"".join(struct.pack("<h", int(sample)) for sample in samples)
            wav_file.writeframes(frames)
        return path

    return _write
