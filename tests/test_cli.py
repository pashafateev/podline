from pathlib import Path
from dataclasses import replace
import logging
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from podline import cli as cli_module
from podline.auphonic import (
    AuphonicAssetDuration,
    AuphonicError,
    AuphonicOffsetPlan,
    AuphonicOffsetResult,
    AuphonicOffsetTarget,
)
from podline.cli import app
from podline.db import connect, initialize_database
from podline.logging_setup import configure_logging
from podline.models import AssetRecord


runner = CliRunner()


def test_episode_meta_help_describes_openai() -> None:
    result = runner.invoke(app, ["episode-meta", "--help"])

    assert result.exit_code == 0
    assert "OpenAI" in result.stdout


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
                    '  base_url: "https://api.podbean.example/v1"',
                    '  podcast_id: ""',
                ]
            )
            + "\n"
        ),
        encoding="utf-8",
    )


def test_default_config_path_points_at_podline_checkout(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_module.Path, "home", lambda: tmp_path)

    assert cli_module._resolve_config_path(None) == tmp_path / "podline" / "config.yaml"


def test_personal_config_alias_uses_separate_runtime_and_local_override(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli_module.Path, "home", lambda: tmp_path)
    personal_dir = tmp_path / "podline-personal"
    personal_dir.mkdir()
    _write_config(personal_dir / "config.yaml", tmp_path / "unused", tmp_path / "raw")
    runtime = tmp_path / "personal-runtime"
    (personal_dir / "config.local.yaml").write_text(
        f"runtime_home: {runtime}\n", encoding="utf-8"
    )
    work_dir = tmp_path / "podline"
    work_dir.mkdir()
    _write_config(work_dir / "config.yaml", work_dir, tmp_path / "work-raw")

    result = runner.invoke(app, ["init", "--config", "personal"])

    assert result.exit_code == 0, result.output
    assert (runtime / "state" / "state.db").is_file()
    assert not (work_dir / "state").exists()
    assert not (tmp_path / "unused").exists()


def test_missing_personal_config_fails_without_falling_back(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_module.Path, "home", lambda: tmp_path)
    work_dir = tmp_path / "podline"
    work_dir.mkdir()
    _write_config(work_dir / "config.yaml", work_dir, tmp_path / "raw")

    result = runner.invoke(app, ["init", "--config", "personal"])

    assert result.exit_code == 2
    assert "Personal config not found" in result.output
    assert not (work_dir / "state").exists()


@pytest.mark.parametrize("filename", ["custom.yaml", "personal"])
def test_explicit_config_path_still_selects_file(monkeypatch, tmp_path: Path, filename: str) -> None:
    monkeypatch.setattr(cli_module.Path, "home", lambda: tmp_path)
    config_file = tmp_path / filename
    runtime = tmp_path / "custom-runtime"
    _write_config(config_file, runtime, tmp_path / "raw")

    result = runner.invoke(app, ["init", "--config", str(config_file)])

    assert result.exit_code == 0, result.output
    assert (runtime / "state" / "state.db").is_file()


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


