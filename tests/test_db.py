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
