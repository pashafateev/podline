"""Repository ownership regressions: every pre-edit operation has one scope."""

from dataclasses import replace
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from podline import repository as repo
from podline.db import connect, initialize_database
from podline.models import MediaArtifactRecord


def artifact_spec(role="host1", *, marker="original", source_id=None):
    return dict(
        role=role,
        filename=f"{marker}.webm",
        content_type="audio/webm",
        byte_size=12,
        sha256=marker,
        duration_seconds=None,
        s3_uri=f"s3://test-podline-media/{marker}.webm",
        source_provider="cozytrack",
        source_id=source_id,
    )


def register(db_file, session_uid, *, kind="raw_track", part_id=None, **kwargs):
    return repo.register_media_artifact(
        db_file,
        session_uid=session_uid,
        part_id=part_id,
        kind=kind,
        timestamp="first",
        **artifact_spec(**kwargs),
    )


@pytest.fixture()
def owned_parts(tmp_path, session_factory):
    db_file = str(tmp_path / "parts.db")
    initialize_database(db_file)
    session_factory(db_file, session_uid="episode")
    first = repo.ensure_default_part(db_file, "episode")
    second = repo.create_recording_part(
        db_file, session_uid="episode", part_number=2, take_id="take-2"
    )
    session_factory(db_file, session_uid="other")
    foreign = repo.ensure_default_part(db_file, "other")
    return db_file, first, second, foreign


def test_record_keeps_old_positional_constructor_compatible():
    record = MediaArtifactRecord(
        "episode", "transcript", "", "ep.vtt", "text/vtt", 8, "hash", None,
        "s3://test/ep.vtt", "auphonic", "production", "first", "first",
    )
    assert record.part_id is None
    assert replace(record, part_id="part").part_id == "part"


def test_parts_have_stable_identity_metadata_and_immutable_order(owned_parts):
    db_file, first, second, _foreign = owned_parts
    with pytest.raises(ValueError, match="part"):
        repo.ensure_default_part(db_file, "episode")
    assert repo.get_recording_part(db_file, second.part_id) == second
    assert repo.get_recording_part(db_file, "missing") is None
    assert repo.list_recording_parts(db_file, "episode") == [first, second]
    snapshot = {"tracks": [{"id": "track-2", "participantId": "host", "role": "host1"}]}
    warnings = [{"track_id": "track-2", "reason": "multiple_segments"}]
    updated = repo.update_recording_part(
        db_file, second.part_id, take_id="take-new", status="prepared",
        auphonic_production_id="prod-2", ingest_complete=False,
        source_snapshot=snapshot, timing_warnings=warnings,
        started_at="start", stopped_at="stop", take_status="stopped",
        output_basename="part-02", operation="create", failure_reason="interrupted",
    )
    assert updated.part_id == second.part_id
    assert updated.source_snapshot == snapshot
    assert updated.timing_warnings == warnings
    assert updated.ingest_complete is False
    assert (updated.started_at, updated.stopped_at, updated.take_status) == (
        "start", "stop", "stopped",
    )
    assert (updated.status, updated.auphonic_production_id) == ("prepared", "prod-2")
    assert updated.output_basename == "part-02"
    assert updated.failure_reason == "interrupted"
    assert updated.operation == "create"
    cleared = repo.update_recording_part(
        db_file, second.part_id, operation=None, failure_reason=None
    )
    assert cleared.operation is None and cleared.failure_reason is None
    assert repo.get_recording_part(db_file, first.part_id) == first
    for fields in ({"part_number": 4}, {"session_uid": "other"}, {"part_id": "new"}):
        with pytest.raises((ValueError, TypeError)):
            repo.update_recording_part(db_file, second.part_id, **fields)


@pytest.mark.parametrize("number", [0, -1, 1.5, True])
def test_invalid_part_numbers_are_rejected(tmp_path, session_factory, number):
    db_file = str(tmp_path / "parts.db")
    initialize_database(db_file)
    session_factory(db_file)
    with pytest.raises(ValueError, match="part_number"):
        repo.create_recording_part(
            db_file, session_uid="session-test-1", part_number=number
        )