def test_refresh_is_retired_and_points_at_import(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    result = runner.invoke(app, ["refresh", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "retired" in result.stdout
    assert "pod import" in result.stdout


def test_scan_is_retired_and_points_at_import(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    result = runner.invoke(app, ["scan", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "retired" in result.stdout
    assert "pod import" in result.stdout


def test_import_command_registers_folder(tmp_path: Path, monkeypatch) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    folder = tmp_path / "recording"
    folder.mkdir()
    seen = {}

    def fake_import(*, config, runtime_paths, db_file, folder, logger):
        seen["folder"] = folder
        seen["runtime_paths"] = runtime_paths
        return SimpleNamespace(
            session_uid="session-imported-1", status="queued"
        ), True

    monkeypatch.setattr(cli_module, "import_local_folder", fake_import)

    result = runner.invoke(
        app, ["import", str(folder), "--config", str(config_file)]
    )

    assert result.exit_code == 0, result.stdout
    assert "Import Complete" in result.stdout
    assert "session-imported-1" in result.stdout
    assert seen["folder"] == folder
    assert seen["runtime_paths"] is not None


def test_list_shows_pick_column_and_full_mode(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    db_file = runtime_home / "state" / "state.db"
    initialize_database(str(db_file))
    with connect(str(db_file)) as connection:
        connection.execute(
            "INSERT INTO sessions (session_uid, source_path, fingerprint, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "session-1234567890",
                "/tmp/source/path",
                "fingerprint-1",
                "queued",
                "now",
                "now",
            ),
        )
        connection.commit()

    result = runner.invoke(app, ["list", "--config", str(config_file)])
    full_result = runner.invoke(app, ["list", "--full", "--config", str(config_file)])

    assert result.exit_code == 0
    assert "pick" in result.stdout
    assert "@1" in result.stdout
    assert "session-12345..." in result.stdout
    assert full_result.exit_code == 0
    assert "session-1234567890" in full_result.stdout


def test_status_accepts_selector_index(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    db_file = runtime_home / "state" / "state.db"
    initialize_database(str(db_file))
    with connect(str(db_file)) as connection:
        connection.execute(
            "INSERT INTO sessions (session_uid, source_path, fingerprint, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("session-1", "/tmp/source1", "fingerprint-1", "queued", "2024", "2024"),
        )
        connection.execute(
            "INSERT INTO sessions (session_uid, source_path, fingerprint, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("session-2", "/tmp/source2", "fingerprint-2", "queued", "2025", "2025"),
        )
        connection.commit()

    result = runner.invoke(app, ["status", "@1", "--config", str(config_file)])

    assert result.exit_code == 0
    assert "session-2" in result.stdout


def test_auphonic_inspect_accepts_selector_index(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    db_file = runtime_home / "state" / "state.db"
    initialize_database(str(db_file))
    with connect(str(db_file)) as connection:
        connection.execute(
            "INSERT INTO sessions (session_uid, source_path, fingerprint, status, auphonic_production_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "session-1",
                "/tmp/source1",
                "fingerprint-1",
                "queued",
                "prod-111",
                "2024",
                "2024",
            ),
        )
        connection.execute(
            "INSERT INTO sessions (session_uid, source_path, fingerprint, status, auphonic_production_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "session-2",
                "/tmp/source2",
                "fingerprint-2",
                "queued",
                "prod-222",
                "2025",
                "2025",
            ),
        )
        connection.commit()

    result = runner.invoke(
        app,
        ["auphonic-inspect", "--session", "@1", "--config", str(config_file)],
    )

    assert result.exit_code != 0
    assert "Session not found" not in result.stdout


def test_auphonic_inspect_prints_track_offsets(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    def fake_inspect(config, production_id):
        return {
            "production_id": production_id,
            "status": 10,
            "status_string": "Production Not Started Yet",
            "start_allowed": True,
            "change_allowed": True,
            "edit_page": "https://example.test/edit/prod-123",
            "status_page": "https://example.test/status/prod-123",
            "error_message": "",
            "warning_message": "",
            "multi_input_files": [
                {
                    "id": "Pasha",
                    "type": "multitrack",
                    "input_file": "Tr2.webm",
                    "offset": 1.801,
                }
            ],
            "output_files": [],
        }

    monkeypatch.setattr(cli_module, "inspect_production", fake_inspect)

    result = runner.invoke(
        app, ["auphonic-inspect", "prod-123", "--config", str(config_file)]
    )

    assert result.exit_code == 0
    assert "offset=1.801" in result.stdout


def test_auphonic_preview_accepts_selector_index_without_opening_browser(
    monkeypatch, tmp_path: Path
) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    db_file = runtime_home / "state" / "state.db"
    initialize_database(str(db_file))
    with connect(str(db_file)) as connection:
        connection.execute(
            "INSERT INTO sessions (session_uid, source_path, fingerprint, status, auphonic_production_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "session-1",
                "/tmp/source1",
                "fingerprint-1",
                "auphonic_prepared",
                "prod-111",
                "2024",
                "2024",
            ),
        )
        connection.commit()

    browser_calls = []

    def fake_open(url: str) -> bool:
        browser_calls.append(url)
        return True

    def fake_preview(*, config, db_file, session, logger=None):
        return SimpleNamespace(
            session=replace(session, status="auphonic_previewed"),
            details={
                "edit_page": "https://example.test/edit/prod-111",
                "status_page": "https://example.test/status/prod-111",
                "multi_input_files": [
                    {
                        "id": "Jonathan",
                        "type": "multitrack",
                        "input_file": "Tr1.webm",
                        "offset": 0,
                    },
                    {
                        "id": "Pasha",
                        "type": "multitrack",
                        "input_file": "Tr2.webm",
                        "offset": 1.612,
                    },
                ],
            },
        )

    monkeypatch.setattr(cli_module.webbrowser, "open", fake_open)
    monkeypatch.setattr(
        cli_module, "preview_prepared_session_with_auphonic", fake_preview
    )

    result = runner.invoke(
        app,
        ["auphonic-preview", "@1", "--no-open", "--config", str(config_file)],
    )

    assert result.exit_code == 0
    assert "Auphonic Preview" in result.stdout
    assert "https://example.test/edit/prod-111" in result.stdout
    assert "offset=1.612" in result.stdout
    assert "pod auphonic-start @1" in result.stdout
    assert browser_calls == []


def test_auphonic_offset_command_prints_dry_run_plan(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    db_file = runtime_home / "state" / "state.db"
    initialize_database(str(db_file))
    with connect(str(db_file)) as connection:
        connection.execute(
            "INSERT INTO sessions (session_uid, source_path, fingerprint, status, auphonic_production_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "session-1",
                "/tmp/source1",
                "fingerprint-1",
                "queued",
                "prod-111",
                "2024",
                "2024",
            ),
        )
        connection.execute(
            "INSERT INTO sessions (session_uid, source_path, fingerprint, status, auphonic_production_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "session-2",
                "/tmp/source2",
                "fingerprint-2",
                "queued",
                "prod-222",
                "2025",
                "2025",
            ),
        )
        connection.commit()

    calls = []
    tr1 = AssetRecord("host1", "Tr1.webm", 9, "/tmp/source2/Tr1.webm")
    tr2 = AssetRecord("host2", "Tr2.webm", 9, "/tmp/source2/Tr2.webm")
    tr3 = AssetRecord("guest1", "Tr3.webm", 9, "/tmp/source2/Tr3.webm")

    def fake_offset(*, config, runtime_paths, db_file, session, apply):
        calls.append((session.session_uid, apply))
        return AuphonicOffsetResult(
            plan=AuphonicOffsetPlan(
                production_id="prod-222",
                asset_durations=[
                    AuphonicAssetDuration(tr1, 101.234),
                    AuphonicAssetDuration(tr2, 99.433),
                    AuphonicAssetDuration(tr3, 101.234),
                ],
                local_assets=[tr1],
                reference_asset=tr1,
                targets=[
                    AuphonicOffsetTarget(
                        asset=tr2,
                        track_id="Pasha",
                        input_file="Tr2.webm",
                        offset_seconds=1.801,
                    )
                ],
                aligned_assets=[tr3],
            ),
            applied=False,
            track_states=[],
        )

    monkeypatch.setattr(cli_module, "offset_session_auphonic_production", fake_offset)

    result = runner.invoke(
        app, ["auphonic-offset", "@1", "--config", str(config_file)]
    )

    assert result.exit_code == 0
    assert calls == [("session-2", False)]
    assert "Auphonic Offset Plan" in result.stdout
    assert "Tr2.webm" in result.stdout
    assert "offset_seconds" in result.stdout
    assert "1.801" in result.stdout
    assert "Already Aligned" in result.stdout
    assert "Tr3.webm" in result.stdout
    assert "applied" in result.stdout


def test_print_section_handles_empty_items(capsys) -> None:
    cli_module._print_section("Targets", [])

    assert capsys.readouterr().out == "\nTargets\n  none\n"


@pytest.mark.parametrize("apply", [False, True])
def test_auphonic_offset_command_prints_equal_duration_plan(
    tmp_path: Path, monkeypatch, apply: bool
) -> None:
    runtime_home = tmp_path / "runtime"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, tmp_path / "raw")

    db_file = runtime_home / "state" / "state.db"
    initialize_database(str(db_file))
    with connect(str(db_file)) as connection:
        connection.execute(
            "INSERT INTO sessions (session_uid, source_path, fingerprint, status, auphonic_production_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "session-1",
                "/tmp/source",
                "fingerprint-1",
                "auphonic_prepared",
                "prod-111",
                "2025",
                "2025",
            ),
        )
        connection.commit()

    tr1 = AssetRecord("host1", "091826_125329_Tr1.WAV", 9, "/tmp/source/Tr1.WAV")
    tr2 = AssetRecord("host2", "091826_125329_Tr2.WAV", 9, "/tmp/source/Tr2.WAV")
    calls = []

    def fake_offset(*, config, runtime_paths, db_file, session, apply):
        calls.append((session.session_uid, apply))
        return AuphonicOffsetResult(
            plan=AuphonicOffsetPlan(
                production_id="prod-111",
                asset_durations=[
                    AuphonicAssetDuration(tr1, 3598.0),
                    AuphonicAssetDuration(tr2, 3598.0),
                ],
                local_assets=[],
                reference_asset=tr1,
                targets=[],
                aligned_assets=[tr2],
            ),
            applied=apply,
            track_states=[],
        )

    monkeypatch.setattr(cli_module, "offset_session_auphonic_production", fake_offset)
    args = ["auphonic-offset", "session-1", "--config", str(config_file)]
    if apply:
        args.append("--apply")

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert calls == [("session-1", apply)]
    heading = "Auphonic Offset Applied" if apply else "Auphonic Offset Plan"
    assert heading in result.stdout
    assert "3598.000s" in result.stdout
    assert "Targets\n  none\n" in result.stdout
    assert (
        "Already Aligned\n091826_125329_Tr2.WAV  offset_seconds=0  update=skipped"
        in result.stdout
    )


def test_auphonic_reset_command_requeues_session(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    db_file = runtime_home / "state" / "state.db"
    initialize_database(str(db_file))
    with connect(str(db_file)) as connection:
        connection.execute(
            "INSERT INTO sessions (session_uid, source_path, fingerprint, status, auphonic_production_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "session-1",
                "/tmp/source1",
                "fingerprint-1",
                "auphonic_previewed",
                "prod-stale",
                "2024",
                "2024",
            ),
        )
        connection.commit()

    result = runner.invoke(
        app, ["auphonic-reset", "session-1", "--config", str(config_file)]
    )

    assert result.exit_code == 0
    assert "Auphonic Reset" in result.stdout
    assert "prod-stale" in result.stdout
    assert "pod auphonic-prepare session-1" in result.stdout
    with connect(str(db_file)) as connection:
        row = connection.execute(
            "SELECT status, auphonic_production_id FROM sessions WHERE session_uid = ?",
            ("session-1",),
        ).fetchone()
    assert row["status"] == "queued"
    assert row["auphonic_production_id"] is None


def test_auphonic_reset_command_rejects_started_session(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    db_file = runtime_home / "state" / "state.db"
    initialize_database(str(db_file))
    with connect(str(db_file)) as connection:
        connection.execute(
            "INSERT INTO sessions (session_uid, source_path, fingerprint, status, auphonic_production_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "session-1",
                "/tmp/source1",
                "fingerprint-1",
                "auphonic_started",
                "prod-live",
                "2024",
                "2024",
            ),
        )
        connection.commit()

    result = runner.invoke(
        app, ["auphonic-reset", "session-1", "--config", str(config_file)]
    )

    assert result.exit_code == 1
    assert "must be auphonic_prepared or auphonic_previewed" in result.stdout
    with connect(str(db_file)) as connection:
        row = connection.execute(
            "SELECT status, auphonic_production_id FROM sessions WHERE session_uid = ?",
            ("session-1",),
        ).fetchone()
    assert row["status"] == "auphonic_started"
    assert row["auphonic_production_id"] == "prod-live"


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


def test_finalize_uses_single_master_from_expected_directory(tmp_path: Path, monkeypatch) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    masters_dir = runtime_home / "deliverables" / "masters"
    masters_dir.mkdir(parents=True)
    final_master = masters_dir / "episode_master.mp3"
    final_master.write_bytes(b"final")

    transcript = tmp_path / "transcript.vtt"
    transcript.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n", encoding="utf-8"
    )
    pre_edit = tmp_path / "pre.mp3"
    pre_edit.write_bytes(b"pre")

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
                "session-finalize-1",
                "010124_012430",
                None,
                "/tmp/source",
                None,
                None,
                "fingerprint-finalize-1",
                "waiting_for_finalize",
                None,
                None,
                "prod-123",
                str(pre_edit),
                str(transcript),
                None,
                "now",
                "now",
            ),
        )
        connection.commit()

    captured = {}

    def fake_finalize_session(*, config, runtime_paths, db_file, session, master_path):
        captured["master_path"] = master_path
        return SimpleNamespace(session_uid=session.session_uid, status="ready_for_draft")

    monkeypatch.setattr(cli_module, "finalize_session", fake_finalize_session)

    result = runner.invoke(
        app, ["finalize", "session-finalize-1", "--config", str(config_file)]
    )

    assert result.exit_code == 0, result.stdout
    assert captured["master_path"] == final_master


