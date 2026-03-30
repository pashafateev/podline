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
                    "  request_timeout_seconds: 600",
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
    assert "Database Reset" in result.stdout
    assert str(db_file) in result.stdout
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


def test_refresh_resets_scans_and_lists_sessions(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    session_dir = raw_root / "010124_012430"
    session_dir.mkdir(parents=True)
    (session_dir / "010124_012430_Tr1.WAV").write_bytes(b"track-one")
    (session_dir / "010124_012430_Tr2.WAV").write_bytes(b"track-two")

    result = runner.invoke(app, ["refresh", "--config", str(config_file)])

    assert result.exit_code == 0
    assert "Refresh Complete" in result.stdout
    assert "discovered" in result.stdout
    assert "created" in result.stdout
    assert "010124_012430" in result.stdout


def test_status_uses_sectioned_output(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    db_file = runtime_home / "state" / "state.db"
    initialize_database(str(db_file))
    with connect(str(db_file)) as connection:
        connection.execute(
            """
            INSERT INTO sessions (
                session_uid, source_basename, source_date_folder, source_path,
                recorded_at, recorded_at_source, fingerprint, status,
                failure_reason, failed_at_stage, auphonic_production_id,
                pre_edit_master_path, transcript_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "session-1",
                "010124_012430",
                None,
                "/tmp/source",
                "2024-01-01T01:24:30-08:00",
                "file_birth_time",
                "fingerprint-1",
                "waiting_for_finalize",
                None,
                None,
                "prod-123",
                "/tmp/master.mp3",
                "/tmp/transcript.vtt",
                "now",
                "now",
            ),
        )
        connection.commit()

    result = runner.invoke(app, ["status", "session-1", "--config", str(config_file)])

    assert result.exit_code == 0
    assert "Session" in result.stdout
    assert "Overview" in result.stdout
    assert "Source" in result.stdout
    assert "Auphonic" in result.stdout
    assert "Metadata" in result.stdout
    assert "session_uid" in result.stdout
    assert "Ready To Finalize" in result.stdout
    assert "/tmp/master.mp3" in result.stdout
    assert "Episode Draft" not in result.stdout


def test_status_shows_episode_draft_pending_only_when_ready_for_draft(
    tmp_path: Path,
) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    db_file = runtime_home / "state" / "state.db"
    initialize_database(str(db_file))
    with connect(str(db_file)) as connection:
        connection.execute(
            """
            INSERT INTO sessions (
                session_uid, source_basename, source_date_folder, source_path,
                recorded_at, recorded_at_source, fingerprint, status,
                failure_reason, failed_at_stage, auphonic_production_id,
                pre_edit_master_path, transcript_path, final_master_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "session-2",
                "010124_012430",
                None,
                "/tmp/source",
                "2024-01-01T01:24:30-08:00",
                "file_birth_time",
                "fingerprint-2",
                "ready_for_draft",
                None,
                None,
                "prod-123",
                "/tmp/pre.mp3",
                "/tmp/transcript.vtt",
                "/tmp/final.mp3",
                "now",
                "now",
            ),
        )
        connection.commit()

    result = runner.invoke(app, ["status", "session-2", "--config", str(config_file)])

    assert result.exit_code == 0
    assert "Episode Draft" in result.stdout
    assert "AI metadata pending" in result.stdout
