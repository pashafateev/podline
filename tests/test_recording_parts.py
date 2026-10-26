from copy import deepcopy
from pathlib import Path

import pytest

from podline.cozytrack.client import _parse_session
from podline.cozytrack.ingest import CozytrackIngestError, ingest_session
from podline.db import connect
from podline.repository import list_media_artifacts


def session_payload(counts=(3, 3, 2)):
    takes = []
    tracks = []
    for number, count in enumerate(counts, 1):
        take_id = f"take-{number}"
        start = f"2026-10-01T12:{number:02d}:00Z"
        takes.append(
            dict(id=take_id, startedAt=start,
                 stoppedAt=f"2026-10-01T12:{number + 1:02d}:00Z",
                 status="stopped")
        )
        participants = ["host-local-ch-1", "host-local-ch-2", "remote-guest"][:count]
        part_tracks = [
            dict(id=f"track-{number}-{index}", takeId=take_id,
                 participantId=participant, participantName=participant,
                 status="complete", partial=False, segmentCount=1,
                 incompleteSegmentCount=0, durationMs=60000,
                 createdAt=start, updatedAt=takes[-1]["stoppedAt"],
                 s3PurgedAt=None)
            for index, participant in enumerate(participants, 1)
        ]
        tracks.extend(reversed(part_tracks) if number == 2 else part_tracks)
    return dict(id="ct-parts", name="Multipart episode", status="recording",
                finalizedAt=None, recordingTakes=list(reversed(takes)), tracks=tracks)


class PartsClient:
    def __init__(self, payload):
        self.payload = payload
        self.downloads = []
        self.fail_track = None

    def get_session(self, session_id):
        assert session_id == self.payload["id"]
        return _parse_session(deepcopy(self.payload))

    def get_track_download_url(self, track_id):
        return f"https://audio.invalid/{track_id}"

    def download_to_file(self, url, destination):
        track_id = url.rsplit("/", 1)[-1]
        self.downloads.append(track_id)
        if track_id == self.fail_track:
            raise RuntimeError("interrupted source download")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f"source:{track_id}".encode())


def ingest(config, runtime, client, **kwargs):
    return ingest_session(
        config=config, runtime_paths=runtime, db_file=runtime.db_file,
        cozytrack_session_id=client.payload["id"], client=client,
        duration_probe=lambda _path: 60.0, **kwargs,
    )


def read_parts(runtime):
    with connect(runtime.db_file) as connection:
        return connection.execute(
            "SELECT * FROM recording_parts ORDER BY part_number"
        ).fetchall()


def test_parser_preserves_take_and_timing_evidence():
    payload = session_payload()
    payload["tracks"][0].update(partial=True, segmentCount=2,
                                incompleteSegmentCount=1)
    session = _parse_session(payload)
    assert {take.id for take in session.recording_takes} == {
        "take-1", "take-2", "take-3"
    }
    track = session.tracks[0]
    assert track.take_id == "take-1"
    assert track.partial is True
    assert track.segment_count == 2
    assert track.incomplete_segment_count == 1
    legacy = _parse_session(dict(id="legacy", tracks=[dict(id="old")]))
    assert legacy.recording_takes is None
    assert legacy.tracks[0].segment_count is None
    assert legacy.tracks[0].partial is None


def test_track_listing_identifies_parts_roles_and_reconnect_evidence():
    from podline.cozytrack.ingest import format_session_track_table
    payload = session_payload()
    payload["tracks"][0]["segmentCount"] = 2
    table = format_session_track_table(_parse_session(payload))
    assert "part-01:host1" in table
    assert "part-02:guest1" in table
    assert "take-3" in table
    assert "reconnect" in table


