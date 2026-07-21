from pathlib import Path
from typing import List, Optional

import httpx
import pytest

from podline.artifacts import cache_path_for
from podline.cozytrack.client import (
    CozytrackClient,
    CozytrackError,
    CozytrackSession,
    CozytrackTrack,
    host_channel_number,
    is_host_participant,
)
from podline.cozytrack.ingest import (
    CozytrackIngestError,
    ingest_session,
    list_ready_sessions_for_ingest,
    slugify,
)
from podline.db import connect
from podline.repository import (
    get_session_by_cozytrack_id,
    list_assets_for_session,
    list_cozytrack_session_ids,
    list_media_artifacts,
)
from podline.scanner import scan_raw_sessions


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
        destination.write_bytes(
            self.downloads.get(track_id, f"audio-data-{track_id}".encode("utf-8"))
        )
        self.downloaded_paths.append(destination)


@pytest.mark.parametrize(
    ("participant_id", "expected"),
    [
        ("host", True),
        ("host-local-ch-1", True),
        ("host-local-ch-2", True),
        ("guest-1", False),
        ("", False),
    ],
)
def test_is_host_participant_uses_cozytrack_identity_contract(
    participant_id: str, expected: bool
) -> None:
    assert is_host_participant(participant_id) is expected


@pytest.mark.parametrize(
    ("participant_id", "expected"),
    [
        ("host", 1),
        ("host-local-ch-1", 1),
        ("host-local-ch-2", 2),
        ("host-local-ch-12", 12),
        ("host-local-ch-", None),
        ("host-local-ch-x", None),
        ("host-local-ch-0", None),
        ("guest-1", None),
        ("", None),
    ],
)
def test_host_channel_number_parses_identity_contract(
    participant_id: str, expected
) -> None:
    assert host_channel_number(participant_id) == expected


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


def _probe(path: Path) -> float:
    return 60.0


