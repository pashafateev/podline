"""Initialize actual pre-part SQLite schemas twice without touching any service."""

from pathlib import Path
import socket
import sqlite3

import pytest

from podline import repository as repo
from podline.db import connect, initialize_database


LEGACY_SCHEMA = """
CREATE TABLE sessions (
    session_uid TEXT PRIMARY KEY, source_basename TEXT, source_date_folder TEXT,
    source_path TEXT NOT NULL, recorded_at TEXT, recorded_at_source TEXT,
    fingerprint TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
    failure_reason TEXT, failed_at_stage TEXT, auphonic_production_id TEXT,
    transcript_refresh_production_id TEXT, pre_edit_master_path TEXT,
    transcript_path TEXT, final_master_path TEXT, cozytrack_session_id TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE media_artifacts (
    id INTEGER PRIMARY KEY, session_uid TEXT NOT NULL, kind TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL, content_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL, sha256 TEXT NOT NULL, duration_seconds REAL,
    s3_uri TEXT NOT NULL, source_provider TEXT, source_id TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_media_artifacts_session_kind_role
    ON media_artifacts(session_uid, kind, role);
CREATE TABLE episodes (
    id INTEGER PRIMARY KEY, session_uid TEXT NOT NULL UNIQUE, episode_number INTEGER UNIQUE,
    podbean_post_id TEXT, draft_url TEXT, title TEXT, description TEXT
);
"""


def legacy_database(tmp_path, *, status="queued", transcript_source="pre-prod",
                    refresh=None, artifacts=True):
    db_file = str(tmp_path / "legacy.db")
    with sqlite3.connect(db_file) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            "INSERT INTO sessions VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy", "recorder-name", "date", "/old-drive/path", "recorded",
             "recorder", "unchanged-fingerprint", status,
             "failure" if status == "failed" else None,
             "auphonic" if status == "failed" else None,
             "pre-prod" if status != "queued" else None, refresh,
             "/workspace/legacy_pre_edit.mp3", "/workspace/legacy.vtt",
             "/workspace/edited.wav" if status in ("ready_for_draft", "draft_created") else None,
             "cozytrack-original", "created", "updated"),
        )
        if artifacts:
            for index, (kind, role, filename, source_id) in enumerate((
                ("raw_track", "host1", "untouched-source.webm", "source-123"),
                ("pre_edit_master", "", "legacy_pre_edit.mp3", "pre-prod"),
                ("pre_edit_stem", "host1", "Jonathan.flac", "pre-prod"),
                ("transcript", "", "legacy.vtt", transcript_source),
                ("final_master", "", "edited.wav", None),
            ), 1):
                connection.execute(
                    "INSERT INTO media_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (index, "legacy", kind, role, filename, "original/type", 123, f"hash-{index}",
                     12.5, f"s3://test/original/{filename}",
                     "cozytrack" if kind == "raw_track" else "auphonic",
                     source_id, "created", "updated"),
                )
        connection.execute(
            "INSERT INTO episodes VALUES (1, 'legacy', 321, 'post', 'draft-url', 'title', 'description')"
        )
    return db_file


def database_snapshot(db_file):
    with connect(db_file) as connection:
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("sessions", "media_artifacts", "episodes")
        }