def test_parts_enforce_parent_number_and_take_uniqueness(owned_parts):
    db_file, _first, _second, _foreign = owned_parts
    for kwargs in (
        dict(session_uid="episode", part_number=2),
        dict(session_uid="episode", part_number=3, take_id="take-2"),
    ):
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            repo.create_recording_part(db_file, **kwargs)
    with pytest.raises(ValueError, match="session"):
        repo.create_recording_part(db_file, session_uid="absent", part_number=1)
    with pytest.raises(ValueError, match="part"):
        repo.update_recording_part(db_file, "absent", status="failed")


def test_create_part_round_trips_requested_ingest_metadata(tmp_path, session_factory):
    db_file = str(tmp_path / "parts.db")
    initialize_database(db_file)
    session_factory(db_file)
    created = repo.create_recording_part(
        db_file, session_uid="session-test-1", part_number=1,
        take_id="take", source_snapshot={"tracks": []}, started_at="start",
        stopped_at="stop", take_status="stopped", status="ingest_pending",
        ingest_complete=False, output_basename="part-01",
    )
    assert created.take_id == "take"
    assert created.source_snapshot == {"tracks": []}
    assert created.started_at == "start" and created.stopped_at == "stop"
    assert created.take_status == "stopped"
    assert created.status == "ingest_pending"
    assert created.ingest_complete is False
    assert created.output_basename == "part-01"
    assert repo.ensure_default_part(db_file, "session-test-1") == created


def test_part_operation_claim_is_compare_and_set(owned_parts):
    db_file, first, _second, _foreign = owned_parts
    assert repo.claim_recording_part_operation(db_file, first.part_id, "creating")
    assert not repo.claim_recording_part_operation(db_file, first.part_id, "creating")
    assert not repo.claim_recording_part_operation(db_file, first.part_id, "starting")
    assert repo.get_recording_part(db_file, first.part_id).operation == "creating"
    repo.update_recording_part(db_file, first.part_id, operation=None)
    assert repo.claim_recording_part_operation(db_file, first.part_id, "starting")


def test_episode_participant_roles_persist_independently(owned_parts):
    db_file, _first, _second, _foreign = owned_parts
    assert repo.get_participant_roles(db_file, "episode") == {}
    roles = {"host-id": "host1", "guest-id": "guest2"}
    repo.set_participant_roles(db_file, "episode", roles)
    assert repo.get_participant_roles(db_file, "episode") == roles
    assert repo.get_participant_roles(db_file, "other") == {}
    assert repo.get_episode(db_file, "episode") is None
    with pytest.raises(ValueError, match="session"):
        repo.set_participant_roles(db_file, "absent", roles)


@pytest.mark.parametrize("kind", ["raw_track", "pre_edit_master", "pre_edit_stem", "transcript"])
def test_same_kind_role_is_independent_across_parts(owned_parts, kind):
    db_file, first, second, _foreign = owned_parts
    before = register(db_file, "episode", part_id=first.part_id, kind=kind, marker="one")
    register(db_file, "episode", part_id=second.part_id, kind=kind, marker="two")
    register(db_file, "episode", part_id=second.part_id, kind=kind, marker="new-two")
    assert repo.get_media_artifact(
        db_file, "episode", kind=kind, role="host1", part_id=first.part_id
    ) == before
    rows = repo.list_media_artifacts(db_file, "episode", kind=kind, part_id=second.part_id)
    assert [row.sha256 for row in rows] == ["new-two"]
    assert len(repo.list_media_artifacts(db_file, "episode")) == 2
    with connect(db_file) as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_artifacts").fetchone()[0] == 2
        indexes = {
            row["name"]: row["sql"]
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'media_artifacts'"
            )
        }
    assert "WHERE part_id IS NULL" in indexes["idx_media_artifacts_session_kind_role"]
    assert any("WHERE part_id IS NOT NULL" in sql for sql in indexes.values())


