import pytest

from podline.artifacts import workspace_path_for
from podline.auphonic import AuphonicError
from podline.preedit import advance_pre_edit
from podline.repository import get_session, update_session_status
from test_auphonic import FakeAuphonicClient, _queued_session_with_tracks


class GuidedClient(FakeAuphonicClient):
    def __init__(self):
        super().__init__()
        self.offsets = []
        self.interrupt_start = False
        self.ignore_offset = False
        self.waited = []

    def update_multi_input_file_offset(self, production_id, track_id, offset):
        self.offsets.append((track_id, offset))
        if not self.ignore_offset:
            next(track for track in self.tracks if track["id"] == track_id)["offset"] = offset

    def start_production(self, production_id):
        super().start_production(production_id)
        if self.interrupt_start:
            raise AuphonicError("connection lost")
        self._status, self._status_string = 4, "Processing"

    def get_production(self, production_id):
        response = super().get_production(production_id)
        if self._status == 4:
            response["data"]["start_allowed"] = False
        return response

    def wait_for_production(self, production_id):
        self.waited.append(production_id)
        self._status, self._status_string = 3, "Done"
        return super().wait_for_production(production_id)


@pytest.fixture
def guided_run(moto_s3, tmp_path, app_config_factory, runtime_factory, session_factory, artifact_registrar):
    config = app_config_factory(tmp_path)
    paths = runtime_factory(config)
    session = _queued_session_with_tracks(tmp_path, paths, session_factory, artifact_registrar)
    client = GuidedClient()

    def run():
        return advance_pre_edit(config=config, runtime_paths=paths, session=get_session(paths.db_file, session.session_uid), client=client)

    return paths, session, client, run


def test_guided_run_applies_offsets_and_materializes_edit(guided_run):
    paths, session, client, run = guided_run
    assert run().status == "waiting_for_finalize"
    assert client.offsets == [("Pasha", pytest.approx(1.801))]
    assert client.started == ["prod-123"]
    assert workspace_path_for(paths, session.session_uid).is_dir()


def test_unconfirmed_offset_leaves_prepared_for_inspection(guided_run):
    paths, session, client, run = guided_run
    client.ignore_offset = True
    with pytest.raises(AuphonicError, match="offset"):
        run()
    from podline.guidance import next_step
    stored = get_session(paths.db_file, session.session_uid)
    assert stored.status == "auphonic_prepared"
    handoff = next_step(paths, stored)
    assert handoff.manual == ("auphonic-inspect", "--session", session.session_uid)
    assert "offset" in handoff.reason
    assert not client.started


def test_interrupted_start_requires_reconciliation_before_another_start(guided_run):
    paths, session, client, run = guided_run
    client.interrupt_start = True
    with pytest.raises(AuphonicError, match="connection lost"):
        run()
    assert get_session(paths.db_file, session.session_uid).status == "auphonic_started"
    with pytest.raises(AuphonicError, match="auphonic-sync"):
        run()
    client._status, client._status_string = 4, "Processing"
    assert run().status == "waiting_for_finalize"
    assert client.started == ["prod-123"]
    assert len(client.created) == 1
    assert len(client.offsets) == 1


@pytest.mark.parametrize("status", ["auphonic_prepared", "auphonic_previewed", "auphonic_started"])
def test_remote_completion_resumes_without_restart(guided_run, status):
    paths, session, client, run = guided_run
    update_session_status(paths.db_file, session_uid=session.session_uid, from_state="queued", to_state=status,
                          timestamp="now", detail="manual preparation", auphonic_production_id="prod-123")
    client._status, client._status_string = 3, "Done"
    assert run().status == "waiting_for_finalize"
    assert not client.started and not client.offsets and not client.waited


def test_guided_retries_failed_prepare_with_no_production(guided_run):
    paths, session, client, run = guided_run
    update_session_status(paths.db_file, session_uid=session.session_uid, from_state="queued", to_state="failed",
                          timestamp="now", detail="prepare failed", failed_at_stage="auphonic")
    assert run().status == "waiting_for_finalize"
    assert len(client.created) == 1


@pytest.mark.parametrize("change_after_inspection", [False, True])
def test_stale_manual_offsets_require_review(guided_run, monkeypatch, change_after_inspection):
    paths, session, client, run = guided_run
    # Exercise the same production-specific marker written by the manual command.
    from podline.repository import record_offsets_applied
    update_session_status(paths.db_file, session_uid=session.session_uid, from_state="queued",
                          to_state="auphonic_previewed", timestamp="now", detail="manual preview",
                          auphonic_production_id="prod-123")
    record_offsets_applied(paths.db_file, session.session_uid, "prod-123")
    client.tracks = [{"id": "Jonathan", "input_file": "Tr1.webm", "offset": 0},
                     {"id": "Pasha", "input_file": "Tr2.webm", "offset": 7}]
    if change_after_inspection:
        client.tracks[1]["offset"] = 1.801
        inspect = client.get_production

        def inspect_then_edit(production_id):
            snapshot = inspect(production_id)
            client.tracks[1]["offset"] = 7
            return snapshot

        monkeypatch.setattr(client, "get_production", inspect_then_edit)
    with pytest.raises(AuphonicError, match="offset"):
        run()
    assert not client.offsets and not client.started


def test_start_http_request_never_replays_an_ambiguous_response(app_config_factory, tmp_path, monkeypatch):
    import httpx
    from podline.auphonic import AuphonicClient

    attempts = []
    def lost_response(request):
        attempts.append(request.url.path)
        raise httpx.ReadTimeout("lost response", request=request)

    monkeypatch.setattr("podline.auphonic.sleep", lambda seconds: None)
    client = AuphonicClient(app_config_factory(tmp_path))
    client._client = httpx.Client(base_url="https://auphonic.test", transport=httpx.MockTransport(lost_response))
    with pytest.raises(AuphonicError):
        client.start_production("prod-123")
    assert len(attempts) == 1


def test_interrupted_attachment_preserves_uuid_and_never_creates_again(guided_run, monkeypatch):
    paths, session, client, run = guided_run
    def interrupt(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(client, "attach_service_input", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run()
    assert get_session(paths.db_file, session.session_uid).auphonic_production_id == "prod-123"
    with pytest.raises(AuphonicError, match="auphonic-inspect"):
        run()
    assert len(client.created) == 1
    assert not client.started


def test_remote_failure_is_recorded_even_when_restart_allowed(guided_run):
    paths, session, client, run = guided_run
    update_session_status(paths.db_file, session_uid=session.session_uid, from_state="queued",
                          to_state="auphonic_started", timestamp="now", detail="manual start",
                          auphonic_production_id="prod-123")
    client._status, client._status_string = 2, "Error"
    with pytest.raises(AuphonicError, match="Error"):
        run()
    stored = get_session(paths.db_file, session.session_uid)
    assert (stored.status, stored.failure_reason, stored.failed_at_stage) == ("failed", "Error", "auphonic")
    assert not client.started