@pytest.fixture()
def no_services(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Migration must not access network, storage or subprocesses")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("podline.storage.MediaStorage.__init__", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)


@pytest.mark.parametrize(
    "status,part_status,transcript_source,refresh,has_artifacts",
    [
        ("queued", "queued", None, None, True),
        ("auphonic_prepared", "prepared", "pre-prod", None, True),
        ("auphonic_previewed", "previewed", "pre-prod", None, True),
        ("auphonic_started", "started", "pre-prod", None, True),
        ("failed", "failed", "pre-prod", None, True),
        ("waiting_for_finalize", "complete", "pre-prod", None, True),
        ("finalize_started", "complete", "pre-prod", None, True),
        ("ready_for_draft", "complete", "pre-prod", "refresh-in-flight", True),
        ("ready_for_draft", "complete", "refresh-in-flight", "refresh-in-flight", True),
        ("ready_for_draft", "complete", "completed-refresh", None, True),
        ("draft_created", "complete", "completed-refresh", None, True),
        ("queued", "queued", None, None, False),
        ("ready_for_draft", "complete", None, None, False),
    ],
)
def test_migrate_legacy_states_twice_preserves_identity_provenance_and_progress(
    tmp_path, no_services, status, part_status, transcript_source, refresh, has_artifacts,
):
    db_file = legacy_database(
        tmp_path, status=status, transcript_source=transcript_source,
        refresh=refresh, artifacts=has_artifacts,
    )
    before = database_snapshot(db_file)
    initialize_database(db_file)
    first = database_snapshot(db_file)
    parts = repo.list_recording_parts(db_file, "legacy")
    assert len(parts) == 1
    part = parts[0]
    assert part.session_uid == "legacy" and part.part_number == 1
    assert part.take_id is None and part.operation is None
    assert part.status == part_status
    assert part.ingest_complete is has_artifacts
    assert part.auphonic_production_id == before["sessions"][0]["auphonic_production_id"]
    assert part.failure_reason == before["sessions"][0]["failure_reason"]
    assert part.source_snapshot == {} and part.timing_warnings == []
    assert part.output_basename == "legacy"
    assert part.created_at == "created"
    assert repo.get_participant_roles(db_file, "legacy") == {}
    after_session = {key: first["sessions"][0][key] for key in before["sessions"][0]}
    assert after_session == before["sessions"][0]
    assert first["episodes"] == before["episodes"]
    by_id = {row["id"]: row for row in first["media_artifacts"]}
    for original in before["media_artifacts"]:
        assert {key: by_id[original["id"]][key] for key in original} == original
        owner = part.part_id if original["kind"] in (
            "raw_track", "pre_edit_master", "pre_edit_stem"
        ) else None
        assert by_id[original["id"]]["part_id"] == owner
    pre_transcript = repo.get_media_artifact(
        db_file, "legacy", kind="transcript", part_id=part.part_id
    )
    if has_artifacts and transcript_source == "pre-prod" and status != "queued":
        assert pre_transcript is not None
        original = before["media_artifacts"][3]
        for field in ("filename", "sha256", "s3_uri", "source_id", "created_at", "updated_at"):
            assert getattr(pre_transcript, field) == original[field]
    else:
        assert pre_transcript is None
    if has_artifacts:
        assert repo.get_media_artifact(db_file, "legacy", kind="transcript").source_id == transcript_source
        assert repo.get_media_artifact(db_file, "legacy", kind="final_master").part_id is None
    else:
        assert repo.list_media_artifacts(db_file, "legacy") == []
    initialize_database(db_file)
    assert database_snapshot(db_file) == first
    assert repo.list_recording_parts(db_file, "legacy") == parts
    assert list(tmp_path.iterdir()) == [Path(db_file)]


def test_migration_does_not_recreate_preedit_transcript_after_final_refresh(
    tmp_path, no_services,
):
    db_file = legacy_database(tmp_path, status="waiting_for_finalize")
    initialize_database(db_file)
    part = repo.list_recording_parts(db_file, "legacy")[0]
    pre = repo.get_media_artifact(db_file, "legacy", kind="transcript", part_id=part.part_id)
    with connect(db_file) as connection:
        connection.execute(
            "UPDATE media_artifacts SET source_id='final-refresh', sha256='final-hash' "
            "WHERE kind='transcript' AND part_id IS NULL"
        )
        connection.execute("UPDATE recording_parts SET operation='creating'")
    initialize_database(db_file)
    assert repo.get_media_artifact(
        db_file, "legacy", kind="transcript", part_id=part.part_id
    ) == pre
    assert repo.get_media_artifact(
        db_file, "legacy", kind="transcript"
    ).source_id == "final-refresh"
    assert repo.get_recording_part(db_file, part.part_id).operation == "creating"


def test_migration_rolls_back_schema_indexes_and_data_as_one_transaction(tmp_path):
    db_file = legacy_database(tmp_path)
    before = database_snapshot(db_file)
    with connect(db_file) as connection:
        connection.execute(
            "CREATE TRIGGER refuse_backfill BEFORE UPDATE ON media_artifacts "
            "BEGIN SELECT RAISE(ABORT, 'backfill refused'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="backfill refused"):
        initialize_database(db_file)
    assert database_snapshot(db_file) == before
    with connect(db_file) as connection:
        assert "part_id" not in {
            row["name"] for row in connection.execute("PRAGMA table_info(media_artifacts)")
        }
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='recording_parts'"
        ).fetchone() is None
        assert "WHERE" not in connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_media_artifacts_session_kind_role'"
        ).fetchone()["sql"]
