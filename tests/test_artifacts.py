from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from podline.artifacts import (
    ArtifactError,
    cache_path_for,
    cache_usage_bytes,
    clear_cache,
    materialize_artifact,
    store_artifact_file,
)
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
from podline.db import initialize_database
from podline.models import ArtifactKind, RuntimePaths
from podline.repository import get_media_artifact, list_media_artifacts
from podline.storage import parse_s3_uri

BUCKET = "test-podline-media"


@pytest.fixture(autouse=True)
def aws_test_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def _build_config(tmp_path: Path) -> AppConfig:
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
            enabled=False,
            api_key="",
            base_url="https://auphonic.example/api",
            request_timeout_seconds=600,
            poll_interval_seconds=0,
            max_poll_attempts=2,
            preset_ids={},
        ),
        podbean=PodbeanConfig(
            enabled=False,
            client_id="",
            client_secret="",
            base_url="https://api.podbean.example/v1",
            podcast_id="",
        ),
        cozytrack=CozytrackConfig(
            base_url="http://localhost:3000",
            api_key="",
            download_subdir="cozytrack",
            request_timeout_seconds=600,
        ),
        storage=StorageConfig(
            enabled=True,
            bucket=BUCKET,
            region="us-east-1",
            key_prefix="",
        ),
        config_file=runtime_home / "config.yaml",
    )


def _build_runtime_paths(tmp_path: Path) -> RuntimePaths:
    home = tmp_path / "runtime"
    paths = RuntimePaths(
        home=str(home),
        config_file=str(home / "config.yaml"),
        state_dir=str(home / "state"),
        db_file=str(home / "state" / "state.db"),
        health_file=str(home / "state" / "health.json"),
        logs_dir=str(home / "logs"),
        staging_dir=str(home / "staging"),
        cache_dir=str(home / "cache"),
        workspaces_dir=str(home / "workspaces"),
    )
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "cache").mkdir(parents=True, exist_ok=True)
    initialize_database(paths.db_file)
    return paths


def _setup(tmp_path: Path):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    config = _build_config(tmp_path)
    runtime_paths = _build_runtime_paths(tmp_path)
    source = tmp_path / "source" / "Tr1.webm"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"opus-track-one")
    return config, runtime_paths, source


@mock_aws
def test_store_artifact_file_uploads_registers_and_seeds_cache(
    tmp_path: Path,
) -> None:
    config, runtime_paths, source = _setup(tmp_path)

    record = store_artifact_file(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session_uid="session-1",
        kind=ArtifactKind.RAW_TRACK.value,
        role="tr1",
        source_path=source,
        content_type="audio/webm",
        duration_seconds=12.5,
        source_provider="cozytrack",
        source_id="ct-track-9",
    )

    assert record.session_uid == "session-1"
    assert record.kind == "raw_track"
    assert record.role == "tr1"
    assert record.filename == "Tr1.webm"
    assert record.byte_size == len(b"opus-track-one")
    assert record.duration_seconds == 12.5
    assert record.source_provider == "cozytrack"
    assert record.source_id == "ct-track-9"
    assert record.sha256 in record.s3_uri

    _bucket, key = parse_s3_uri(record.s3_uri)
    stored = boto3.client("s3", region_name="us-east-1").get_object(
        Bucket=BUCKET, Key=key
    )
    assert stored["Body"].read() == b"opus-track-one"

    cached = cache_path_for(runtime_paths, record.sha256, record.filename)
    assert cached.exists()
    assert cached.read_bytes() == b"opus-track-one"


@mock_aws
def test_store_artifact_file_replaces_current_row_per_kind_and_role(
    tmp_path: Path,
) -> None:
    config, runtime_paths, source = _setup(tmp_path)
    common = dict(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session_uid="session-1",
        kind=ArtifactKind.TRANSCRIPT.value,
        content_type="text/vtt",
    )

    first = store_artifact_file(source_path=source, **common)

    replacement = tmp_path / "source" / "refreshed.vtt"
    replacement.write_bytes(b"WEBVTT refreshed")
    second = store_artifact_file(source_path=replacement, **common)

    rows = list_media_artifacts(
        runtime_paths.db_file, "session-1", kind=ArtifactKind.TRANSCRIPT.value
    )
    assert len(rows) == 1
    assert rows[0].sha256 == second.sha256
    assert rows[0].filename == "refreshed.vtt"
    assert rows[0].created_at == first.created_at

    # The superseded object stays in the bucket as history.
    _bucket, first_key = parse_s3_uri(first.s3_uri)
    assert (
        boto3.client("s3", region_name="us-east-1")
        .get_object(Bucket=BUCKET, Key=first_key)["Body"]
        .read()
        == b"opus-track-one"
    )