def test_episode_final_and_current_transcript_never_select_a_part(owned_parts):
    db_file, first, second, _foreign = owned_parts
    register(db_file, "episode", part_id=first.part_id, kind="transcript", marker="pre-one")
    register(db_file, "episode", part_id=second.part_id, kind="transcript", marker="pre-two")
    assert repo.get_media_artifact(db_file, "episode", kind="transcript", role="host1") is None
    final = register(db_file, "episode", kind="final_master", marker="final")
    current = register(db_file, "episode", kind="transcript", marker="refreshed")
    register(db_file, "episode", part_id=second.part_id, kind="transcript", marker="resync")
    assert repo.get_media_artifact(db_file, "episode", kind="transcript", role="host1") == current
    assert repo.get_media_artifact(db_file, "episode", kind="final_master", role="host1") == final
    assert current.part_id is None and final.part_id is None
    assert repo.list_media_artifacts(db_file, "episode", kind="transcript") == [current]
    assert len(repo.list_media_artifacts(db_file, "episode")) == 4
    assert len(repo.list_media_artifacts(db_file, "episode", part_id=first.part_id)) == 1
    with pytest.raises(ValueError, match="final_master"):
        register(db_file, "episode", kind="final_master", part_id=first.part_id)


def test_scoped_set_replace_delete_and_failed_replace_are_atomic(owned_parts):
    db_file, first, second, _foreign = owned_parts
    for part in (first, second):
        register(db_file, "episode", part_id=part.part_id, kind="pre_edit_stem", marker=part.part_id)
        register(db_file, "episode", part_id=part.part_id, kind="pre_edit_stem", role="guest1")
    before = repo.list_media_artifacts(db_file, "episode", part_id=first.part_id)
    rows = repo.replace_media_artifact_set(
        db_file, session_uid="episode", kind="pre_edit_stem", part_id=second.part_id,
        artifacts=[artifact_spec(marker="replacement")], timestamp="later",
    )
    assert [row.sha256 for row in rows] == ["replacement"]
    assert repo.list_media_artifacts(db_file, "episode", part_id=first.part_id) == before
    with pytest.raises((KeyError, TypeError)):
        repo.replace_media_artifact_set(
            db_file, session_uid="episode", kind="pre_edit_stem", part_id=first.part_id,
            artifacts=[{"role": "host1"}], timestamp="bad",
        )
    assert repo.list_media_artifacts(db_file, "episode", part_id=first.part_id) == before
    repo.delete_media_artifacts(
        db_file, session_uid="episode", kind="pre_edit_stem", part_id=second.part_id
    )
    assert repo.list_media_artifacts(db_file, "episode", part_id=second.part_id) == []
    assert repo.list_media_artifacts(db_file, "episode", part_id=first.part_id) == before


def test_scoped_role_duration_and_source_reconciliation(owned_parts):
    db_file, first, second, _foreign = owned_parts
    for part in (first, second):
        register(db_file, "episode", part_id=part.part_id, marker="same-content")
        register(
            db_file, "episode", part_id=part.part_id, role="guest1",
            marker="guest-content", source_id="do-not-replace",
        )
    with connect(db_file) as connection:
        connection.execute(
            "INSERT INTO assets(session_uid, role, filename, file_size, local_path) "
            "VALUES ('episode', 'host1', 'legacy.webm', 1, '/legacy')"
        )
    before = repo.list_media_artifacts(db_file, "episode", part_id=first.part_id)
    assert repo.backfill_artifact_source_ids(
        db_file, session_uid="episode", part_id=second.part_id,
        source_ids_by_sha256={"same-content": "track-two", "guest-content": "wrong"},
        timestamp="backfill",
    ) == 1
    repo.update_media_artifact_duration(
        db_file, session_uid="episode", kind="raw_track", role="host1",
        part_id=second.part_id, duration_seconds=12.5, timestamp="duration",
    )
    repo.reconcile_artifact_roles(
        db_file, session_uid="episode", part_id=second.part_id, status="queued",
        role_updates={"host1": "guest1", "guest1": "host1"}, timestamp="roles",
        detail="scoped role swap",
    )
    selected = {row.role: row for row in repo.list_media_artifacts(
        db_file, "episode", kind="raw_track", part_id=second.part_id
    )}
    assert selected["guest1"].source_id == "track-two"
    assert selected["guest1"].duration_seconds == 12.5
    assert selected["host1"].source_id == "do-not-replace"
    assert repo.list_media_artifacts(db_file, "episode", part_id=first.part_id) == before
    assert repo.list_assets_for_session(db_file, "episode")[0].role == "host1"


