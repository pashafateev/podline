import logging
from pathlib import Path
import shlex

import pytest
from typer.testing import CliRunner

from podline import cli
from podline.repository import complete_transcript_refresh, upsert_episode_metadata

runner = CliRunner()


@pytest.fixture
def workflow(tmp_path, app_config_factory, runtime_factory, session_factory, monkeypatch):
    config = app_config_factory(tmp_path)
    paths = runtime_factory(config)
    monkeypatch.setattr(cli, "_bootstrap", lambda *args, **kw: (config, paths, logging.getLogger("workflow")))
    config_path = tmp_path / "show config.yaml"
    config_path.touch()

    def invoke(command, *args):
        return runner.invoke(cli.app, [command, "@1", *args, "--config", str(config_path)])

    def seed(status, **kwargs):
        return session_factory(paths.db_file, status=status, **kwargs)

    return paths, config_path, invoke, seed


@pytest.mark.parametrize("state,expected", [
    ("queued", "auphonic-prepare"),
    ("auphonic_prepared", "auphonic-offset"),
    ("auphonic_started", "auphonic-sync --session"),
    ("waiting_for_finalize", "finalize"),
    ("ready_for_draft", "transcript-refresh"),
])
def test_status_guides_manual_steps(workflow, state, expected):
    paths, config, invoke, seed = workflow
    session = seed(state)
    result = invoke("status")
    assert result.exit_code == 0, result.exception
    assert f"pod {expected} {session.session_uid}" in result.stdout
    assert shlex.quote(str(config)) in result.stdout


def test_continue_stops_until_explicit_export_selection(workflow):
    paths, config, invoke, seed = workflow
    session = seed("waiting_for_finalize")
    result = invoke("continue")
    assert result.exit_code == 0, result.exception
    assert str(Path(paths.workspaces_dir) / session.session_uid) in result.stdout
    assert f"pod continue {session.session_uid} --master" in result.stdout


def test_draft_link_is_terminal_for_status_and_continue(workflow):
    from podline.db import connect
    paths, _, invoke, seed = workflow
    session = seed("draft_created")
    upsert_episode_metadata(paths.db_file, session_uid=session.session_uid, title="Title", description="Notes")
    with connect(paths.db_file) as conn:
        conn.execute("UPDATE episodes SET podbean_post_id='123', draft_url='https://example.test/draft'")
    for cmd in ["status", "continue"]:
        result = invoke(cmd)
        assert result.exit_code == 0, result.exception
        assert "Review" in result.stdout and "https://example.test/draft" in result.stdout
        assert "pod podbean-draft" not in result.stdout


def test_refreshed_transcript_resumes_at_metadata(workflow, tmp_path, artifact_registrar):
    paths, _, invoke, seed = workflow
    session = seed("ready_for_draft", auphonic_production_id="pre-edit", transcript_refresh_production_id="final-refresh")
    vtt = tmp_path / "transcript.vtt"
    vtt.write_text("WEBVTT")
    artifact_registrar(paths.db_file, paths, session_uid=session.session_uid, kind="transcript",
                       path=vtt, source_provider="auphonic", source_id="final-refresh")
    complete_transcript_refresh(paths.db_file, session_uid=session.session_uid, status=session.status,
                                production_id="final-refresh", timestamp="now")
    result = invoke("status")
    assert f"pod episode-meta {session.session_uid}" in result.stdout
    upsert_episode_metadata(paths.db_file, session_uid=session.session_uid, title="Title", description="Notes")
    assert f"pod podbean-draft {session.session_uid}" in invoke("status").stdout


def test_continue_refuses_master_before_editing(workflow):
    paths, _, invoke, seed = workflow
    seed("queued")
    result = invoke("continue", "--master", "ep52")
    assert result.exit_code == 1
    assert "waiting_for_finalize" in result.stdout


def test_concurrent_continue_cannot_start_second_run(workflow):
    from podline.workflow_lock import session_lock
    paths, _, invoke, seed = workflow
    session = seed("queued")
    with session_lock(paths.db_file, session.session_uid, "continue"):
        result = invoke("continue")
    assert result.exit_code == 1 and "already running" in result.stdout