@mock_aws
def test_materialize_artifact_prefers_verified_cache(tmp_path: Path) -> None:
    config, runtime_paths, source = _setup(tmp_path)
    record = store_artifact_file(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session_uid="session-1",
        kind=ArtifactKind.FINAL_MASTER.value,
        source_path=source,
        content_type="audio/webm",
    )

    # Remove the object so any S3 download attempt would fail loudly.
    _bucket, key = parse_s3_uri(record.s3_uri)
    boto3.client("s3", region_name="us-east-1").delete_object(Bucket=BUCKET, Key=key)

    materialized = materialize_artifact(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session_uid="session-1",
        kind=ArtifactKind.FINAL_MASTER.value,
    )
    assert materialized.read_bytes() == b"opus-track-one"


@mock_aws
def test_materialize_artifact_downloads_on_cold_cache(tmp_path: Path) -> None:
    config, runtime_paths, source = _setup(tmp_path)
    store_artifact_file(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session_uid="session-1",
        kind=ArtifactKind.PRE_EDIT_MASTER.value,
        source_path=source,
        content_type="audio/webm",
    )
    assert clear_cache(runtime_paths) > 0
    assert cache_usage_bytes(runtime_paths) == 0

    materialized = materialize_artifact(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session_uid="session-1",
        kind=ArtifactKind.PRE_EDIT_MASTER.value,
    )

    assert materialized.read_bytes() == b"opus-track-one"
    assert cache_usage_bytes(runtime_paths) == len(b"opus-track-one")


@mock_aws
def test_materialize_artifact_replaces_corrupt_cache_entry(tmp_path: Path) -> None:
    config, runtime_paths, source = _setup(tmp_path)
    record = store_artifact_file(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session_uid="session-1",
        kind=ArtifactKind.RAW_TRACK.value,
        role="tr1",
        source_path=source,
        content_type="audio/webm",
    )

    cached = cache_path_for(runtime_paths, record.sha256, record.filename)
    cached.write_bytes(b"corrupted cache entry")

    materialized = materialize_artifact(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session_uid="session-1",
        kind=ArtifactKind.RAW_TRACK.value,
        role="tr1",
    )
    assert materialized.read_bytes() == b"opus-track-one"


@mock_aws
def test_materialize_artifact_rejects_bucket_mismatch(tmp_path: Path) -> None:
    from podline.repository import register_media_artifact

    config, runtime_paths, _source = _setup(tmp_path)
    register_media_artifact(
        runtime_paths.db_file,
        session_uid="session-1",
        kind=ArtifactKind.TRANSCRIPT.value,
        role="",
        filename="t.vtt",
        content_type="text/vtt",
        byte_size=9,
        sha256="a" * 64,
        duration_seconds=None,
        s3_uri="s3://some-other-bucket/sessions/session-1/transcript/aa/t.vtt",
        source_provider=None,
        source_id=None,
        timestamp="2026-07-20T00:00:00+00:00",
    )

    with pytest.raises(ArtifactError, match="configured for bucket"):
        materialize_artifact(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session_uid="session-1",
            kind=ArtifactKind.TRANSCRIPT.value,
        )


@mock_aws
def test_materialize_artifact_without_row_raises_clear_error(tmp_path: Path) -> None:
    config, runtime_paths, _source = _setup(tmp_path)

    with pytest.raises(ArtifactError, match="No transcript artifact registered"):
        materialize_artifact(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session_uid="session-1",
            kind=ArtifactKind.TRANSCRIPT.value,
        )


def test_workspace_cleanup_respects_status_and_retention_window(
    session_factory, tmp_path: Path
) -> None:
    import os
    import time as time_module

    from podline.artifacts import (
        apply_workspace_cleanup,
        plan_workspace_cleanup,
        workspace_path_for,
    )

    runtime_paths = _build_runtime_paths(tmp_path)
    now = time_module.time()
    old = now - 20 * 86400
    recent = now - 2 * 86400

    def _make_workspace(session_uid: str, mtime: float) -> Path:
        workspace = workspace_path_for(runtime_paths, session_uid)
        workspace.mkdir(parents=True)
        project = workspace / "edit.cpr"
        project.write_bytes(b"cubase-project")
        os.utime(project, (mtime, mtime))
        os.utime(workspace, (mtime, mtime))
        return workspace

    session_factory(
        runtime_paths.db_file, session_uid="released-old", status="draft_created"
    )
    session_factory(
        runtime_paths.db_file, session_uid="released-fresh", status="draft_created"
    )
    session_factory(
        runtime_paths.db_file, session_uid="mid-pipeline", status="waiting_for_finalize"
    )
    released_old = _make_workspace("released-old", old)
    released_fresh = _make_workspace("released-fresh", recent)
    mid_pipeline = _make_workspace("mid-pipeline", old)
    unknown = _make_workspace("no-session-row", old)

    candidates = plan_workspace_cleanup(
        runtime_paths, runtime_paths.db_file, older_than_days=14, now=now
    )
    by_uid = {candidate.session_uid: candidate for candidate in candidates}

    assert by_uid["released-old"].eligible is True
    assert by_uid["released-fresh"].eligible is False
    assert "< 14d" in by_uid["released-fresh"].reason
    assert by_uid["mid-pipeline"].eligible is False
    assert "waiting_for_finalize" in by_uid["mid-pipeline"].reason
    assert by_uid["no-session-row"].eligible is False

    freed = apply_workspace_cleanup(candidates)

    assert freed == len(b"cubase-project")
    assert not released_old.exists()
    assert released_fresh.exists()
    assert mid_pipeline.exists()
    assert unknown.exists()


