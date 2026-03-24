import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_uid TEXT PRIMARY KEY,
    recorder_name TEXT,
    source_path TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    failure_reason TEXT,
    failed_at_stage TEXT,
    auphonic_production_id TEXT,
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
"""


def connect(db_file: str) -> sqlite3.Connection:
    Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_file)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database(db_file: str) -> None:
    with connect(db_file) as connection:
        connection.executescript(SCHEMA)
        connection.commit()