def test_client_list_ready_sessions_uses_api_key_header(
    app_config_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")
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
                            "participantId": "host-local-ch-1",
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
    assert sessions[0].tracks[0].participant_id == "host-local-ch-1"


def test_client_get_track_download_url_returns_presigned_url(
    app_config_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")

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


def test_client_raises_typed_error_with_response_body(
    app_config_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")

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


def test_client_requires_api_key(app_config_factory, tmp_path: Path) -> None:
    config = app_config_factory(tmp_path, cozytrack_api_key="")
    with pytest.raises(CozytrackError):
        CozytrackClient(config)


def test_list_ready_sessions_filters_already_ingested(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")
    runtime_paths = runtime_factory(config)

    fake = FakeCozytrackClient(
        sessions=[
            _make_session(session_id="sess-new"),
            _make_session(session_id="sess-old"),
        ]
    )

    ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-old",
        client=fake,
        duration_probe=_probe,
    )

    visible = list_ready_sessions_for_ingest(
        config=config,
        db_file=runtime_paths.db_file,
        client=fake,
    )

    assert [session.id for session in visible] == ["sess-new"]
    assert "sess-old" in list_cozytrack_session_ids(runtime_paths.db_file)


def test_ingest_session_uploads_artifacts_and_cleans_staging(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")
    runtime_paths = runtime_factory(config)

    fake = FakeCozytrackClient(
        sessions=[_make_session(session_id="sess-1", name="Boba Boom")]
    )

    record = ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-1",
        client=fake,
        duration_probe=_probe,
    )

    assert record.cozytrack_session_id == "sess-1"
    assert record.source_basename == "2026-04-24_boba-boom"
    assert fake.requested_track_ids == ["track-a", "track-b"]

    artifacts = list_media_artifacts(runtime_paths.db_file, record.session_uid)
    assert [(a.kind, a.role, a.filename) for a in artifacts] == [
        ("raw_track", "host1", "Tr1.webm"),
        ("raw_track", "host2", "Tr2.webm"),
    ]
    assert [a.source_id for a in artifacts] == ["track-a", "track-b"]
    assert all(a.source_provider == "cozytrack" for a in artifacts)
    assert all(a.duration_seconds == 60.0 for a in artifacts)

    # Staging folder is disposable and removed after registration; the cache
    # keeps the content.
    staging = Path(runtime_paths.staging_dir) / "ct-ingest" / "2026-04-24_boba-boom"
    assert not staging.exists()
    for artifact in artifacts:
        assert cache_path_for(
            runtime_paths, artifact.sha256, artifact.filename
        ).exists()


def _make_identified_session(
    *,
    session_id: str,
    name: str = "Mixed Session",
    tracks,
) -> CozytrackSession:
    """Session whose tracks carry stable participant ids, created in the
    given order (Cozytrack creates rows in recorder start order)."""
    return CozytrackSession(
        id=session_id,
        name=name,
        status="ready",
        finalized_at="2026-07-18T17:00:00.000Z",
        tracks=[
            CozytrackTrack(
                id=track_id,
                participant_name=participant_name,
                status="complete",
                created_at=f"2026-07-18T16:52:{39 + index:02d}Z",
                participant_id=participant_id,
            )
            for index, (track_id, participant_id, participant_name) in enumerate(
                tracks
            )
        ],
    )


def test_ingest_session_derives_roles_from_participant_ids(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    """A guest recorder interleaved between the host-local channels must not
    shift naming or roles: files follow recorder order (host channels, then
    guests) and roles follow participant identity."""
    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")
    runtime_paths = runtime_factory(config)

    fake = FakeCozytrackClient(
        sessions=[
            _make_identified_session(
                session_id="sess-mixed",
                tracks=[
                    ("ct-ch-1", "host-local-ch-1", "Local Ch 1"),
                    ("ct-guest", "guest_df6e4c0f", "J"),
                    ("ct-ch-2", "host-local-ch-2", "Local Ch 2"),
                ],
            )
        ]
    )

    record = ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-mixed",
        client=fake,
        duration_probe=_probe,
    )

    assert fake.requested_track_ids == ["ct-ch-1", "ct-ch-2", "ct-guest"]
    artifacts = list_media_artifacts(runtime_paths.db_file, record.session_uid)
    assert [(a.role, a.filename, a.source_id) for a in artifacts] == [
        ("guest1", "Tr3.webm", "ct-guest"),
        ("host1", "Tr1.webm", "ct-ch-1"),
        ("host2", "Tr2.webm", "ct-ch-2"),
    ]


def test_ingest_session_rejects_partially_identified_tracks(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")
    runtime_paths = runtime_factory(config)

    fake = FakeCozytrackClient(
        sessions=[
            _make_identified_session(
                session_id="sess-partial",
                tracks=[
                    ("ct-ch-1", "host-local-ch-1", "Local Ch 1"),
                    ("ct-guest", None, "J"),
                ],
            )
        ]
    )

    with pytest.raises(CozytrackIngestError, match="missing participantId: ct-guest"):
        ingest_session(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            cozytrack_session_id="sess-partial",
            client=fake,
            duration_probe=_probe,
        )

    assert fake.requested_track_ids == []
    assert get_session_by_cozytrack_id(runtime_paths.db_file, "sess-partial") is None


def test_ingest_session_rejects_duplicate_participant_roles(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")
    runtime_paths = runtime_factory(config)

    fake = FakeCozytrackClient(
        sessions=[
            _make_identified_session(
                session_id="sess-dup",
                tracks=[
                    ("ct-host", "host", "Host"),
                    ("ct-ch-1", "host-local-ch-1", "Local Ch 1"),
                    ("ct-guest", "guest_a", "A"),
                ],
            )
        ]
    )

    with pytest.raises(CozytrackIngestError, match="duplicate role 'host1'"):
        ingest_session(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            cozytrack_session_id="sess-dup",
            client=fake,
            duration_probe=_probe,
        )


def _reconcile_event_count(db_file: str, session_uid: str) -> int:
    with connect(db_file) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM events WHERE session_uid = ? AND detail LIKE 'reconciled raw track roles%'",
            (session_uid,),
        ).fetchone()
    return row["n"]


def test_reingest_reconciles_roles_of_previously_mislabeled_session(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    """Sessions ingested before participant-derived roles keep download-order
    labels; re-running ingest migrates them once identity is available."""
    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")
    runtime_paths = runtime_factory(config)

    # Ingested while Cozytrack exposed no participant ids: creation order
    # [ch-1, guest, ch-2] mislabels the guest as host2 and ch-2 as guest1.
    fake = FakeCozytrackClient(
        sessions=[
            _make_session(
                session_id="sess-legacy",
                track_ids=["ct-ch-1", "ct-guest", "ct-ch-2"],
            )
        ]
    )
    record = ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-legacy",
        client=fake,
        duration_probe=_probe,
    )
    artifacts = list_media_artifacts(runtime_paths.db_file, record.session_uid)
    assert [(a.role, a.filename, a.source_id) for a in artifacts] == [
        ("guest1", "Tr3.webm", "ct-ch-2"),
        ("host1", "Tr1.webm", "ct-ch-1"),
        ("host2", "Tr2.webm", "ct-guest"),
    ]

    # Cozytrack now exposes participant ids for the same tracks.
    fake.sessions["sess-legacy"] = _make_identified_session(
        session_id="sess-legacy",
        tracks=[
            ("ct-ch-1", "host-local-ch-1", "Local Ch 1"),
            ("ct-guest", "guest_j", "J"),
            ("ct-ch-2", "host-local-ch-2", "Local Ch 2"),
        ],
    )

    again = ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-legacy",
        client=fake,
        duration_probe=_probe,
    )

    assert again.session_uid == record.session_uid
    artifacts = list_media_artifacts(runtime_paths.db_file, record.session_uid)
    assert [(a.role, a.filename, a.source_id) for a in artifacts] == [
        ("guest1", "Tr2.webm", "ct-guest"),
        ("host1", "Tr1.webm", "ct-ch-1"),
        ("host2", "Tr3.webm", "ct-ch-2"),
    ]
    # S3-native registration writes no legacy assets rows; reconciliation
    # must not invent any.
    assert list_assets_for_session(runtime_paths.db_file, record.session_uid) == []
    assert _reconcile_event_count(runtime_paths.db_file, record.session_uid) == 1

    # Re-running once roles already match is a no-op.
    ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-legacy",
        client=fake,
        duration_probe=_probe,
    )
    assert _reconcile_event_count(runtime_paths.db_file, record.session_uid) == 1


def test_reingest_leaves_roles_alone_when_tracks_cannot_be_mapped(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")
    runtime_paths = runtime_factory(config)

    fake = FakeCozytrackClient(
        sessions=[
            _make_session(
                session_id="sess-unmappable",
                track_ids=["ct-a", "ct-b"],
            )
        ]
    )
    record = ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-unmappable",
        client=fake,
        duration_probe=_probe,
    )

    # Identity arrives, but under track ids that do not match the stored
    # source ids — reconciliation must refuse to guess.
    fake.sessions["sess-unmappable"] = _make_identified_session(
        session_id="sess-unmappable",
        tracks=[
            ("other-1", "host-local-ch-1", "Local Ch 1"),
            ("other-2", "guest_x", "X"),
        ],
    )

    again = ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-unmappable",
        client=fake,
        duration_probe=_probe,
    )

    assert again.session_uid == record.session_uid
    artifacts = list_media_artifacts(runtime_paths.db_file, record.session_uid)
    assert [(a.role, a.filename) for a in artifacts] == [
        ("host1", "Tr1.webm"),
        ("host2", "Tr2.webm"),
    ]
    assert _reconcile_event_count(runtime_paths.db_file, record.session_uid) == 0


def test_deduplicated_ingest_backfills_source_ids_and_roles(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    """Same audio imported locally first, then ingested from Cozytrack with
    participant identity: the surviving session must gain source ids
    (paired by content hash — a local import stores none) and
    participant-derived roles, or offset topology can never resolve."""
    from podline.ingest import import_local_folder

    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")
    runtime_paths = runtime_factory(config)

    tr1_bytes = b"webm-host-channel-data" * 32
    tr2_bytes = b"webm-remote-guest-data" * 32
    import_dir = tmp_path / "riverside" / "2026-07-18_demo"
    import_dir.mkdir(parents=True)
    (import_dir / "Tr1.webm").write_bytes(tr1_bytes)
    (import_dir / "Tr2.webm").write_bytes(tr2_bytes)
    imported, created = import_local_folder(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        folder=import_dir,
        duration_probe=_probe,
    )
    assert created is True
    artifacts = list_media_artifacts(runtime_paths.db_file, imported.session_uid)
    assert [(a.role, a.source_id) for a in artifacts] == [
        ("host1", None),
        ("host2", None),
    ]

    fake = FakeCozytrackClient(
        sessions=[
            _make_identified_session(
                session_id="sess-import-dup",
                tracks=[
                    ("ct-host", "host", "Host"),
                    ("ct-guest", "guest_x", "X"),
                ],
            )
        ],
        downloads={"ct-host": tr1_bytes, "ct-guest": tr2_bytes},
    )

    record = ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-import-dup",
        client=fake,
        duration_probe=_probe,
    )

    assert record.session_uid == imported.session_uid
    assert record.cozytrack_session_id == "sess-import-dup"
    artifacts = list_media_artifacts(runtime_paths.db_file, record.session_uid)
    assert [(a.role, a.filename, a.source_id, a.source_provider) for a in artifacts] == [
        ("guest1", "Tr2.webm", "ct-guest", "local_import"),
        ("host1", "Tr1.webm", "ct-host", "local_import"),
    ]
    assert _reconcile_event_count(runtime_paths.db_file, record.session_uid) == 1

    # The healed session behaves like a native ct-ingest on later re-runs.
    ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-import-dup",
        client=fake,
        duration_probe=_probe,
    )
    assert _reconcile_event_count(runtime_paths.db_file, record.session_uid) == 1