def test_guest_identity_does_not_compact_when_another_guest_leaves(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    payload = session_payload((3, 2))
    for track in payload["tracks"]:
        if track["id"] == "track-1-2":
            track["participantId"] = "guest-first"
        elif track["id"] in {"track-1-3", "track-2-2"}:
            track["participantId"] = "guest-second"
    episode = ingest(config, runtime, PartsClient(payload))
    raw = [a for a in list_media_artifacts(runtime.db_file, episode.session_uid)
           if a.kind == "raw_track"]
    assert next(a for a in raw if a.source_id == "track-1-3").role == "guest2"
    assert next(a for a in raw if a.source_id == "track-2-2").role == "guest2"


def test_quiet_sources_are_preserved_without_activity_filtering(
    moto_s3, app_config_factory, runtime_factory, tmp_path, monkeypatch,
):
    monkeypatch.setattr("podline.scanner._track_has_audio", lambda *_a, **_k: (False, -100))
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    episode = ingest(config, runtime, PartsClient(session_payload((3,))))
    raw = [a for a in list_media_artifacts(runtime.db_file, episode.session_uid)
           if a.kind == "raw_track"]
    assert len(raw) == 3


def test_ingest_groups_332_with_stable_identity(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    client = PartsClient(session_payload())
    episode = ingest(config, runtime, client)
    parts = read_parts(runtime)
    assert [part["take_id"] for part in parts] == ["take-1", "take-2", "take-3"]
    assert [part["part_number"] for part in parts] == [1, 2, 3]
    assert all(part["ingest_complete"] for part in parts)
    raw = [a for a in list_media_artifacts(runtime.db_file, episode.session_uid)
           if a.kind == "raw_track"]
    assert len(raw) == 8
    for part, expected in zip(parts, (3, 3, 2)):
        owned = [a for a in raw if a.part_id == part["part_id"]]
        assert len(owned) == expected
        assert {a.role for a in owned} == (
            {"host1", "host2", "guest1"} if expected == 3 else {"host1", "host2"}
        )
    previous = set(client.downloads)
    assert len(previous) == 8
    assert ingest(config, runtime, client).session_uid == episode.session_uid
    assert len(client.downloads) == 8


def test_ingest_resumes_after_one_part_without_redownloading_it(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    client = PartsClient(session_payload())
    client.fail_track = "track-2-1"
    with pytest.raises(CozytrackIngestError, match="interrupted"):
        ingest(config, runtime, client)
    parts = read_parts(runtime)
    assert parts[0]["ingest_complete"]
    assert not parts[1]["ingest_complete"]
    first_downloads = [x for x in client.downloads if x.startswith("track-1-")]
    client.fail_track = None
    ingest(config, runtime, client)
    assert [x for x in client.downloads if x.startswith("track-1-")] == first_downloads
    assert all(p["ingest_complete"] for p in read_parts(runtime))


@pytest.mark.parametrize("change", [
    "active", "uploading", "missing-take", "mixed", "missing-identity", "duplicate-identity",
])
def test_ingest_rejects_unready_or_ambiguous_sources_before_download(
    moto_s3, app_config_factory, runtime_factory, tmp_path, change,
):
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    payload = session_payload()
    if change == "active":
        payload["recordingTakes"][0]["status"] = "recording"
    elif change == "uploading":
        payload["tracks"][0]["status"] = "uploading"
    elif change == "missing-take":
        payload["recordingTakes"] = []
    elif change == "mixed":
        payload["tracks"][0]["takeId"] = None
    elif change == "missing-identity":
        payload["tracks"][0]["participantId"] = None
    else:
        payload["tracks"][1]["participantId"] = payload["tracks"][0]["participantId"]
    client = PartsClient(payload)
    with pytest.raises(CozytrackIngestError):
        ingest(config, runtime, client)
    assert client.downloads == []


@pytest.mark.parametrize("selector", ["track-2-3", "part-02:guest1"])
def test_explicit_exclusion_preserves_siblings_and_repeated_selection(
    moto_s3, app_config_factory, runtime_factory, tmp_path, selector,
):
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    client = PartsClient(session_payload())
    episode = ingest(config, runtime, client, excluded_track_selectors=[selector])
    raw = [a for a in list_media_artifacts(runtime.db_file, episode.session_uid)
           if a.kind == "raw_track"]
    assert len(raw) == 7
    assert "track-2-3" not in {a.source_id for a in raw}
    assert "track-1-3" in {a.source_id for a in raw}
    ingest(config, runtime, client)
    assert len(client.downloads) == 7


def test_ambiguous_role_exclusion_requires_a_part_or_exact_id(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    client = PartsClient(session_payload())
    with pytest.raises(CozytrackIngestError, match="ambiguous"):
        ingest(config, runtime, client, excluded_track_selectors=["guest1"])
    assert client.downloads == []


def test_known_active_take_blocks_legacy_unassociated_tracks(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    payload = session_payload((2,))
    for track in payload["tracks"]:
        track["takeId"] = None
    payload["recordingTakes"][0]["status"] = "recording"
    client = PartsClient(payload)
    with pytest.raises(CozytrackIngestError, match="not stopped"):
        ingest(config, runtime, client)
    assert client.downloads == []


def test_modern_single_take_deduplicates_a_verified_local_import(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    from podline.ingest import import_local_folder
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    folder = tmp_path / "original-local-copy"
    folder.mkdir()
    for number in (1, 2):
        (folder / f"Tr{number}.webm").write_bytes(f"source:track-1-{number}".encode())
    original, created = import_local_folder(
        config=config, runtime_paths=runtime, db_file=runtime.db_file,
        folder=folder, duration_probe=lambda _: 60.0,
    )
    assert created
    associated = ingest(config, runtime, PartsClient(session_payload((2,))))
    assert associated.session_uid == original.session_uid
    assert read_parts(runtime)[0]["take_id"] == "take-1"


def test_reconnect_is_one_flagged_part_not_another_part(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    import json
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    payload = session_payload((2,))
    payload["tracks"][0]["segmentCount"] = 2
    client = PartsClient(payload)
    ingest(config, runtime, client)
    parts = read_parts(runtime)
    assert len(parts) == 1
    assert "reconnect" in json.dumps(parts[0]["timing_warnings"]).lower()


def test_ingest_appends_take_without_changing_existing_part(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    client = PartsClient(session_payload((3, 3)))
    episode = ingest(config, runtime, client)
    before = [(p["part_id"], p["part_number"]) for p in read_parts(runtime)]
    client.payload = session_payload()
    assert ingest(config, runtime, client).session_uid == episode.session_uid
    assert [(p["part_id"], p["part_number"]) for p in read_parts(runtime)][:2] == before
    assert len(read_parts(runtime)) == 3
    assert len(client.downloads) == 8


def test_ingest_detects_prepared_source_change(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    client = PartsClient(session_payload())
    ingest(config, runtime, client)
    with connect(runtime.db_file) as connection:
        connection.execute(
            "UPDATE recording_parts SET status = 'prepared', "
            "auphonic_production_id = 'existing-production' WHERE part_number = 1"
        )
        connection.commit()
    client.payload["tracks"][0]["segmentCount"] = 2
    with pytest.raises(CozytrackIngestError, match="changed"):
        ingest(config, runtime, client)
    assert len(client.downloads) == 8


def test_source_becoming_active_during_ingest_does_not_leave_ready_parts(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    from podline.repository import list_recording_parts

    class ChangingClient(PartsClient):
        reads = 0

        def get_session(self, session_id):
            self.reads += 1
            if self.reads == 2:
                self.payload["recordingTakes"][0]["status"] = "recording"
            return super().get_session(session_id)

    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    client = ChangingClient(session_payload())
    with pytest.raises(CozytrackIngestError, match="not stopped|changed"):
        ingest(config, runtime, client)
    assert not all(p["ingest_complete"] for p in read_parts(runtime))


def test_registered_final_master_blocks_new_take_even_if_status_update_was_interrupted(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    from podline.artifacts import store_artifact_file
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    client = PartsClient(session_payload((3, 3)))
    episode = ingest(config, runtime, client)
    master = tmp_path / "assembled.wav"
    master.write_bytes(b"assembled final master")
    store_artifact_file(
        config=config, runtime_paths=runtime, db_file=runtime.db_file,
        session_uid=episode.session_uid, kind="final_master", source_path=master,
        content_type="audio/wav",
    )
    client.payload = session_payload()
    with pytest.raises(CozytrackIngestError, match="Finalized"):
        ingest(config, runtime, client)
    assert len(read_parts(runtime)) == 2


def test_modern_reingest_heals_interrupted_legacy_source_association(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    client = PartsClient(session_payload((2,)))
    episode = ingest(config, runtime, client)
    with connect(runtime.db_file) as connection:
        connection.execute("UPDATE media_artifacts SET source_id=NULL WHERE kind='raw_track'")
        connection.execute(
            "UPDATE recording_parts SET take_id=NULL, source_snapshot='{}'"
        )
        connection.commit()
    assert ingest(config, runtime, client).session_uid == episode.session_uid
    raw = [a for a in list_media_artifacts(runtime.db_file, episode.session_uid)
           if a.kind == "raw_track"]
    assert {a.source_id for a in raw} == {"track-1-1", "track-1-2"}


def test_local_import_cannot_deduplicate_onto_only_one_part(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    from podline.ingest import IngestError, import_local_folder
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    client = PartsClient(session_payload((2,)))
    episode = ingest(config, runtime, client)
    client.payload = session_payload((2, 2))
    ingest(config, runtime, client)
    folder = tmp_path / "local-copy-of-part-1"
    folder.mkdir()
    for number in (1, 2):
        (folder / f"Tr{number}.webm").write_bytes(f"source:track-1-{number}".encode())
    with pytest.raises(IngestError, match="multipart|part"):
        import_local_folder(
            config=config, runtime_paths=runtime, db_file=runtime.db_file,
            folder=folder, duration_probe=lambda _: 60.0,
        )


def test_multipart_plan_is_atomic_before_any_media_transfer(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    with connect(runtime.db_file) as connection:
        connection.execute(
            "CREATE TRIGGER fail_part_plan BEFORE INSERT ON recording_parts "
            "WHEN NEW.part_number=2 BEGIN SELECT RAISE(ABORT, 'part planning interrupted'); END"
        )
        connection.commit()
    client = PartsClient(session_payload())
    with pytest.raises(CozytrackIngestError, match="planning interrupted"):
        ingest(config, runtime, client)
    with connect(runtime.db_file) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM recording_parts").fetchone()[0] == 0
    assert client.downloads == []


def test_single_take_timing_evidence_is_atomic_with_source_registration(
    moto_s3, app_config_factory, runtime_factory, tmp_path, monkeypatch,
):
    from podline.cozytrack import parts
    from podline.repository import list_recording_parts
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    payload = session_payload((2,))
    payload["tracks"][0]["partial"] = True
    original = parts.register_candidate_with_storage

    def interrupt_after_registration(**kwargs):
        original(**kwargs)
        raise RuntimeError("interrupted after registration")

    monkeypatch.setattr(parts, "register_candidate_with_storage", interrupt_after_registration)
    with pytest.raises(CozytrackIngestError, match="after registration"):
        ingest(config, runtime, PartsClient(payload))
    rows = read_parts(runtime)
    assert len(rows) == 1
    part = list_recording_parts(runtime.db_file, rows[0]["session_uid"])[0]
    assert part.source_snapshot["tracks"][0]["partial"] is True
    assert part.timing_warnings