def test_legacy_refresh_receipt_without_original_production_id(workflow, tmp_path, artifact_registrar):
    paths, _, invoke, seed = workflow
    session = seed("ready_for_draft", transcript_refresh_production_id="refresh-legacy")
    vtt = tmp_path / "refreshed.vtt"
    vtt.write_text("WEBVTT")
    artifact_registrar(paths.db_file, paths, session_uid=session.session_uid, kind="transcript",
                       path=vtt, source_provider="auphonic", source_id="refresh-legacy")
    complete_transcript_refresh(paths.db_file, session_uid=session.session_uid, status=session.status,
                                production_id="refresh-legacy", timestamp="now")
    assert f"pod episode-meta {session.session_uid}" in invoke("status").stdout


def test_partial_prepare_offers_inspection(workflow):
    paths, _, invoke, seed = workflow
    session = seed("queued", auphonic_production_id="partial")
    assert f"pod auphonic-inspect --session {session.session_uid}" in invoke("status").stdout


def test_status_remembers_unresolved_export_and_lists_quoted_commands(
    tmp_path, app_config_factory, runtime_factory, session_factory, monkeypatch
):
    from dataclasses import replace
    config = app_config_factory(tmp_path)
    config = replace(config, paths=replace(config.paths, cubase_projects_root=tmp_path))
    paths = runtime_factory(config)
    session = session_factory(paths.db_file, status="waiting_for_finalize")
    monkeypatch.setattr(cli, "_bootstrap", lambda *a, **kw: (config, paths, logging.getLogger("test")))
    mixdown = tmp_path / "ep49" / "Mixdown"
    mixdown.mkdir(parents=True)
    candidate = mixdown / "corrected export.mp3"
    candidate.touch()
    for args in [["continue", "@1", "--master", "ep49"], ["status", "@1"]]:
        result = runner.invoke(cli.app, args)
        assert f"pod continue {session.session_uid} --master {shlex.quote(str(candidate))}" in result.stdout
        assert "ep49.mp3" in result.stdout


def test_single_track_prepared_goes_straight_to_preview(workflow, tmp_path, artifact_registrar):
    paths, _, invoke, seed = workflow
    session = seed("auphonic_prepared", auphonic_production_id="one-track")
    track = tmp_path / "speaker.wav"
    track.touch()
    artifact_registrar(paths.db_file, paths, session_uid=session.session_uid, kind="raw_track", role="host", path=track)
    assert f"pod auphonic-preview {session.session_uid}" in invoke("status").stdout


def test_unrelated_auphonic_transcript_cannot_skip_final_master_refresh(workflow, tmp_path, artifact_registrar):
    paths, _, invoke, seed = workflow
    session = seed("ready_for_draft", auphonic_production_id="current-pre-edit")
    vtt = tmp_path / "older.vtt"
    vtt.write_text("WEBVTT")
    artifact_registrar(paths.db_file, paths, session_uid=session.session_uid, kind="transcript",
                       path=vtt, source_provider="auphonic", source_id="other-production")
    assert f"pod transcript-refresh {session.session_uid}" in invoke("status").stdout


def test_manual_preview_without_applied_offsets_still_requires_alignment(
    workflow, tmp_path, artifact_registrar
):
    from podline.repository import record_offsets_applied
    paths, _, invoke, seed = workflow
    session = seed("auphonic_previewed", auphonic_production_id="previewed-first")
    for role in ["host", "guest"]:
        track = tmp_path / f"{role}.wav"
        track.write_bytes(role.encode())
        artifact_registrar(paths.db_file, paths, session_uid=session.session_uid,
                           kind="raw_track", role=role, path=track)
    assert f"pod auphonic-offset {session.session_uid} --apply" in invoke("status").stdout
    record_offsets_applied(paths.db_file, session.session_uid, "previewed-first")
    assert f"pod auphonic-start {session.session_uid}" in invoke("status").stdout


def test_empty_recovery_id_cannot_fall_through_to_draft_creation(workflow):
    _, _, invoke, seed = workflow
    seed("ready_for_draft")
    result = invoke("podbean-draft", "--existing-id", "")
    assert result.exit_code == 1
    assert "Choose either --existing-id" in result.stdout