def test_reingest_resumes_interrupted_source_id_migration(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    """A session associated with Cozytrack but left without source ids (an
    interrupted or pre-migration run) must not be stranded behind the
    idempotent return: re-running ct-ingest re-downloads, joins the ids by
    content, and reconciles roles — converging on the healed state."""
    from podline.ingest import import_local_folder
    from podline.repository import set_cozytrack_session_id

    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")
    runtime_paths = runtime_factory(config)

    tr1_bytes = b"webm-host-channel-data" * 32
    tr2_bytes = b"webm-remote-guest-data" * 32
    import_dir = tmp_path / "riverside" / "2026-07-18_interrupted"
    import_dir.mkdir(parents=True)
    (import_dir / "Tr1.webm").write_bytes(tr1_bytes)
    (import_dir / "Tr2.webm").write_bytes(tr2_bytes)
    imported, _created = import_local_folder(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        folder=import_dir,
        duration_probe=_probe,
    )

    # The stuck state the old code (or a crash before the atomic backfill
    # existed) leaves behind: association persisted, source ids never were.
    assert set_cozytrack_session_id(
        runtime_paths.db_file,
        session_uid=imported.session_uid,
        cozytrack_session_id="sess-interrupted",
        status=imported.status,
        timestamp="2026-07-18T00:00:00+00:00",
    )
    artifacts = list_media_artifacts(runtime_paths.db_file, imported.session_uid)
    assert [(a.role, a.source_id) for a in artifacts] == [
        ("host1", None),
        ("host2", None),
    ]

    fake = FakeCozytrackClient(
        sessions=[
            _make_identified_session(
                session_id="sess-interrupted",
                tracks=[
                    ("ct-host", "host", "Host"),
                    ("ct-guest", "guest_x", "X"),
                ],
            )
        ],
        downloads={"ct-host": tr1_bytes, "ct-guest": tr2_bytes},
    )

    record = ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-interrupted",
        client=fake,
        duration_probe=_probe,
    )

    assert record.session_uid == imported.session_uid
    assert fake.requested_track_ids == ["ct-host", "ct-guest"]
    artifacts = list_media_artifacts(runtime_paths.db_file, record.session_uid)
    assert [(a.role, a.filename, a.source_id) for a in artifacts] == [
        ("guest1", "Tr2.webm", "ct-guest"),
        ("host1", "Tr1.webm", "ct-host"),
    ]
    assert _reconcile_event_count(runtime_paths.db_file, record.session_uid) == 1

    # Healed: the next run takes the cheap idempotent path again.
    downloads_after_heal = len(fake.requested_track_ids)
    ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-interrupted",
        client=fake,
        duration_probe=_probe,
    )
    assert len(fake.requested_track_ids) == downloads_after_heal
    assert _reconcile_event_count(runtime_paths.db_file, record.session_uid) == 1


