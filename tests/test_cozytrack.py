from pathlib import Path
from typing import List, Optional

import httpx
import pytest

from sexdrunks_automation.config import (
    AppConfig,
    AuphonicConfig,
    CozytrackConfig,
    NotificationsConfig,
    PathsConfig,
    PodbeanConfig,
    PollingConfig,
)
from sexdrunks_automation.cozytrack.client import (
    CozytrackClient,
    CozytrackError,
    CozytrackSession,
    CozytrackTrack,
)
from sexdrunks_automation.cozytrack.ingest import (
    CozytrackIngestError,
    ingest_session,
    list_ready_sessions_for_ingest,
    slugify,
)
from sexdrunks_automation.db import initialize_database
from sexdrunks_automation.repository import (
    get_session_by_cozytrack_id,
    list_cozytrack_session_ids,
)
from sexdrunks_automation.scanner import scan_raw_sessions


def _build_config(tmp_path: Path, *, api_key: str = "test-key") -> AppConfig:
    return AppConfig(
        runtime_home=tmp_path / "runtime",
        paths=PathsConfig(
            raw_root=tmp_path / "raw",
            deliverables_root=tmp_path / "deliverables",
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
            enabled=False,
            client_id="",
            client_secret="",
            base_url="",
            podcast_id="",
        ),
        cozytrack=CozytrackConfig(
            base_url="https://cozytrack.test",
            api_key=api_key,
            download_subdir="cozytrack",
            request_timeout_seconds=60,
        ),
        config_file=tmp_path / "config.yaml",
    )


class FakeCozytrackClient:
    def __init__(
        self,
        *,
        sessions: Optional[List[CozytrackSession]] = None,
        downloads: Optional[dict] = None,
        download_failures: Optional[List[str]] = None,
    ) -> None:
        self.sessions = {s.id: s for s in (sessions or [])}
        self.downloads = downloads or {}
        self.download_failures = set(download_failures or [])
        self.requested_track_ids: List[str] = []
        self.downloaded_paths: List[Path] = []

    def list_ready_sessions(self) -> List[CozytrackSession]:
        return list(self.sessions.values())

    def get_session(self, session_id: str) -> CozytrackSession:
        if session_id not in self.sessions:
            raise CozytrackError(f"unknown session {session_id}")
        return self.sessions[session_id]

    def get_track_download_url(self, track_id: str) -> str:
        self.requested_track_ids.append(track_id)
        return f"https://s3.example/{track_id}"

    def download_to_file(self, url: str, destination: Path) -> None:
        track_id = url.rsplit("/", 1)[-1]
        if track_id in self.download_failures:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"partial-bytes")
            raise CozytrackError(f"simulated download failure for {track_id}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.downloads.get(track_id, b"audio-data"))
        self.downloaded_paths.append(destination)


def _make_session(
    *,
    session_id: str,
    name: str = "Pilot Episode",
    finalized_at: str = "2026-04-24T12:34:56.000Z",
    track_ids: Optional[List[str]] = None,
) -> CozytrackSession:
    track_ids = track_ids or ["track-a", "track-b"]
    return CozytrackSession(
        id=session_id,
        name=name,
        status="ready",
        finalized_at=finalized_at,
        tracks=[
            CozytrackTrack(
                id=track_id,
                participant_name=f"speaker-{i + 1}",
                status="complete",
                created_at=f"2026-04-24T12:00:0{i}Z",
            )
            for i, track_id in enumerate(track_ids)
        ],
    )


def test_client_list_ready_sessions_uses_api_key_header(tmp_path: Path) -> None:
    config = _build_config(tmp_path)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers.get("X-API-Key")
        assert request.url.params.get("status") == "ready"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "sess-1",
                    "name": "Pilot",
                    "status": "ready",
                    "finalizedAt": "2026-04-24T12:00:00Z",
                    "tracks": [
                        {
                            "id": "tr-1",
                            "participantName": "Pasha",
                            "status": "complete",
                            "createdAt": "2026-04-24T12:00:01Z",
                        }
                    ],
                }
            ],
        )

    client = CozytrackClient(config)
    client._client = httpx.Client(
        base_url=config.cozytrack.base_url,
        headers={"X-API-Key": config.cozytrack.api_key},
        transport=httpx.MockTransport(handler),
    )

    sessions = client.list_ready_sessions()

    assert captured["api_key"] == "test-key"
    assert captured["url"].endswith("/api/ingest/sessions?status=ready")
    assert len(sessions) == 1
    assert sessions[0].id == "sess-1"
    assert sessions[0].finalized_at == "2026-04-24T12:00:00Z"
    assert sessions[0].tracks[0].participant_name == "Pasha"


def test_client_get_track_download_url_returns_presigned_url(tmp_path: Path) -> None:
    config = _build_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ingest/tracks/track-1/download"
        return httpx.Response(200, json={"url": "https://s3.example/presigned"})

    client = CozytrackClient(config)
    client._client = httpx.Client(
        base_url=config.cozytrack.base_url,
        headers={"X-API-Key": config.cozytrack.api_key},
        transport=httpx.MockTransport(handler),
    )

    url = client.get_track_download_url("track-1")
    assert url == "https://s3.example/presigned"