def test_workspace_cleanup_rejects_negative_retention(tmp_path: Path) -> None:
    from podline.artifacts import plan_workspace_cleanup

    runtime_paths = _build_runtime_paths(tmp_path)
    with pytest.raises(ArtifactError, match="at least 0 days"):
        plan_workspace_cleanup(
            runtime_paths, runtime_paths.db_file, older_than_days=-1
        )


def test_workspace_cleanup_uses_newest_file_mtime(
    session_factory, tmp_path: Path
) -> None:
    import os
    import time as time_module

    from podline.artifacts import plan_workspace_cleanup, workspace_path_for

    runtime_paths = _build_runtime_paths(tmp_path)
    now = time_module.time()
    session_factory(
        runtime_paths.db_file, session_uid="touched-lately", status="draft_created"
    )
    workspace = workspace_path_for(runtime_paths, "touched-lately")
    workspace.mkdir(parents=True)
    old_file = workspace / "old.mp3"
    old_file.write_bytes(b"old")
    os.utime(old_file, (now - 30 * 86400, now - 30 * 86400))
    fresh_file = workspace / "edit.cpr"
    fresh_file.write_bytes(b"fresh")
    os.utime(fresh_file, (now - 1 * 86400, now - 1 * 86400))
    os.utime(workspace, (now - 30 * 86400, now - 30 * 86400))

    candidates = plan_workspace_cleanup(
        runtime_paths, runtime_paths.db_file, older_than_days=14, now=now
    )

    assert len(candidates) == 1
    assert candidates[0].eligible is False
    assert "1.0d ago" in candidates[0].reason


@mock_aws
def test_get_media_artifact_distinguishes_roles(tmp_path: Path) -> None:
    config, runtime_paths, source = _setup(tmp_path)
    second_track = tmp_path / "source" / "Tr2.webm"
    second_track.write_bytes(b"opus-track-two")

    for role, path in (("tr1", source), ("tr2", second_track)):
        store_artifact_file(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session_uid="session-1",
            kind=ArtifactKind.RAW_TRACK.value,
            role=role,
            source_path=path,
            content_type="audio/webm",
        )

    tr2 = get_media_artifact(
        runtime_paths.db_file,
        "session-1",
        kind=ArtifactKind.RAW_TRACK.value,
        role="tr2",
    )
    assert tr2 is not None
    assert tr2.filename == "Tr2.webm"
    assert (
        len(list_media_artifacts(runtime_paths.db_file, "session-1")) == 2
    )


def test_create_workspace_refreshes_replaced_transcript(
    app_config_factory, runtime_factory, artifact_registrar, tmp_path: Path
) -> None:
    """Replacing the current transcript (same filename, new content) must
    refresh the workspace copy and manifest on the next workspace run."""
    import json

    from podline.artifacts import create_workspace
    from podline.storage import sha256_of_file

    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    master = tmp_path / "src" / "pre_edit.mp3"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"pre-edit-master")
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid="s-ws",
        kind="pre_edit_master",
        path=master,
    )
    original = tmp_path / "v1" / "ep.vtt"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"WEBVTT\n\noriginal\n")
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid="s-ws",
        kind="transcript",
        path=original,
    )

    workspace = create_workspace(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session_uid="s-ws",
    )
    assert (workspace / "ep.vtt").read_bytes() == b"WEBVTT\n\noriginal\n"

    refreshed = tmp_path / "v2" / "ep.vtt"
    refreshed.parent.mkdir(parents=True)
    refreshed.write_bytes(b"WEBVTT\n\nrefreshed after transcript-refresh\n")
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid="s-ws",
        kind="transcript",
        path=refreshed,
    )

    workspace = create_workspace(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session_uid="s-ws",
    )

    assert (
        workspace / "ep.vtt"
    ).read_bytes() == b"WEBVTT\n\nrefreshed after transcript-refresh\n"
    manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifacts"]["transcript"]["sha256"] == sha256_of_file(refreshed)