def test_ingest_session_is_idempotent(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")
    runtime_paths = runtime_factory(config)

    fake = FakeCozytrackClient(
        sessions=[_make_session(session_id="sess-1", name="Repeat")]
    )

    first = ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-1",
        client=fake,
        duration_probe=_probe,
    )
    track_calls_after_first = list(fake.requested_track_ids)

    second = ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-1",
        client=fake,
        duration_probe=_probe,
    )

    assert first.session_uid == second.session_uid
    # No new download requests on the second call
    assert fake.requested_track_ids == track_calls_after_first


def test_ingest_session_cleans_up_staging_on_failure(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")
    runtime_paths = runtime_factory(config)

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
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            cozytrack_session_id="sess-fail",
            client=fake,
            duration_probe=_probe,
        )

    staging = Path(runtime_paths.staging_dir) / "ct-ingest" / "2026-04-24_boom"
    assert not staging.exists()
    assert get_session_by_cozytrack_id(runtime_paths.db_file, "sess-fail") is None


def test_ingest_session_uses_name_override_for_slug(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")
    runtime_paths = runtime_factory(config)

    fake = FakeCozytrackClient(
        sessions=[_make_session(session_id="sess-1", name="Original Name")]
    )

    record = ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-1",
        name_override="Custom Title!",
        client=fake,
        duration_probe=_probe,
    )

    assert record.source_basename == "2026-04-24_custom-title"


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