def test_cozytrack_association_backfill_is_scoped_and_atomic(owned_parts):
    db_file, first, second, _foreign = owned_parts
    for part in (first, second):
        register(db_file, "episode", part_id=part.part_id, marker="identical")
    assert repo.set_cozytrack_session_id(
        db_file, session_uid="episode", part_id=second.part_id,
        cozytrack_session_id="ct", status="queued", timestamp="now",
        source_ids_by_sha256={"identical": "two"},
    )
    assert repo.get_media_artifact(
        db_file, "episode", kind="raw_track", role="host1", part_id=first.part_id
    ).source_id is None
    assert repo.get_media_artifact(
        db_file, "episode", kind="raw_track", role="host1", part_id=second.part_id
    ).source_id == "two"
    assert not repo.set_cozytrack_session_id(
        db_file, session_uid="episode", part_id=first.part_id,
        cozytrack_session_id="new", status="queued", timestamp="later",
        source_ids_by_sha256={"identical": "one"},
    )


def ownership_operations(db_file, part_id):
    return [
        lambda: register(db_file, "episode", part_id=part_id),
        lambda: repo.get_media_artifact(db_file, "episode", kind="raw_track", part_id=part_id),
        lambda: repo.list_media_artifacts(db_file, "episode", part_id=part_id),
        lambda: repo.replace_media_artifact_set(
            db_file, session_uid="episode", kind="pre_edit_stem", part_id=part_id,
            artifacts=[artifact_spec()], timestamp="now",
        ),
        lambda: repo.delete_media_artifacts(
            db_file, session_uid="episode", kind="raw_track", part_id=part_id
        ),
        lambda: repo.update_media_artifact_duration(
            db_file, session_uid="episode", kind="raw_track", role="host1",
            part_id=part_id, duration_seconds=1.0, timestamp="now",
        ),
        lambda: repo.reconcile_artifact_roles(
            db_file, session_uid="episode", status="queued", role_updates={},
            part_id=part_id, timestamp="now", detail="no-op",
        ),
        lambda: repo.backfill_artifact_source_ids(
            db_file, session_uid="episode", part_id=part_id,
            source_ids_by_sha256={}, timestamp="now",
        ),
        lambda: repo.set_cozytrack_session_id(
            db_file, session_uid="episode", part_id=part_id,
            cozytrack_session_id="ct", status="queued", timestamp="now",
        ),
    ]


@pytest.mark.parametrize("index", range(9))
def test_every_operation_rejects_cross_episode_or_unknown_part(owned_parts, index):
    db_file, first, _second, foreign = owned_parts
    register(db_file, "episode", part_id=first.part_id)
    before = repo.list_media_artifacts(db_file, "episode")
    for invalid in (foreign.part_id, "absent"):
        with pytest.raises(ValueError, match="part|Part"):
            ownership_operations(db_file, invalid)[index]()
    assert repo.list_media_artifacts(db_file, "episode") == before
    assert repo.get_session(db_file, "episode").cozytrack_session_id is None


@pytest.mark.parametrize("index", [0, 1, 3, 4, 5, 6, 7, 8])
def test_multipart_omission_fails_instead_of_first_match(owned_parts, index):
    db_file, first, _second, _foreign = owned_parts
    register(db_file, "episode", part_id=first.part_id)
    with pytest.raises(ValueError, match="part"):
        ownership_operations(db_file, None)[index]()


def test_new_session_creation_and_lazy_single_part_registration(tmp_path, session_factory):
    db_file = str(tmp_path / "parts.db")
    initialize_database(db_file)
    candidate = SimpleNamespace(
        source_basename="original", date_folder_name="2026-01-01",
        session_path=Path("/not-read"), recorded_at=None, recorded_at_source=None,
        spoken_tracks=[],
    )
    repo.create_session(
        db_file, session_uid="scan", fingerprint="scan", candidate=candidate,
        timestamp="original-time",
    )
    default = repo.list_recording_parts(db_file, "scan")
    assert len(default) == 1
    assert repo.ensure_default_part(db_file, "scan") == default[0]
    repo.create_session_with_artifacts(
        db_file, session_uid="local", fingerprint="local", candidate=candidate,
        timestamp="original-time", artifacts=[dict(kind="raw_track", **artifact_spec())],
    )
    local = repo.ensure_default_part(db_file, "local")
    assert local.ingest_complete is True
    assert repo.list_media_artifacts(db_file, "local")[0].part_id == local.part_id
    session_factory(db_file, session_uid="lazy")
    assert repo.list_recording_parts(db_file, "lazy") == []
    raw = register(db_file, "lazy")
    assert raw.part_id == repo.ensure_default_part(db_file, "lazy").part_id
    assert repo.get_media_artifact(db_file, "lazy", kind="raw_track", role="host1") == raw
    assert register(db_file, "orphan").part_id is None


