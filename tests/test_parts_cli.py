import logging

import pytest
from typer.testing import CliRunner

from podline import cli
from test_part_processing import MultiProductionClient
from test_recording_parts import PartsClient, ingest, session_payload


runner = CliRunner()


@pytest.mark.parametrize("command", [
    "auphonic-prepare", "auphonic-preview", "auphonic-start", "auphonic-offset",
    "auphonic-reset", "auphonic-inspect", "auphonic-sync", "workspace", "status",
])
def test_part_selector_is_documented(command):
    result = runner.invoke(cli.app, [command, "--help"])
    assert result.exit_code == 0, result.output
    assert "--part" in result.output


def test_source_export_option_is_documented():
    result = runner.invoke(cli.app, ["workspace", "--help"])
    assert "--sources" in result.output


@pytest.fixture
def cli_parts(moto_s3, app_config_factory, runtime_factory, tmp_path, monkeypatch):
    from podline import part_processing
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    episode = ingest(config, runtime, PartsClient(session_payload()))
    client = MultiProductionClient()
    monkeypatch.setattr(cli, "_bootstrap", lambda *_a, **_k: (
        config, runtime, logging.getLogger("parts-cli-test"),
    ))
    monkeypatch.setattr(part_processing, "AuphonicClient", lambda _config: client)
    return runtime, episode, client


def test_episode_cli_prepare_offset_preview_start_and_resume(cli_parts, monkeypatch):
    runtime, episode, client = cli_parts
    monkeypatch.setattr(cli.webbrowser, "open", lambda *_: pytest.fail("must not open with --no-open"))
    for command in (
        ["auphonic-prepare", episode.session_uid],
        ["auphonic-offset", episode.session_uid],
        ["auphonic-preview", episode.session_uid, "--no-open"],
    ):
        result = runner.invoke(cli.app, command)
        assert result.exit_code == 0, result.output
        assert "Part 01" in result.output and "Part 03" in result.output
        assert client.started == []
    result = runner.invoke(cli.app, ["auphonic-start", episode.session_uid])
    assert result.exit_code == 0, result.output
    assert client.started == ["prod-1", "prod-2", "prod-3"]
    result = runner.invoke(cli.app, ["auphonic-start", episode.session_uid])
    assert result.exit_code == 0, result.output
    assert len(client.started) == 3
    workspace = runtime.workspaces_dir
    assert workspace


def test_selected_part_does_not_prepare_siblings(cli_parts):
    _, episode, client = cli_parts
    result = runner.invoke(cli.app, [
        "auphonic-prepare", episode.session_uid, "--part", "2",
    ])
    assert result.exit_code == 0, result.output
    assert client.created[0][1] == 3
    assert len(client.created) == 1
    result = runner.invoke(cli.app, ["auphonic-reset", episode.session_uid])
    assert result.exit_code == 1, result.output
    assert "--part" in result.output


def test_sources_available_before_any_processing(cli_parts):
    _, episode, client = cli_parts
    result = runner.invoke(cli.app, [
        "workspace", episode.session_uid, "--sources", "--part", "2",
    ])
    assert result.exit_code == 0, result.output
    assert client.created == [] and client.started == []


def test_existing_explicit_combined_command_still_processes_a_single_take(
    moto_s3, app_config_factory, runtime_factory, tmp_path, monkeypatch,
):
    from podline import part_processing
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    episode = ingest(config, runtime, PartsClient(session_payload((2,))))
    client = MultiProductionClient()
    monkeypatch.setattr(cli, "_bootstrap", lambda *_a, **_k: (
        config, runtime, logging.getLogger("single-cli-test"),
    ))
    monkeypatch.setattr(part_processing, "AuphonicClient", lambda _config: client)
    result = runner.invoke(cli.app, ["auphonic", episode.session_uid])
    assert result.exit_code == 0, result.output
    assert len(client.created) == 1
    assert len(client.started) == 1
    from pathlib import Path
    workspace = Path(runtime.workspaces_dir) / episode.session_uid
    assert (workspace / f"{episode.session_uid}_pre_edit.mp3").exists()
    assert (workspace / f"{episode.session_uid}.vtt").exists()