def test_deduplicated_ingest_backfills_cozytrack_identity(
    moto_s3, app_config_factory, runtime_factory, tmp_path: Path
) -> None:
    """Same audio imported locally first, then ingested from Cozytrack.

    Dedup must not create a second session, and the surviving session must
    take over the Cozytrack identity so ct-list stops advertising it and
    later ingests stop re-downloading the tracks.
    """
    from podline.ingest import import_local_folder
    from podline.repository import get_session_by_cozytrack_id, list_sessions

    config = app_config_factory(tmp_path, cozytrack_api_key="test-key")
    runtime_paths = runtime_factory(config)

    tr1_bytes = b"webm-track-1-data" * 32
    tr2_bytes = b"webm-track-2-data" * 32
    import_dir = tmp_path / "riverside" / "2026-04-24_demo"
    import_dir.mkdir(parents=True)
    (import_dir / "Tr1.webm").write_bytes(tr1_bytes)
    (import_dir / "Tr2.webm").write_bytes(tr2_bytes)
    imported, created = import_local_folder(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        folder=import_dir,
        duration_probe=_probe,
    )
    assert created is True
    assert imported.cozytrack_session_id is None

    fake = FakeCozytrackClient(
        sessions=[_make_session(session_id="sess-dup")],
        downloads={"track-a": tr1_bytes, "track-b": tr2_bytes},
    )
    record = ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-dup",
        client=fake,
        duration_probe=_probe,
    )

    assert record.session_uid == imported.session_uid
    assert len(list_sessions(runtime_paths.db_file)) == 1
    assert record.cozytrack_session_id == "sess-dup"
    linked = get_session_by_cozytrack_id(runtime_paths.db_file, "sess-dup")
    assert linked is not None
    assert linked.session_uid == imported.session_uid

    ready = list_ready_sessions_for_ingest(
        config=config, db_file=runtime_paths.db_file, client=fake
    )
    assert [session.id for session in ready] == []

    downloads_before_rerun = len(fake.downloaded_paths)
    rerun = ingest_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        cozytrack_session_id="sess-dup",
        client=fake,
        duration_probe=_probe,
    )
    assert rerun.session_uid == imported.session_uid
    assert len(fake.downloaded_paths) == downloads_before_rerun