@pytest.mark.parametrize(
    "session_status,part_status",
    [
        ("auphonic_prepared", "prepared"),
        ("auphonic_previewed", "previewed"),
        ("auphonic_started", "started"),
        ("waiting_for_finalize", "complete"),
        ("finalize_started", "complete"),
        ("ready_for_draft", "complete"),
        ("draft_created", "complete"),
        ("failed", "failed"),
    ],
)
def test_legacy_session_status_projects_to_the_sole_unsnapshotted_part(
    tmp_path, session_factory, session_status, part_status,
):
    db_file = str(tmp_path / "legacy.db")
    initialize_database(db_file)
    session_factory(db_file)
    part = repo.ensure_default_part(db_file, "session-test-1")
    repo.update_session_status(
        db_file, session_uid="session-test-1", from_state="queued", to_state=session_status,
        timestamp="transition", detail="legacy workflow", auphonic_production_id="pre-prod",
        failure_reason="failed operation" if session_status == "failed" else None,
    )
    updated = repo.get_recording_part(db_file, part.part_id)
    assert updated.status == part_status
    assert updated.auphonic_production_id == "pre-prod"
    assert updated.updated_at == "transition"
    assert updated.failure_reason == ("failed operation" if session_status == "failed" else None)


def test_legacy_reset_clears_only_the_compatibility_part_production(tmp_path, session_factory):
    db_file = str(tmp_path / "legacy.db")
    initialize_database(db_file)
    session_factory(db_file, status="auphonic_prepared", auphonic_production_id="pre-prod")
    part = repo.ensure_default_part(db_file, "session-test-1")
    repo.reset_session_to_queued(
        db_file, session_uid="session-test-1", from_state="auphonic_prepared",
        timestamp="reset", detail="operator reset",
    )
    updated = repo.get_recording_part(db_file, part.part_id)
    assert updated.status == "queued"
    assert updated.auphonic_production_id is None


def test_session_projection_does_not_rewrite_snapshot_owned_or_multiple_parts(owned_parts):
    db_file, first, second, foreign = owned_parts
    repo.update_recording_part(
        db_file, foreign.part_id, source_snapshot={"tracks": []},
        status="prepared", auphonic_production_id="owned-production",
    )
    before = repo.get_recording_part(db_file, foreign.part_id)
    for session_uid in ("episode", "other"):
        repo.update_session_status(
            db_file, session_uid=session_uid, from_state="queued", to_state="failed",
            auphonic_production_id="not-a-part-production", timestamp="later",
            detail="episode aggregate change",
        )
    assert repo.get_recording_part(db_file, first.part_id) == first
    assert repo.get_recording_part(db_file, second.part_id) == second
    assert repo.get_recording_part(db_file, foreign.part_id) == before


def test_concurrent_default_creation_and_operation_claim_have_one_winner(tmp_path, session_factory):
    from concurrent.futures import ThreadPoolExecutor

    db_file = str(tmp_path / "concurrent.db")
    initialize_database(db_file)
    session_factory(db_file)
    with ThreadPoolExecutor(max_workers=2) as workers:
        parts = list(workers.map(
            lambda _: repo.ensure_default_part(db_file, "session-test-1"), range(2)
        ))
    assert parts[0] == parts[1]
    with ThreadPoolExecutor(max_workers=2) as workers:
        claimed = list(workers.map(
            lambda _: repo.claim_recording_part_operation(db_file, parts[0].part_id, "creating"),
            range(2),
        ))
    assert sorted(claimed) == [False, True]
