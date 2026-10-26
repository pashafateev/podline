"""Storage helpers preserve content-addressed keys and validate ownership first."""

import pytest

from podline import repository as repo
from podline.artifacts import (
    clear_cache,
    materialize_artifact,
    store_artifact_file,
    store_artifact_files,
)
from podline.storage import MediaStorage


@pytest.fixture()
def artifact_parts(tmp_path, app_config_factory, runtime_factory, session_factory):
    config = app_config_factory(tmp_path)
    paths = runtime_factory(config)
    session_factory(paths.db_file, session_uid="episode")
    first = repo.ensure_default_part(paths.db_file, "episode")
    second = repo.create_recording_part(
        paths.db_file, session_uid="episode", part_number=2, take_id="take-2"
    )
    session_factory(paths.db_file, session_uid="other")
    foreign = repo.ensure_default_part(paths.db_file, "other")
    return config, paths, first, second, foreign


def test_store_and_materialize_parts_with_same_filename_preserves_keys_and_cache(
    artifact_parts, tmp_path, moto_s3, monkeypatch,
):
    config, paths, first, second, _foreign = artifact_parts
    records = []
    for index, part in enumerate((first, second), 1):
        source = tmp_path / str(index) / "host1.webm"
        source.parent.mkdir()
        source.write_bytes(f"part {index}".encode())
        record = store_artifact_file(
            config=config, runtime_paths=paths, db_file=paths.db_file,
            session_uid="episode", kind="raw_track", role="host1", part_id=part.part_id,
            source_path=source, content_type="audio/webm",
        )
        assert record.s3_uri.endswith(MediaStorage(config.storage).key_for(
            session_uid="episode", kind="raw_track", sha256=record.sha256, filename=source.name
        ))
        records.append(record)
    assert records[0].part_id != records[1].part_id
    assert records[0].s3_uri != records[1].s3_uri
    clear_cache(paths)
    for index, part in enumerate((first, second), 1):
        materialized = materialize_artifact(
            config=config, runtime_paths=paths, db_file=paths.db_file,
            session_uid="episode", kind="raw_track", role="host1", part_id=part.part_id,
        )
        assert materialized.read_bytes() == f"part {index}".encode()
    monkeypatch.setattr(
        "podline.storage.MediaStorage.download_to_path",
        lambda *args, **kwargs: pytest.fail("verified cache must be reused"),
    )
    for part in (first, second):
        assert materialize_artifact(
            config=config, runtime_paths=paths, db_file=paths.db_file,
            session_uid="episode", kind="raw_track", role="host1", part_id=part.part_id,
        ).exists()


def test_complete_set_storage_only_replaces_selected_part(
    artifact_parts, tmp_path, moto_s3,
):
    config, paths, first, second, _foreign = artifact_parts
    for part in (first, second):
        for roles in (["host1", "guest1"], ["host1"]):
            files = []
            for role in roles:
                source = tmp_path / part.part_id / f"{role}.flac"
                source.parent.mkdir(exist_ok=True)
                source.write_bytes(f"{part.part_id} {role}".encode())
                files.append({"role": role, "source_path": source})
            records = store_artifact_files(
                config=config, runtime_paths=paths, db_file=paths.db_file,
                session_uid="episode", part_id=part.part_id, kind="pre_edit_stem",
                files=files, expected_roles=roles,
            )
            assert {record.part_id for record in records} == {part.part_id}
            if part == first:
                break
    assert {row.role for row in repo.list_media_artifacts(
        paths.db_file, "episode", kind="pre_edit_stem", part_id=first.part_id
    )} == {"host1", "guest1"}
    assert {row.role for row in repo.list_media_artifacts(
        paths.db_file, "episode", kind="pre_edit_stem", part_id=second.part_id
    )} == {"host1"}


@pytest.mark.parametrize("operation", ["single", "set", "materialize"])
@pytest.mark.parametrize("selector", ["foreign", "unknown", "omitted"])
def test_storage_ownership_fails_before_any_remote_call(
    artifact_parts, tmp_path, operation, selector,
):
    config, paths, _first, _second, foreign = artifact_parts
    source = tmp_path / "source.webm"
    source.write_bytes(b"never uploaded")

    class NoStorage:
        def __getattr__(self, name):
            pytest.fail(f"Invalid ownership accessed storage.{name}")

    part_id = {"foreign": foreign.part_id, "unknown": "absent", "omitted": None}[selector]
    common = dict(
        config=config, runtime_paths=paths, db_file=paths.db_file,
        session_uid="episode", kind="raw_track", part_id=part_id, storage=NoStorage(),
    )
    with pytest.raises(ValueError, match="part|Part"):
        if operation == "single":
            store_artifact_file(source_path=source, content_type="audio/webm", **common)
        elif operation == "set":
            store_artifact_files(
                files=[{"role": "host1", "source_path": source}],
                expected_roles=["host1"], **common,
            )
        else:
            materialize_artifact(role="host1", **common)
