from pathlib import Path

import httpx

from podline.config import (
    AppConfig,
    AuphonicConfig,
    CozytrackConfig,
    NotificationsConfig,
    PathsConfig,
    PodbeanConfig,
    PollingConfig,
)
from podline.db import initialize_database
from podline.ingest import scan_and_ingest
from podline.podbean import (
    PodbeanClient,
    PodbeanError,
    PodbeanDraftResult,
    create_podbean_draft,
    _next_episode_number,
)
from podline.repository import (
    get_episode,
    get_session,
    list_sessions,
    update_session_status,
    upsert_episode_metadata,
)


def _default_cozytrack_config() -> CozytrackConfig:
    return CozytrackConfig(
        base_url="http://localhost:3000",
        api_key="",
        download_subdir="cozytrack",
        request_timeout_seconds=600,
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


def test_create_podbean_draft_persists_episode_and_updates_session(
    fixture_raw_root, tmp_path: Path
) -> None:
    db_file = tmp_path / "state.db"
    initialize_database(str(db_file))
    scan_and_ingest(fixture_raw_root, str(db_file))
    session = next(
        item
        for item in list_sessions(str(db_file))
        if item.source_basename == "010124_013143"
    )

    transcript = tmp_path / "transcript.vtt"
    transcript.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n", encoding="utf-8"
    )
    pre_edit = tmp_path / "pre.mp3"
    pre_edit.write_bytes(b"pre")
    final_master = tmp_path / "final.mp3"
    final_master.write_bytes(b"final")
    update_session_status(
        str(db_file),
        session_uid=session.session_uid,
        from_state=session.status,
        to_state="ready_for_draft",
        timestamp="now",
        detail="ready for podbean draft",
        transcript_path=str(transcript),
        pre_edit_master_path=str(pre_edit),
        final_master_path=str(final_master),
    )
    upsert_episode_metadata(
        str(db_file),
        session_uid=session.session_uid,
        title="Is my sponsor God?",
        description="Draft description",
    )
    session = get_session(str(db_file), session.session_uid)
    assert session is not None

    config = AppConfig(
        runtime_home=tmp_path,
        paths=PathsConfig(
            raw_root=tmp_path / "raw", deliverables_root=tmp_path / "deliverables"
        ),
        polling=PollingConfig(interval_seconds=30),
        notifications=NotificationsConfig(enabled=False),
        auphonic=AuphonicConfig(
            enabled=False,
            api_key="",
            base_url="",
            request_timeout_seconds=600,
            poll_interval_seconds=1,
            max_poll_attempts=1,
            preset_ids={},
        ),
        podbean=PodbeanConfig(
            enabled=True,
            client_id="id",
            client_secret="secret",
            base_url="https://api.podbean.test/v1",
            podcast_id="",
        ),
        cozytrack=_default_cozytrack_config(),
        config_file=tmp_path / "config.yaml",
    )

    episode = create_podbean_draft(
        config=config, db_file=str(db_file), session=session, client=FakePodbeanClient()
    )

    assert episode.episode_number == 16
    assert episode.podbean_post_id == "pb-123"
    assert episode.draft_url == "https://podbean.test/draft/pb-123"
    refreshed = get_session(str(db_file), session.session_uid)
    assert refreshed is not None
    assert refreshed.status == "draft_created"
    saved = get_episode(str(db_file), session.session_uid)
    assert saved is not None
    assert saved.episode_number == 16


def test_podbean_client_wraps_http_status_errors(tmp_path: Path) -> None:
    config = AppConfig(
        runtime_home=tmp_path,
        paths=PathsConfig(
            raw_root=tmp_path / "raw", deliverables_root=tmp_path / "deliverables"
        ),
        polling=PollingConfig(interval_seconds=30),
        notifications=NotificationsConfig(enabled=False),
        auphonic=AuphonicConfig(
            enabled=False,
            api_key="",
            base_url="",
            request_timeout_seconds=600,
            poll_interval_seconds=1,
            max_poll_attempts=1,
            preset_ids={},
        ),
        podbean=PodbeanConfig(
            enabled=True,
            client_id="id",
            client_secret="secret",
            base_url="https://api.podbean.test/v1",
            podcast_id="",
        ),
        cozytrack=_default_cozytrack_config(),
        config_file=tmp_path / "config.yaml",
    )
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
