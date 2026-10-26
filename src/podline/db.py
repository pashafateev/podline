import sqlite3
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

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
    transcript_refresh_production_id TEXT,
    pre_edit_master_path TEXT,
    transcript_path TEXT,
    final_master_path TEXT,
    cozytrack_session_id TEXT,
    participant_roles TEXT NOT NULL DEFAULT '{}',
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

CREATE TABLE IF NOT EXISTS recording_parts (
    part_id TEXT PRIMARY KEY,
    session_uid TEXT NOT NULL,
    part_number INTEGER NOT NULL CHECK(part_number > 0),
    take_id TEXT,
    status TEXT NOT NULL,
    auphonic_production_id TEXT,
    ingest_complete INTEGER NOT NULL DEFAULT 1 CHECK(ingest_complete IN (0, 1)),
    source_snapshot TEXT NOT NULL DEFAULT '{}',
    timing_warnings TEXT NOT NULL DEFAULT '[]',
    started_at TEXT,
    stopped_at TEXT,
    take_status TEXT,
    output_basename TEXT,
    operation TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(session_uid, part_number),
    FOREIGN KEY(session_uid) REFERENCES sessions(session_uid)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_recording_parts_session_take
    ON recording_parts(session_uid, take_id) WHERE take_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS media_artifacts (
    id INTEGER PRIMARY KEY,
    session_uid TEXT NOT NULL,
    part_id TEXT,
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
    FOREIGN KEY(session_uid) REFERENCES sessions(session_uid),
    FOREIGN KEY(part_id) REFERENCES recording_parts(part_id)
);
"""


def connect(db_file: str) -> sqlite3.Connection:
    Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_file)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database(db_file: str) -> None:
    with connect(db_file) as connection:
        # executescript commits any pending transaction before running. Start
        # inside the script so schema, indexes, and backfill roll back together.
        connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
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
        "transcript_refresh_production_id": "ALTER TABLE sessions ADD COLUMN transcript_refresh_production_id TEXT",
        "pre_edit_master_path": "ALTER TABLE sessions ADD COLUMN pre_edit_master_path TEXT",
        "transcript_path": "ALTER TABLE sessions ADD COLUMN transcript_path TEXT",
        "final_master_path": "ALTER TABLE sessions ADD COLUMN final_master_path TEXT",
        "cozytrack_session_id": "ALTER TABLE sessions ADD COLUMN cozytrack_session_id TEXT",
        "participant_roles": "ALTER TABLE sessions ADD COLUMN participant_roles TEXT NOT NULL DEFAULT '{}'",
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

    artifact_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(media_artifacts)")
    }
    if "part_id" not in artifact_columns:
        connection.execute(
            "ALTER TABLE media_artifacts ADD COLUMN part_id TEXT "
            "REFERENCES recording_parts(part_id)"
        )
    old_index = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_media_artifacts_session_kind_role'"
    ).fetchone()
    if old_index is not None and "WHERE" not in old_index["sql"].upper():
        connection.execute("DROP INDEX idx_media_artifacts_session_kind_role")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_media_artifacts_session_kind_role "
        "ON media_artifacts(session_uid, kind, role) WHERE part_id IS NULL"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_media_artifacts_part_kind_role "
        "ON media_artifacts(session_uid, part_id, kind, role) WHERE part_id IS NOT NULL"
    )

    # Only unprojected sessions are legacy. Re-running must not change part
    # progress, resurrect invalidated artifacts, or alias a final refresh.
    sessions = connection.execute(
        "SELECT session_uid FROM sessions WHERE NOT EXISTS "
        "(SELECT 1 FROM recording_parts WHERE recording_parts.session_uid = sessions.session_uid)"
    ).fetchall()
    for session in sessions:
        ensure_default_recording_part(connection, session["session_uid"])


def recording_part_id(session_uid: str, part_number: int) -> str:
    """Deterministic opaque identity; neither take nor production IDs are keys."""
    return uuid5(NAMESPACE_URL, f"podline:recording-part:{session_uid}:{part_number}").hex


def recording_part_status(session_status: str) -> str:
    """The one-way compatibility projection from the legacy episode workflow."""
    return {
        "auphonic_prepared": "prepared",
        "auphonic_previewed": "previewed",
        "auphonic_started": "started",
        "waiting_for_finalize": "complete",
        "finalize_started": "complete",
        "ready_for_draft": "complete",
        "draft_created": "complete",
    }.get(session_status, session_status)


def ensure_default_recording_part(
    connection: sqlite3.Connection, session_uid: str, *, ingest_complete=None
) -> sqlite3.Row:
    """Project a legacy session once, using the caller's database transaction."""
    session = connection.execute(
        "SELECT * FROM sessions WHERE session_uid = ?", (session_uid,)
    ).fetchone()
    if session is None:
        raise ValueError(f"Unknown session {session_uid}")
    parts = connection.execute(
        "SELECT * FROM recording_parts WHERE session_uid = ? ORDER BY part_number",
        (session_uid,),
    ).fetchall()
    if len(parts) > 1:
        raise ValueError(f"Session {session_uid} has multiple parts; supply part_id")
    if parts:
        return parts[0]

    status = recording_part_status(session["status"])
    if ingest_complete is None:
        ingest_complete = connection.execute(
            "SELECT 1 FROM media_artifacts WHERE session_uid = ? AND kind = 'raw_track'",
            (session_uid,),
        ).fetchone() is not None
    part_id = recording_part_id(session_uid, 1)
    connection.execute(
        """
        INSERT INTO recording_parts (
            part_id, session_uid, part_number, status, auphonic_production_id,
            ingest_complete, output_basename, failure_reason, created_at, updated_at
        ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            part_id, session_uid, status, session["auphonic_production_id"],
            int(ingest_complete), session_uid, session["failure_reason"],
            session["created_at"], session["updated_at"],
        ),
    )
    connection.execute(
        "UPDATE media_artifacts SET part_id = ? WHERE session_uid = ? "
        "AND part_id IS NULL AND kind IN ('raw_track', 'pre_edit_master', 'pre_edit_stem')",
        (part_id, session_uid),
    )
    # Preserve the episode's current transcript and its row ID. Only positive
    # pre-edit provenance permits a second metadata reference for the part.
    connection.execute(
        """
        INSERT INTO media_artifacts (
            session_uid, part_id, kind, role, filename, content_type, byte_size,
            sha256, duration_seconds, s3_uri, source_provider, source_id,
            created_at, updated_at
        )
        SELECT session_uid, ?, kind, role, filename, content_type, byte_size,
               sha256, duration_seconds, s3_uri, source_provider, source_id,
               created_at, updated_at
        FROM media_artifacts
        WHERE session_uid = ? AND part_id IS NULL AND kind = 'transcript'
          AND source_provider = 'auphonic' AND source_id = ?
          AND source_id != COALESCE(?, '')
        """,
        (
            part_id, session_uid, session["auphonic_production_id"],
            session["transcript_refresh_production_id"],
        ),
    )
    return connection.execute(
        "SELECT * FROM recording_parts WHERE part_id = ?", (part_id,)
    ).fetchone()
