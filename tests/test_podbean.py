from pathlib import Path

import httpx
import pytest

from podline.podbean import (
    PodbeanClient,
    PodbeanError,
    create_podbean_draft,
    _next_episode_number,
)
from podline.repository import (
    get_episode,
    get_session,
    upsert_episode_metadata,
)


class FakePodbeanClient:
    def __init__(self) -> None:
        self.uploaded = []

    def get_access_token(self) -> str:
        return "token-123"

    def list_episodes(self, access_token: str, limit: int = 100):
        assert access_token == "token-123"
        return [{"episode_number": 15}]

    def authorize_upload(self, access_token: str, file_path: Path, content_type: str):
        return {
            "presigned_url": f"https://upload.test/{file_path.name}",
            "file_key": f"key-{file_path.name}",
        }

    def upload_presigned_file(
        self, presigned_url: str, file_path: Path, content_type: str
    ) -> None:
        assert file_path.exists()
        self.uploaded.append((presigned_url, file_path.name, content_type))

    def create_draft_episode(
        self,
        access_token: str,
        *,
        title: str,
        description: str,
        media_key: str,
        transcript_key: str,
        episode_number: int,
    ):
        return {
            "episode": {
                "id": "pb-123",
                "url": "https://podbean.test/draft/pb-123",
            }
        }


def test_next_episode_number_uses_max_existing_number() -> None:
    assert _next_episode_number([{"episode_number": 15}, {"episode_number": 12}]) == 16
    assert _next_episode_number([]) == 1


def test_create_podbean_draft_materializes_artifacts_and_updates_session(
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path: Path,
) -> None:
    config = app_config_factory(tmp_path, podbean_enabled=True)
    runtime_paths = runtime_factory(config)
    session = session_factory(
        runtime_paths.db_file,
        session_uid="session-podbean-1",
        status="ready_for_draft",
    )

    final_master = tmp_path / "final.mp3"
    final_master.write_bytes(b"final")
    transcript = tmp_path / "transcript.vtt"
    transcript.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n", encoding="utf-8"
    )
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid=session.session_uid,
        kind="final_master",
        path=final_master,
    )
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid=session.session_uid,
        kind="transcript",
        path=transcript,
    )
    upsert_episode_metadata(
        runtime_paths.db_file,
        session_uid=session.session_uid,
        title="Is my sponsor God?",
        description="Draft description",
    )
    client = FakePodbeanClient()

    episode = create_podbean_draft(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=session,
        client=client,
    )

    assert episode.episode_number == 16
    assert episode.podbean_post_id == "pb-123"
    assert episode.draft_url == "https://podbean.test/draft/pb-123"
    assert [name for _url, name, _ct in client.uploaded] == [
        "final.mp3",
        "transcript.vtt",
    ]
    refreshed = get_session(runtime_paths.db_file, session.session_uid)
    assert refreshed is not None
    assert refreshed.status == "draft_created"
    saved = get_episode(runtime_paths.db_file, session.session_uid)
    assert saved is not None
    assert saved.episode_number == 16


def test_create_podbean_draft_requires_final_master_artifact(
    app_config_factory, runtime_factory, session_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path, podbean_enabled=True)
    runtime_paths = runtime_factory(config)
    session = session_factory(
        runtime_paths.db_file,
        session_uid="session-podbean-2",
        status="ready_for_draft",
    )
    upsert_episode_metadata(
        runtime_paths.db_file,
        session_uid=session.session_uid,
        title="Title",
        description="Description",
    )

    with pytest.raises(PodbeanError, match="No final_master artifact"):
        create_podbean_draft(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            client=FakePodbeanClient(),
        )


def test_podbean_client_wraps_http_status_errors(
    app_config_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path, podbean_enabled=True)
    client = PodbeanClient(config)
    request = httpx.Request("POST", "https://api.podbean.test/v1/oauth/token")
    response = httpx.Response(401, request=request, text="unauthorized")

    try:
        client._raise_for_status(response, "Podbean OAuth token request")
    except PodbeanError as exc:
        assert "status 401" in str(exc)
        assert "unauthorized" in str(exc)
    else:
        raise AssertionError("Expected PodbeanError for wrapped HTTP failure")


