import logging

import pytest
from typer.testing import CliRunner

from podline import cli
from podline.workflow_lock import session_lock


@pytest.mark.parametrize(("command", "arguments", "service"), [
    ("auphonic", ["@1"], "process_session_with_auphonic"),
    ("auphonic-prepare", ["@1"], "prepare_session_with_auphonic"),
    ("auphonic-preview", ["@1", "--no-open"], "preview_prepared_session_with_auphonic"),
    ("auphonic-offset", ["@1", "--apply"], "offset_session_auphonic_production"),
    ("auphonic-reset", ["@1"], "reset_session_auphonic_production"),
    ("auphonic-start", ["@1"], "start_prepared_session_with_auphonic"),
    ("auphonic-sync", ["--session", "@1"], "reconcile_production_to_session"),
    ("finalize", ["@1", "--master", "ep48"], "finalize_session"),
])
def test_manual_commands_respect_active_guided_run(
    command, arguments, service, app_config_factory, runtime_factory,
    session_factory, tmp_path, monkeypatch,
):
    config = app_config_factory(tmp_path)
    paths = runtime_factory(config)
    session = session_factory(paths.db_file, auphonic_production_id="prod")
    monkeypatch.setattr(cli, "_bootstrap", lambda *args, **kwargs: (config, paths, logging.getLogger()))
    monkeypatch.setattr(cli, service, lambda **kwargs: pytest.fail("service called while locked"))
    with session_lock(paths.db_file, session.session_uid, "continue"):
        result = CliRunner().invoke(cli.app, [command, *arguments])
    assert result.exit_code == 1
    assert "already running" in result.stdout


def test_manual_command_reloads_stable_session_under_lock(
    app_config_factory, runtime_factory, session_factory, tmp_path, monkeypatch,
):
    from contextlib import contextmanager
    from podline.auphonic import AuphonicError
    from podline.repository import update_session_status
    from podline.workflow_lock import WorkflowBusy

    config = app_config_factory(tmp_path)
    paths = runtime_factory(config)
    selected = session_factory(paths.db_file)
    monkeypatch.setattr(cli, "_bootstrap", lambda *args, **kwargs: (config, paths, logging.getLogger()))

    @contextmanager
    def concurrent_change(db_file, uid, operation):
        assert uid == selected.session_uid
        update_session_status(
            db_file, session_uid=uid, from_state="queued", to_state="auphonic_previewed",
            timestamp="now", detail="another command finished before lock acquisition",
        )
        with session_lock(db_file, uid, operation):
            yield

    def verify_start(*, session, **kwargs):
        assert session.status == "auphonic_previewed"
        with pytest.raises(WorkflowBusy):
            with session_lock(paths.db_file, session.session_uid, "continue"):
                pytest.fail("manual command released its lock before start")
        raise AuphonicError("checked locked state")

    monkeypatch.setattr(cli, "session_lock", concurrent_change)
    monkeypatch.setattr(cli, "start_prepared_session_with_auphonic", verify_start)
    result = CliRunner().invoke(cli.app, ["auphonic-start", "@1"])
    assert result.exit_code == 1
    assert "checked locked state" in result.stdout
