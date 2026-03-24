from pathlib import Path

from sexdrunks_automation.db import initialize_database


def test_initialize_database_creates_file(tmp_path: Path) -> None:
    db_file = tmp_path / "state" / "state.db"

    initialize_database(str(db_file))

    assert db_file.exists()