def test_podbean_presigned_upload_retries_after_connection_reset(
    app_config_factory, tmp_path: Path, monkeypatch
) -> None:
    config = app_config_factory(tmp_path, podbean_enabled=True)
    attempts = 0
    uploaded_bodies = []
    delays = []

    def handle_upload(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        uploaded_bodies.append(request.read())
        if attempts == 1:
            raise httpx.ReadError("Connection reset by peer", request=request)
        return httpx.Response(200, request=request)

    client = PodbeanClient(config)
    client._client = httpx.Client(transport=httpx.MockTransport(handle_upload))
    media_file = tmp_path / "final.mp3"
    media_file.write_bytes(b"final audio")
    monkeypatch.setattr(
        "podline.podbean.sleep",
        lambda seconds: delays.append(seconds),
    )

    client.upload_presigned_file(
        "https://uploads.podbean.test/final.mp3",
        media_file,
        "audio/mpeg",
    )

    assert attempts == 2
    assert uploaded_bodies == [b"final audio", b"final audio"]
    assert delays == [2.0]


def test_podbean_presigned_upload_stops_after_bounded_transport_retries(
    app_config_factory, tmp_path: Path, monkeypatch
) -> None:
    config = app_config_factory(tmp_path, podbean_enabled=True)
    attempts = 0
    delays = []

    def fail_upload(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        request.read()
        raise httpx.ReadError("Connection reset by peer", request=request)

    client = PodbeanClient(config)
    client._client = httpx.Client(transport=httpx.MockTransport(fail_upload))
    media_file = tmp_path / "final.mp3"
    media_file.write_bytes(b"final audio")
    monkeypatch.setattr(
        "podline.podbean.sleep", lambda seconds: delays.append(seconds)
    )

    with pytest.raises(PodbeanError, match="failed after 3 attempts"):
        client.upload_presigned_file(
            "https://uploads.podbean.test/final.mp3",
            media_file,
            "audio/mpeg",
        )

    assert attempts == 3
    assert delays == [2.0, 4.0]


@pytest.fixture
def draft_args(app_config_factory, runtime_factory, session_factory, monkeypatch, tmp_path):
    config = app_config_factory(tmp_path, podbean_enabled=True)
    paths = runtime_factory(config)
    session = session_factory(paths.db_file, session_uid="resume", status="ready_for_draft")
    upsert_episode_metadata(paths.db_file, session_uid="resume", title="Title", description="Notes")
    media = tmp_path / "audio.mp3"
    media.touch()
    monkeypatch.setattr("podline.podbean.materialize_artifact", lambda **kwargs: media)
    return dict(config=config, runtime_paths=paths, db_file=paths.db_file, session=session)


def test_saved_draft_replay_returns_without_uploads_even_with_stale_session(draft_args):
    original = create_podbean_draft(**draft_args, client=FakePodbeanClient())
    retry_client = FakePodbeanClient()
    assert create_podbean_draft(**draft_args, client=retry_client) == original
    assert retry_client.uploaded == []


@pytest.mark.parametrize("id_field", ["id", "episode_id", "post_id", None])
def test_lost_draft_response_requires_reconciliation_before_retry(draft_args, id_field):
    from podline import podbean
    from podline.auphonic import AuphonicError, reconcile_production_to_session

    recovery = {k: v for k, v in draft_args.items() if k != "runtime_paths"}

    class InterruptedClient(FakePodbeanClient):
        def create_draft_episode(self, access_token, **fields):
            pending = podbean.get_pending_draft(draft_args["db_file"], "resume")
            assert (pending.title, pending.episode_number) == ("Title", 16)
            with pytest.raises(PodbeanError, match="in progress"):
                create_podbean_draft(**draft_args, client=self)
            with pytest.raises(PodbeanError, match="in progress"):
                podbean.recover_podbean_draft(**recovery, confirm_absent=True)
            with pytest.raises(AuphonicError, match="in progress"):
                reconcile_production_to_session(**draft_args, production_id="pre-edit", client=object())
            raise httpx.ReadTimeout("lost reply")

    with pytest.raises(PodbeanError, match="Podbean"):
        create_podbean_draft(**draft_args, client=InterruptedClient())
    with pytest.raises(AuphonicError, match="confirm-absent"):
        reconcile_production_to_session(**draft_args, production_id="pre-edit", client=object())
    fresh_client = FakePodbeanClient()
    with pytest.raises(PodbeanError, match="confirm-absent"):
        create_podbean_draft(**draft_args, client=fresh_client)
    assert fresh_client.uploaded == []
    if id_field:
        result = {id_field: "abc", "title": "Different", "episode_number": 16, "status": "draft"}
        fresh_client.get_remote_episode = lambda token, post_id: {"episode": result}
        with pytest.raises(PodbeanError, match="match"):
            podbean.recover_podbean_draft(**recovery, existing_id="abc", client=fresh_client)
        result["title"] = "Title"
        with pytest.raises(PodbeanError, match="match"):
            podbean.recover_podbean_draft(**recovery, existing_id="wrong", client=fresh_client)
        saved = podbean.recover_podbean_draft(**recovery, existing_id="abc", client=fresh_client)
        assert saved.podbean_post_id == "abc"
        assert get_session(draft_args["db_file"], "resume").status == "draft_created"
    else:
        assert podbean.recover_podbean_draft(**recovery, confirm_absent=True) is None
        assert create_podbean_draft(**draft_args, client=fresh_client).podbean_post_id == "pb-123"
    assert podbean.get_pending_draft(draft_args["db_file"], "resume") is None


def test_legacy_partial_save_repairs_status_without_contacting_podbean(draft_args):
    from podline.db import connect

    with connect(draft_args["db_file"]) as connection:
        connection.execute("UPDATE episodes SET podbean_post_id='legacy', episode_number=7")
    client = FakePodbeanClient()
    saved = create_podbean_draft(**draft_args, client=client)
    assert saved.podbean_post_id == "legacy"
    assert client.uploaded == []
    assert get_session(draft_args["db_file"], "resume").status == "draft_created"


def test_draft_stops_when_metadata_changes_during_upload(draft_args):
    from podline.podbean import get_pending_draft

    class ChangedMetadata(FakePodbeanClient):
        def upload_presigned_file(self, *args):
            upsert_episode_metadata(
                draft_args["db_file"], session_uid="resume",
                title="Revised", description="Revised notes",
            )

        def create_draft_episode(self, *args, **kwargs):
            pytest.fail("Draft submission used obsolete metadata")

    with pytest.raises(PodbeanError, match="changed"):
        create_podbean_draft(**draft_args, client=ChangedMetadata())
    assert get_pending_draft(draft_args["db_file"], "resume") is None


def test_draft_recording_failure_rolls_back_and_blocks_duplicate(draft_args):
    from podline.db import connect
    from podline.podbean import get_pending_draft

    db_file = draft_args["db_file"]
    with connect(db_file) as conn:
        conn.execute(
            """CREATE TRIGGER fail_draft_event BEFORE INSERT ON events
               WHEN NEW.to_state='draft_created'
               BEGIN SELECT RAISE(ABORT, 'event save failed'); END"""
        )
    with pytest.raises(PodbeanError, match="event save failed"):
        create_podbean_draft(**draft_args, client=FakePodbeanClient())
    assert get_episode(db_file, "resume").podbean_post_id is None
    assert get_session(db_file, "resume").status == "ready_for_draft"
    assert get_pending_draft(db_file, "resume").episode_number == 16
    with pytest.raises(PodbeanError, match="confirm-absent"):
        create_podbean_draft(**draft_args, client=FakePodbeanClient())


def test_pending_draft_preserves_metadata_and_transcript(draft_args):
    from podline.auphonic import AuphonicError, refresh_transcript_with_auphonic
    from podline.episode_meta import EpisodeMetaError, generate_episode_metadata

    class MissingId(FakePodbeanClient):
        def create_draft_episode(self, *args, **kwargs):
            return {"episode": {}}

    with pytest.raises(PodbeanError, match="episode id"):
        create_podbean_draft(**draft_args, client=MissingId())
    with pytest.raises(AuphonicError, match="confirm-absent"):
        refresh_transcript_with_auphonic(**draft_args, client=object())
    with pytest.raises(EpisodeMetaError, match="confirm-absent"):
        generate_episode_metadata(**draft_args, client=object())
    assert get_episode(draft_args["db_file"], "resume").title == "Title"


@pytest.mark.parametrize("details", [
    {"status": "publish"}, {"title": "Another"}, {"episode_number": 2}, {"id": []},
])
def test_unexpected_create_response_keeps_attempt_pending(draft_args, details):
    from podline.podbean import get_pending_draft

    class UnexpectedResult(FakePodbeanClient):
        def create_draft_episode(self, *args, **kwargs):
            return {"episode": {"id": "pb-123", **details}}

    with pytest.raises(PodbeanError, match="confirm-absent"):
        create_podbean_draft(**draft_args, client=UnexpectedResult())
    assert get_pending_draft(draft_args["db_file"], "resume") is not None


def test_recovery_reads_authenticated_episode_endpoint(app_config_factory, tmp_path):
    config = app_config_factory(tmp_path, podbean_enabled=True)
    client = PodbeanClient(config)
    def lookup(request):
        assert request.method == "GET"
        assert request.url.path.endswith("/episodes/abc123")
        assert request.url.params["access_token"] == "scoped-token"
        return httpx.Response(200, json={"episode": {"id": "abc123"}})

    client._client = httpx.Client(transport=httpx.MockTransport(lookup))
    assert client.get_remote_episode("scoped-token", "abc123")["episode"]["id"] == "abc123"


def test_preparation_timeout_retries_without_reconciliation(draft_args):
    from podline.podbean import get_pending_draft

    class Unavailable(FakePodbeanClient):
        def authorize_upload(self, *args):
            raise httpx.ConnectTimeout("down")

    with pytest.raises(PodbeanError, match="before draft submission"):
        create_podbean_draft(**draft_args, client=Unavailable())
    assert get_pending_draft(draft_args["db_file"], "resume") is None
    saved = create_podbean_draft(**draft_args, client=FakePodbeanClient())
    assert saved.podbean_post_id == "pb-123"