def test_client_raises_typed_error_with_response_body(tmp_path: Path) -> None:
    config = _build_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    client = CozytrackClient(config)
    client._client = httpx.Client(
        base_url=config.cozytrack.base_url,
        headers={"X-API-Key": config.cozytrack.api_key},
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CozytrackError) as excinfo:
        client.list_ready_sessions()

    assert "401" in str(excinfo.value)
    assert "unauthorized" in str(excinfo.value)


def test_client_requires_api_key(tmp_path: Path) -> None:
    config = _build_config(tmp_path, api_key="")
    with pytest.raises(CozytrackError):
        CozytrackClient(config)


def test_list_ready_sessions_filters_already_ingested(tmp_path: Path) -> None:
    config = _build_config(tmp_path)
    db_file = tmp_path / "state.db"
    initialize_database(str(db_file))

    fake = FakeCozytrackClient(
        sessions=[
            _make_session(session_id="sess-new"),
            _make_session(session_id="sess-old"),
        ]
    )

    # Pre-ingest sess-old
    ingest_session(
        config=config,
        db_file=str(db_file),
        cozytrack_session_id="sess-old",
        client=fake,
    )

    visible = list_ready_sessions_for_ingest(
        config=config,
        db_file=str(db_file),
        client=fake,
    )

    assert [session.id for session in visible] == ["sess-new"]
    assert "sess-old" in list_cozytrack_session_ids(str(db_file))


def test_ingest_session_happy_path(tmp_path: Path) -> None:
    config = _build_config(tmp_path)
    db_file = tmp_path / "state.db"
    initialize_database(str(db_file))

    fake = FakeCozytrackClient(
        sessions=[_make_session(session_id="sess-1", name="Boba Boom")]
    )

    record = ingest_session(
        config=config,
        db_file=str(db_file),
        cozytrack_session_id="sess-1",
        client=fake,
    )

    assert record.cozytrack_session_id == "sess-1"
    folder = Path(record.source_path)
    assert folder.exists()
    assert folder.parent.name == "cozytrack"
    assert folder.name == "2026-04-24_boba-boom"
    assert (folder / "Tr1.webm").exists()
    assert (folder / "Tr2.webm").exists()
    assert fake.requested_track_ids == ["track-a", "track-b"]


def test_ingest_session_is_idempotent(tmp_path: Path) -> None:
    config = _build_config(tmp_path)
    db_file = tmp_path / "state.db"
    initialize_database(str(db_file))

    fake = FakeCozytrackClient(
        sessions=[_make_session(session_id="sess-1", name="Repeat")]
    )

    first = ingest_session(
        config=config,
        db_file=str(db_file),
        cozytrack_session_id="sess-1",
        client=fake,
    )
    track_calls_after_first = list(fake.requested_track_ids)

    second = ingest_session(
        config=config,
        db_file=str(db_file),
        cozytrack_session_id="sess-1",
        client=fake,
    )

    assert first.session_uid == second.session_uid
    assert second.source_path == first.source_path
    # No new download requests on the second call
    assert fake.requested_track_ids == track_calls_after_first


def test_ingest_session_cleans_up_partial_downloads_on_failure(tmp_path: Path) -> None:
    config = _build_config(tmp_path)
    db_file = tmp_path / "state.db"
    initialize_database(str(db_file))

    fake = FakeCozytrackClient(
        sessions=[
            _make_session(
                session_id="sess-fail",
                name="Boom",
                track_ids=["good-track", "broken-track"],
            )
        ],
        download_failures=["broken-track"],
    )

    with pytest.raises(CozytrackIngestError):
        ingest_session(
            config=config,
            db_file=str(db_file),
            cozytrack_session_id="sess-fail",
            client=fake,
        )

    folder = config.paths.raw_root / "cozytrack" / "2026-04-24_boom"
    assert folder.exists()
    # Both written files (the successful download AND the partial broken file)
    # must have been removed so the next attempt starts clean.
    assert list(folder.iterdir()) == []
    # The DB row must NOT have been created since ingest failed.
    assert get_session_by_cozytrack_id(str(db_file), "sess-fail") is None


def test_ingest_session_uses_name_override_for_slug(tmp_path: Path) -> None:
    config = _build_config(tmp_path)
    db_file = tmp_path / "state.db"
    initialize_database(str(db_file))

    fake = FakeCozytrackClient(
        sessions=[_make_session(session_id="sess-1", name="Original Name")]
    )

    record = ingest_session(
        config=config,
        db_file=str(db_file),
        cozytrack_session_id="sess-1",
        name_override="Custom Title!",
        client=fake,
    )

    folder = Path(record.source_path)
    assert folder.name == "2026-04-24_custom-title"


def test_classifier_handles_two_track_webm_session(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    session_dir = raw_root / "2026-04-24_demo"
    session_dir.mkdir(parents=True)

    (session_dir / "Tr1.webm").write_bytes(b"webm-track-1-data" * 32)
    (session_dir / "Tr2.webm").write_bytes(b"webm-track-2-data" * 32)

    scan_result = scan_raw_sessions(raw_root)

    assert len(scan_result.candidates) == 1
    candidate = scan_result.candidates[0]
    assert [track.track_name for track in candidate.spoken_tracks] == ["Tr1", "Tr2"]
    assert [track.role for track in candidate.spoken_tracks] == ["host1", "host2"]


def test_slugify_handles_unicode_and_punctuation() -> None:
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("   ") == "session"
    assert slugify("ep07: Boba & Beats") == "ep07-boba-beats"
