import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
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
    auphonic_offsets_verified_production_id TEXT,
    transcript_refresh_production_id TEXT,
    transcript_refreshed_from_sha256 TEXT,
    pre_edit_master_path TEXT,
    transcript_path TEXT,
    final_master_path TEXT,
    cozytrack_session_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    session_uid TEXT NOT NULL,
    role TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    local_path TEXT NOT NULL,
    FOREIGN KEY(session_uid) REFERENCES sessions(session_uid)
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    session_uid TEXT NOT NULL UNIQUE,
    episode_number INTEGER UNIQUE,
    podbean_post_id TEXT,
    draft_url TEXT,
    title TEXT,
    description TEXT,
    FOREIGN KEY(session_uid) REFERENCES sessions(session_uid)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    session_uid TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    detail TEXT,
    FOREIGN KEY(session_uid) REFERENCES sessions(session_uid)
);

CREATE TABLE IF NOT EXISTS media_artifacts (
    id INTEGER PRIMARY KEY,
    session_uid TEXT NOT NULL,
    kind TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    duration_seconds REAL,
    s3_uri TEXT NOT NULL,
    source_provider TEXT,
    source_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(session_uid) REFERENCES sessions(session_uid)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_media_artifacts_session_kind_role
    ON media_artifacts(session_uid, kind, role);
"""


def connect(db_file: str) -> sqlite3.Connection:
    Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_file)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database(db_file: str) -> None:
    with connect(db_file) as connection:
        connection.executescript(SCHEMA)
        _apply_migrations(connection)
        connection.commit()


def _apply_migrations(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
    }
    migrations = {
        "source_basename": "ALTER TABLE sessions ADD COLUMN source_basename TEXT",
        "source_date_folder": "ALTER TABLE sessions ADD COLUMN source_date_folder TEXT",
        "recorded_at": "ALTER TABLE sessions ADD COLUMN recorded_at TEXT",
        "recorded_at_source": "ALTER TABLE sessions ADD COLUMN recorded_at_source TEXT",
        "auphonic_offsets_verified_production_id": "ALTER TABLE sessions ADD COLUMN auphonic_offsets_verified_production_id TEXT",
        "transcript_refresh_production_id": "ALTER TABLE sessions ADD COLUMN transcript_refresh_production_id TEXT",
        "transcript_refreshed_from_sha256": "ALTER TABLE sessions ADD COLUMN transcript_refreshed_from_sha256 TEXT",
        "pre_edit_master_path": "ALTER TABLE sessions ADD COLUMN pre_edit_master_path TEXT",
        "transcript_path": "ALTER TABLE sessions ADD COLUMN transcript_path TEXT",
        "final_master_path": "ALTER TABLE sessions ADD COLUMN final_master_path TEXT",
        "cozytrack_session_id": "ALTER TABLE sessions ADD COLUMN cozytrack_session_id TEXT",
    }

    for column_name, statement in migrations.items():
        if column_name not in existing_columns:
            connection.execute(statement)

    # SQLite cannot drop a column-level UNIQUE constraint without a full table
    # rewrite; existing DBs that were initialized with the prior schema may keep
    # the redundant constraint. Fresh DBs use only this partial index.
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_cozytrack_session_id "
        "ON sessions(cozytrack_session_id) WHERE cozytrack_session_id IS NOT NULL"
    )

    if "recorder_name" in existing_columns:
        connection.execute(
            "UPDATE sessions SET source_basename = recorder_name WHERE source_basename IS NULL"
        )
