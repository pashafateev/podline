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
