from pathlib import Path
import logging

from typer.testing import CliRunner

from sexdrunks_automation.cli import app
from sexdrunks_automation.db import connect, initialize_database
from sexdrunks_automation.logging_setup import configure_logging


runner = CliRunner()


def _write_config(config_file: Path, runtime_home: Path, raw_root: Path) -> None:
    config_file.write_text(
        (
            "\n".join(
                [
                    f"runtime_home: {runtime_home}",
                    "paths:",
                    f"  raw_root: {raw_root}",
                    f"  deliverables_root: {runtime_home / 'deliverables'}",
                    "polling:",
                    "  interval_seconds: 30",
                    "notifications:",
                    "  enabled: false",
                    "auphonic:",
                    "  enabled: false",
                    '  api_key: ""',
                    '  base_url: "https://auphonic.example"',
                    "  poll_interval_seconds: 15",
                    "  max_poll_attempts: 20",
                    "  preset_ids:",
                    '    speakers_2: ""',
                    '    speakers_3: ""',
                    '    speakers_4: ""',
                    "podbean:",
                    "  enabled: false",
                    '  client_id: ""',
                    '  client_secret: ""',
                ]
            )
            + "\n"
        ),
        encoding="utf-8",
    )


def test_reset_db_recreates_database(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    db_file = runtime_home / "state" / "state.db"
    initialize_database(str(db_file))
    with connect(str(db_file)) as connection:
        connection.execute(
            "INSERT INTO sessions (session_uid, source_path, fingerprint, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("session-1", "/tmp/source", "fingerprint-1", "queued", "now", "now"),
        )
        connection.commit()

    result = runner.invoke(app, ["reset-db", "--yes", "--config", str(config_file)])

    assert result.exit_code == 0
    assert f"Reset database: {db_file}" in result.stdout
    with connect(str(db_file)) as connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()
    assert row["count"] == 0


def test_reset_db_requires_yes_flag(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    result = runner.invoke(app, ["reset-db", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "without --yes" in result.stdout


def test_configure_logging_switches_debug_level(tmp_path: Path) -> None:
    logger = configure_logging(str(tmp_path / "logs"), debug=False)
    assert logger.level == logging.INFO

    logger = configure_logging(str(tmp_path / "logs"), debug=True)
    assert logger.level == logging.DEBUG
