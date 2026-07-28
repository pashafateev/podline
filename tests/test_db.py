import sqlite3
from pathlib import Path

from podline.db import initialize_database


def test_initialize_database_creates_file(tmp_path: Path) -> None:
    db_file = tmp_path / "state" / "state.db"

    initialize_database(str(db_file))

    assert db_file.exists()


def test_initialize_database_applies_session_metadata_migrations(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "state" / "state.db"
    db_file.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_file)
    connection.execute(
        """
        CREATE TABLE sessions (
            session_uid TEXT PRIMARY KEY,
            source_basename TEXT,
            source_date_folder TEXT,
            source_path TEXT NOT NULL,
            recorded_at TEXT,
            recorded_at_source TEXT,
            fingerprint TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            failure_reason TEXT,
            failed_at_stage TEXT,
            auphonic_production_id TEXT,
            pre_edit_master_path TEXT,
            transcript_path TEXT,
            final_master_path TEXT,
            cozytrack_session_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()

    initialize_database(str(db_file))

    connection = sqlite3.connect(db_file)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
    }
    connection.close()

    assert "source_basename" in columns
    assert "source_date_folder" in columns
    assert "recorded_at" in columns
    assert "recorded_at_source" in columns
    assert "transcript_refresh_production_id" in columns
    assert "transcript_refresh_source_sha256" in columns
    assert "auphonic_offsets_verified_production_id" in columns
    assert "transcript_refreshed_from_sha256" in columns


def test_initialize_database_creates_media_artifacts_table(tmp_path: Path) -> None:
    db_file = tmp_path / "state" / "state.db"

    initialize_database(str(db_file))

    connection = sqlite3.connect(db_file)
    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(media_artifacts)"
        ).fetchall()
    }
    indexes = {
        row[1] for row in connection.execute("PRAGMA index_list(media_artifacts)")
    }
    connection.close()

    assert {
        "session_uid",
        "kind",
        "role",
        "filename",
        "content_type",
        "byte_size",
        "sha256",
        "duration_seconds",
        "s3_uri",
        "source_provider",
        "source_id",
        "created_at",
        "updated_at",
    } <= columns
    assert "idx_media_artifacts_session_kind_role" in indexes
