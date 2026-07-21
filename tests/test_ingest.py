from pathlib import Path

import boto3
import pytest

from conftest import TEST_BUCKET
from podline.artifacts import cache_path_for
from podline.ingest import IngestError, import_local_folder
from podline.repository import (
    count_sessions,
    list_media_artifacts,
    list_sessions,
)
from podline.storage import parse_s3_uri


def _make_session_folder(tmp_path: Path, name: str = "010124_013143") -> Path:
    folder = tmp_path / "recordings" / name
    folder.mkdir(parents=True)
    (folder / f"{name}_Tr1.WAV").write_bytes(b"alpha-track-1")
    (folder / f"{name}_Tr2.WAV").write_bytes(b"alpha-track-2-longer")
    return folder


def _fixed_probe(path: Path) -> float:
    return {"Tr1": 101.5, "Tr2": 99.25}.get(path.stem.rsplit("_", 1)[-1], 42.0)


def test_import_local_folder_registers_session_and_artifacts(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    folder = _make_session_folder(tmp_path)

    record, created = import_local_folder(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        folder=folder,
        duration_probe=_fixed_probe,
    )

    assert created is True
    assert record.status == "queued"
    assert record.source_basename == "010124_013143"

    artifacts = list_media_artifacts(runtime_paths.db_file, record.session_uid)
    assert [(a.kind, a.role) for a in artifacts] == [
        ("raw_track", "host1"),
        ("raw_track", "host2"),
    ]
    assert artifacts[0].duration_seconds == pytest.approx(101.5)
    assert artifacts[1].duration_seconds == pytest.approx(99.25)
    assert artifacts[0].source_provider == "local_import"

    s3 = boto3.client("s3", region_name="us-east-1")
    for artifact in artifacts:
        _bucket, key = parse_s3_uri(artifact.s3_uri)
        stored = s3.get_object(Bucket=TEST_BUCKET, Key=key)["Body"].read()
        assert len(stored) == artifact.byte_size
        cached = cache_path_for(runtime_paths, artifact.sha256, artifact.filename)
        assert cached.exists()


def test_import_local_folder_is_idempotent_for_same_content(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    folder = _make_session_folder(tmp_path)

    first, created_first = import_local_folder(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        folder=folder,
        duration_probe=_fixed_probe,
    )
    second, created_second = import_local_folder(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        folder=folder,
        duration_probe=_fixed_probe,
    )

    assert created_first is True
    assert created_second is False
    assert first.session_uid == second.session_uid
    assert count_sessions(runtime_paths.db_file) == 1


def test_register_backfills_cozytrack_id_on_content_dedup(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    from podline.ingest import register_candidate_with_storage
    from podline.repository import get_session_by_cozytrack_id
    from podline.scanner import _parse_session_dir

    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    folder = _make_session_folder(tmp_path)

    imported, _created = import_local_folder(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        folder=folder,
        duration_probe=_fixed_probe,
    )
    assert imported.cozytrack_session_id is None

    candidate = _parse_session_dir(folder)
    deduped, created = register_candidate_with_storage(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        candidate=candidate,
        source_provider="cozytrack",
        cozytrack_session_id="ct-77",
        duration_probe=_fixed_probe,
    )

    assert created is False
    assert deduped.session_uid == imported.session_uid
    assert deduped.cozytrack_session_id == "ct-77"
    assert (
        get_session_by_cozytrack_id(runtime_paths.db_file, "ct-77").session_uid
        == imported.session_uid
    )

    # Same content under a second, different Cozytrack session must fail
    # loudly rather than silently re-listing forever.
    with pytest.raises(IngestError, match="refusing to also associate"):
        register_candidate_with_storage(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            candidate=candidate,
            source_provider="cozytrack",
            cozytrack_session_id="ct-88",
            duration_probe=_fixed_probe,
        )


def test_import_refuses_when_storage_disabled(
    app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path, storage_enabled=False)
    runtime_paths = runtime_factory(config)
    folder = _make_session_folder(tmp_path)

    with pytest.raises(IngestError, match="storage.enabled is false"):
        import_local_folder(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            folder=folder,
            duration_probe=_fixed_probe,
        )
    assert count_sessions(runtime_paths.db_file) == 0


def test_import_rejects_missing_and_unsupported_folders(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)

    with pytest.raises(IngestError, match="does not exist"):
        import_local_folder(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            folder=tmp_path / "nope",
            duration_probe=_fixed_probe,
        )

    single_track = tmp_path / "recordings" / "solo"
    single_track.mkdir(parents=True)
    (single_track / "solo_Tr1.WAV").write_bytes(b"only-one-track")
    with pytest.raises(IngestError, match="Unsupported session"):
        import_local_folder(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            folder=single_track,
            duration_probe=_fixed_probe,
        )


def test_import_fixture_sessions_end_to_end(
    moto_s3, app_config_factory, runtime_factory, fixture_raw_root, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)

    imported = []
    for folder in sorted(fixture_raw_root.iterdir()):
        if not folder.is_dir():
            continue
        try:
            record, created = import_local_folder(
                config=config,
                runtime_paths=runtime_paths,
                db_file=runtime_paths.db_file,
                folder=folder,
                duration_probe=_fixed_probe,
            )
        except IngestError:
            continue
        if created:
            imported.append(record)

    sessions = list_sessions(runtime_paths.db_file)
    assert {session.source_basename for session in sessions} == {
        "010124_013143",
        "010724_013643",
    }
    assert all(session.recorded_at is not None for session in sessions)
    assert len(imported) == 2