def test_finalize_errors_when_multiple_expected_masters_exist(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    masters_dir = runtime_home / "deliverables" / "masters"
    masters_dir.mkdir(parents=True)
    (masters_dir / "one.mp3").write_bytes(b"1")
    (masters_dir / "two.mp3").write_bytes(b"2")

    transcript = tmp_path / "transcript.vtt"
    transcript.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n", encoding="utf-8"
    )
    pre_edit = tmp_path / "pre.mp3"
    pre_edit.write_bytes(b"pre")

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
                "session-finalize-2",
                "010124_012430",
                None,
                "/tmp/source",
                None,
                None,
                "fingerprint-finalize-2",
                "waiting_for_finalize",
                None,
                None,
                "prod-123",
                str(pre_edit),
                str(transcript),
                None,
                "now",
                "now",
            ),
        )
        connection.commit()

    result = runner.invoke(
        app, ["finalize", "session-finalize-2", "--config", str(config_file)]
    )

    assert result.exit_code == 1
    assert "multiple masters were found" in result.stdout


def test_finalize_accepts_filename_relative_to_expected_directory(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    masters_dir = runtime_home / "deliverables" / "masters"
    masters_dir.mkdir(parents=True)
    final_master = masters_dir / "episode_master.mp3"
    final_master.write_bytes(b"final")

    transcript = tmp_path / "transcript.vtt"
    transcript.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n", encoding="utf-8"
    )
    pre_edit = tmp_path / "pre.mp3"
    pre_edit.write_bytes(b"pre")

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
                "session-finalize-3",
                "010124_012430",
                None,
                "/tmp/source",
                None,
                None,
                "fingerprint-finalize-3",
                "waiting_for_finalize",
                None,
                None,
                "prod-123",
                str(pre_edit),
                str(transcript),
                None,
                "now",
                "now",
            ),
        )
        connection.commit()

    captured = {}

    def fake_finalize_session(*, config, runtime_paths, db_file, session, master_path):
        captured["master_path"] = master_path
        return SimpleNamespace(session_uid=session.session_uid, status="ready_for_draft")

    monkeypatch.setattr(cli_module, "finalize_session", fake_finalize_session)

    result = runner.invoke(
        app,
        [
            "finalize",
            "session-finalize-3",
            "--master",
            "episode_master.mp3",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert captured["master_path"] == final_master


def test_finalize_prefers_current_directory_file_over_expected_directory(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    masters_dir = runtime_home / "deliverables" / "masters"
    masters_dir.mkdir(parents=True)
    expected_master = masters_dir / "episode_master.mp3"
    expected_master.write_bytes(b"expected")

    current_dir = tmp_path / "cwd"
    current_dir.mkdir()
    local_master = current_dir / "episode_master.mp3"
    local_master.write_bytes(b"local")
    monkeypatch.chdir(current_dir)

    transcript = tmp_path / "transcript.vtt"
    transcript.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n", encoding="utf-8"
    )
    pre_edit = tmp_path / "pre.mp3"
    pre_edit.write_bytes(b"pre")

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
                "session-finalize-4",
                "010124_012430",
                None,
                "/tmp/source",
                None,
                None,
                "fingerprint-finalize-4",
                "waiting_for_finalize",
                None,
                None,
                "prod-123",
                str(pre_edit),
                str(transcript),
                None,
                "now",
                "now",
            ),
        )
        connection.commit()

    captured = {}

    def fake_finalize_session(*, config, runtime_paths, db_file, session, master_path):
        captured["master_path"] = master_path
        return SimpleNamespace(session_uid=session.session_uid, status="ready_for_draft")

    monkeypatch.setattr(cli_module, "finalize_session", fake_finalize_session)

    result = runner.invoke(
        app,
        [
            "finalize",
            "session-finalize-4",
            "--master",
            "episode_master.mp3",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert captured["master_path"] == local_master.resolve()


def test_finalize_accepts_episode_shortcut_for_expected_master(tmp_path: Path, monkeypatch) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    masters_dir = runtime_home / "deliverables" / "masters"
    masters_dir.mkdir(parents=True)
    final_master = masters_dir / "sexdrunks_ep16_master.mp3"
    final_master.write_bytes(b"final")

    transcript = tmp_path / "transcript.vtt"
    transcript.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n", encoding="utf-8"
    )
    pre_edit = tmp_path / "pre.mp3"
    pre_edit.write_bytes(b"pre")

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
                "session-finalize-5",
                "010124_012430",
                None,
                "/tmp/source",
                None,
                None,
                "fingerprint-finalize-5",
                "waiting_for_finalize",
                None,
                None,
                "prod-123",
                str(pre_edit),
                str(transcript),
                None,
                "now",
                "now",
            ),
        )
        connection.commit()

    captured = {}

    def fake_finalize_session(*, config, runtime_paths, db_file, session, master_path):
        captured["master_path"] = master_path
        return SimpleNamespace(session_uid=session.session_uid, status="ready_for_draft")

    monkeypatch.setattr(cli_module, "finalize_session", fake_finalize_session)

    result = runner.invoke(
        app,
        [
            "finalize",
            "session-finalize-5",
            "--master",
            "ep16",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert captured["master_path"] == final_master


def test_transcript_refresh_accepts_selector_index(tmp_path: Path, monkeypatch) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    transcript = tmp_path / "old-transcript.vtt"
    transcript.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nold\n", encoding="utf-8"
    )
    final_master = tmp_path / "final-master.mp3"
    final_master.write_bytes(b"final")

    db_file = runtime_home / "state" / "state.db"
    initialize_database(str(db_file))
    with connect(str(db_file)) as connection:
        connection.execute(
            """
            INSERT INTO sessions (
                session_uid, source_path, fingerprint, status, transcript_path,
                final_master_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "session-refresh-1",
                "/tmp/source1",
                "fingerprint-refresh-1",
                "ready_for_draft",
                str(transcript),
                str(final_master),
                "2024",
                "2024",
            ),
        )
        connection.execute(
            """
            INSERT INTO sessions (
                session_uid, source_path, fingerprint, status, transcript_path,
                final_master_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "session-refresh-2",
                "/tmp/source2",
                "fingerprint-refresh-2",
                "ready_for_draft",
                str(transcript),
                str(final_master),
                "2025",
                "2025",
            ),
        )
        connection.commit()

    seen = {}

    def fake_refresh_transcript_with_auphonic(**kwargs):
        seen["session_uid"] = kwargs["session"].session_uid
        seen["preset_id"] = kwargs["preset_id"]
        return kwargs["session"]

    monkeypatch.setattr(
        cli_module,
        "refresh_transcript_with_auphonic",
        fake_refresh_transcript_with_auphonic,
    )

    result = runner.invoke(
        app,
        [
            "transcript-refresh",
            "@1",
            "--preset",
            "one-off-preset",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == 0
    assert seen == {
        "session_uid": "session-refresh-2",
        "preset_id": "one-off-preset",
    }
    assert "Transcript Refreshed" in result.stdout
    assert "session-refresh-2" in result.stdout


def _bootstrap_cli_runtime(tmp_path: Path):
    """Write a config, initialize the runtime DB, and return (config, home, db)."""
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)
    bootstrap = runner.invoke(app, ["list", "--config", str(config_file)])
    assert bootstrap.exit_code == 0, bootstrap.stdout
    return config_file, runtime_home, str(runtime_home / "state" / "state.db")


def test_workspace_clean_rejects_negative_retention(
    tmp_path: Path, session_factory
) -> None:
    config_file, runtime_home, db_file = _bootstrap_cli_runtime(tmp_path)
    session_factory(db_file, session_uid="released-1", status="draft_created")
    workspace = runtime_home / "workspaces" / "released-1"
    workspace.mkdir(parents=True)
    project = workspace / "edit.cpr"
    project.write_bytes(b"fresh-cubase-project")

    result = runner.invoke(
        app,
        [
            "workspace-clean",
            "--older-than-days",
            "-1",
            "--apply",
            "--config",
            str(config_file),
        ],
    )

    assert project.exists(), "negative retention must never delete workspaces"
    assert result.exit_code != 0


def test_workspace_clean_rejects_nan_retention(
    tmp_path: Path, session_factory
) -> None:
    """NaN passes numeric floors (nan < 0 is False) and must not evict anything."""
    config_file, runtime_home, db_file = _bootstrap_cli_runtime(tmp_path)
    session_factory(db_file, session_uid="released-2", status="draft_created")
    workspace = runtime_home / "workspaces" / "released-2"
    workspace.mkdir(parents=True)
    project = workspace / "edit.cpr"
    project.write_bytes(b"fresh-cubase-project")

    result = runner.invoke(
        app,
        [
            "workspace-clean",
            "--older-than-days",
            "nan",
            "--apply",
            "--config",
            str(config_file),
        ],
    )

    assert project.exists(), "NaN retention must never delete workspaces"
    assert result.exit_code != 0


def test_status_workspace_tag_requires_current_content(
    tmp_path: Path, session_factory
) -> None:
    """+workspace means "the current artifact is in the workspace", not
    "a file with that name exists"."""
    import hashlib

    from podline.repository import register_media_artifact

    config_file, runtime_home, db_file = _bootstrap_cli_runtime(tmp_path)
    session_factory(db_file, session_uid="s-tag", status="ready_for_draft")
    current = b"WEBVTT\n\ncurrent transcript v2\n"
    sha256 = hashlib.sha256(current).hexdigest()
    register_media_artifact(
        db_file,
        session_uid="s-tag",
        kind="transcript",
        role="",
        filename="ep.vtt",
        content_type="text/vtt",
        byte_size=len(current),
        sha256=sha256,
        duration_seconds=None,
        s3_uri=f"s3://podline-media/sessions/s-tag/transcript/{sha256}/ep.vtt",
        source_provider="test",
        source_id=None,
        timestamp="now",
    )
    workspace = runtime_home / "workspaces" / "s-tag"
    workspace.mkdir(parents=True)
    workspace_copy = workspace / "ep.vtt"

    workspace_copy.write_bytes(current)
    fresh = runner.invoke(app, ["status", "s-tag", "--config", str(config_file)])
    assert fresh.exit_code == 0
    assert "+workspace" in fresh.stdout

    workspace_copy.write_bytes(b"WEBVTT\n\nstale transcript v1\n")
    stale = runner.invoke(app, ["status", "s-tag", "--config", str(config_file)])
    assert stale.exit_code == 0
    assert "+workspace" not in stale.stdout


def test_auphonic_prepare_survives_display_inspection_failure(
    tmp_path: Path, session_factory, monkeypatch
) -> None:
    """The attached-tracks display is informational; a transient inspection
    failure after a successful prepare must not fail the command."""
    config_file, _runtime_home, db_file = _bootstrap_cli_runtime(tmp_path)
    prepared = session_factory(
        db_file,
        session_uid="s-prep",
        status="auphonic_prepared",
        auphonic_production_id="prod-9",
    )
    monkeypatch.setattr(
        cli_module, "prepare_session_with_auphonic", lambda **_kwargs: prepared
    )

    def _transient_failure(*_args, **_kwargs):
        raise AuphonicError("transient inspection failure")

    monkeypatch.setattr(cli_module, "inspect_production", _transient_failure)

    result = runner.invoke(
        app, ["auphonic-prepare", "s-prep", "--config", str(config_file)]
    )

    assert "Auphonic Prepared" in result.stdout
    assert result.exit_code == 0
