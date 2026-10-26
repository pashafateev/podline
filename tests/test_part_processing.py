from pathlib import Path

import pytest

from podline.auphonic import (
    AuphonicError, AuphonicOutputFile, AuphonicPreparedProduction, AuphonicProduction,
)
from podline.repository import get_session, list_media_artifacts
from test_recording_parts import PartsClient, ingest, session_payload


class MultiProductionClient:
    def __init__(self):
        self.productions = {}
        self.created = []
        self.started = []
        self.offsets = []
        self.single_created = []
        self.fail_wait = None
        self.fail_attach = None
        self.lose_create = False
        self.lose_start = False

    def create_production(self, *, title, track_count, preset_id):
        production_id = f"prod-{len(self.created) + 1}"
        self.created.append((production_id, track_count, preset_id))
        self.productions[production_id] = dict(
            uuid=production_id, status=10, metadata={"title": title}, preset=preset_id,
            multi_input_files=[dict(id=f"slot-{n}", type="multitrack")
                               for n in range(1, track_count + 1)],
        )
        if self.lose_create:
            raise RuntimeError("lost create response")
        return AuphonicPreparedProduction(
            production_id, [f"slot-{n}" for n in range(1, track_count + 1)],
            f"https://test/status/{production_id}", f"https://test/edit/{production_id}",
        )

    def attach_service_input(self, production_id, track_id, service_id, input_key):
        if self.fail_attach == production_id:
            raise RuntimeError("attachment interrupted")
        track = next(t for t in self.productions[production_id]["multi_input_files"]
                     if t["id"] == track_id)
        track["input_file"] = input_key

    def create_single_input_production(self, **kwargs):
        self.single_created.append(kwargs)
        production_id = "prod-final-refresh"
        self.productions[production_id] = dict(
            uuid=production_id, status=10, multi_input_files=[],
        )
        return production_id

    def get_production(self, production_id):
        production = self.productions[production_id]
        return {"data": {
            **production,
            "status_string": "Done" if production["status"] == 3 else "Pending",
            "start_allowed": production["status"] == 10,
            "change_allowed": production["status"] == 10,
            "edit_page": f"https://test/edit/{production_id}",
            "output_files": [
                dict(format="mp3", ending="mp3",
                     download_url=f"https://test/{production_id}/mix.mp3"),
                dict(format="subtitle", ending="vtt",
                     download_url=f"https://test/{production_id}/transcript.vtt"),
            ],
        }}

    def start_production(self, production_id):
        assert self.productions[production_id]["status"] == 10
        self.started.append(production_id)
        self.productions[production_id]["status"] = 1
        if self.lose_start:
            raise RuntimeError("lost start response")

    def wait_for_production(self, production_id):
        if self.fail_wait == production_id:
            raise RuntimeError("poll interrupted")
        self.productions[production_id]["status"] = 3
        return AuphonicProduction(production_id, [
            AuphonicOutputFile("mp3", f"https://test/{production_id}/mix.mp3", "mp3"),
            AuphonicOutputFile("vtt", f"https://test/{production_id}/transcript.vtt", "vtt"),
        ])

    def download_file(self, url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        if "/prod-final-refresh/" in url:
            destination.write_text(
                "WEBVTT\n\n00:00.000 --> 00:01.000\nFinal cut transcript\n",
                encoding="utf-8",
            )
        else:
            destination.write_bytes(f"output:{url}".encode())

    def update_multi_input_file_offset(self, production_id, track_id, offset):
        self.offsets.append((production_id, track_id, offset))
        track = next(t for t in self.productions[production_id]["multi_input_files"]
                     if t["id"] == track_id)
        track["offset"] = offset


@pytest.fixture
def multipart(moto_s3, app_config_factory, runtime_factory, tmp_path):
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    episode = ingest(config, runtime, PartsClient(session_payload()))
    return config, runtime, episode, MultiProductionClient()


def prepare(multipart):
    from podline.part_processing import prepare_parts
    config, runtime, episode, client = multipart
    return prepare_parts(config=config, db_file=runtime.db_file, session=episode, client=client)


def preview(multipart):
    from podline.part_processing import preview_parts
    config, runtime, episode, client = multipart
    return preview_parts(config=config, db_file=runtime.db_file,
                         session=get_session(runtime.db_file, episode.session_uid),
                         client=client)


def start(multipart, **kwargs):
    from podline.part_processing import start_parts
    config, runtime, episode, client = multipart
    return start_parts(config=config, runtime_paths=runtime, db_file=runtime.db_file,
                       session=get_session(runtime.db_file, episode.session_uid),
                       client=client, **kwargs)


def test_prepare_uses_332_presets_and_reuses_completed_preparation(multipart):
    config, runtime, episode, client = multipart
    prepare(multipart)
    assert [row[2] for row in client.created] == ["preset-3", "preset-3", "preset-2"]
    raw = [a for a in list_media_artifacts(runtime.db_file, episode.session_uid)
           if a.kind == "raw_track"]
    for production, count, _preset in client.created:
        keys = [track["input_file"] for track in client.productions[production]["multi_input_files"]]
        assert len(keys) == count
        owners = {a.part_id for a in raw if any(a.s3_uri.endswith(key) for key in keys)}
        assert len(owners) == 1
    prepare(multipart)
    assert len(client.created) == 3
    assert client.started == []


def test_start_preflight_requires_every_selected_preview(multipart):
    from podline.part_processing import preview_parts
    config, runtime, episode, client = multipart
    prepare(multipart)
    preview_parts(config=config, db_file=runtime.db_file, session=episode,
                  client=client, part_number=1)
    with pytest.raises(AuphonicError, match="preview"):
        start(multipart)
    assert client.started == []


def test_middle_part_resume_retains_independent_outputs(multipart):
    config, runtime, episode, client = multipart
    prepare(multipart)
    preview(multipart)
    client.fail_wait = "prod-2"
    with pytest.raises(AuphonicError, match="poll interrupted"):
        start(multipart)
    assert client.started == ["prod-1", "prod-2"]
    first = [a for a in list_media_artifacts(runtime.db_file, episode.session_uid)
             if a.kind == "pre_edit_master"]
    assert len(first) == 1
    assert get_session(runtime.db_file, episode.session_uid).status != "waiting_for_finalize"
    client.fail_wait = None
    assert start(multipart).status == "waiting_for_finalize"
    assert client.started == ["prod-1", "prod-2", "prod-3"]
    assert len(client.created) == 3
    outputs = [a for a in list_media_artifacts(runtime.db_file, episode.session_uid)
               if a.kind == "pre_edit_master"]
    assert {a.filename for a in outputs} == {"part-01.mp3", "part-02.mp3", "part-03.mp3"}
    assert len({a.part_id for a in outputs}) == 3
    assert first[0].sha256 == next(a.sha256 for a in outputs if a.part_id == first[0].part_id)
    assert start(multipart).status == "waiting_for_finalize"
    assert len(client.started) == 3


def test_attachment_failure_reuses_created_production(multipart):
    _, _, _, client = multipart
    client.fail_attach = "prod-1"
    with pytest.raises(AuphonicError, match="attachment"):
        prepare(multipart)
    client.fail_attach = None
    prepare(multipart)
    assert len(client.created) == 3


def test_dynamic_preset_slots_resume_after_partial_attachment(multipart, monkeypatch):
    _, _, _, client = multipart
    original_create = client.create_production
    attached = []
    fail_once = True

    def without_declared_slots(**kwargs):
        production = original_create(**kwargs)
        client.productions[production.production_id]["multi_input_files"] = []
        return AuphonicPreparedProduction(
            production.production_id, [], production.status_page, production.edit_page,
        )

    def attach(production_id, track_id, service_id, input_key):
        nonlocal fail_once
        if track_id == "host2" and fail_once:
            fail_once = False
            raise RuntimeError("dynamic attachment interrupted")
        attached.append((production_id, track_id))
        client.productions[production_id]["multi_input_files"].append(
            dict(id=track_id, type="multitrack", input_file=input_key)
        )

    monkeypatch.setattr(client, "create_production", without_declared_slots)
    monkeypatch.setattr(client, "attach_service_input", attach)
    with pytest.raises(AuphonicError, match="attachment interrupted"):
        prepare(multipart)
    prepare(multipart)
    assert len(client.created) == 3
    assert attached.count(("prod-1", "host1")) == 1


def test_stale_concurrent_prepare_cannot_create_second_production(multipart, monkeypatch):
    from podline import part_processing as processing
    config, runtime, episode, client = multipart
    claim = processing.claim_recording_part_operation
    competing_call_completed = False

    def competing_claim(db_file, part_id, operation):
        nonlocal competing_call_completed
        if not competing_call_completed:
            competing_call_completed = True
            processing.prepare_parts(
                config=config, db_file=db_file, session=episode, client=client,
                part_number=1,
            )
        return claim(db_file, part_id, operation)

    monkeypatch.setattr(processing, "claim_recording_part_operation", competing_claim)
    processing.prepare_parts(config=config, db_file=runtime.db_file, session=episode,
                             client=client, part_number=1)
    assert len(client.created) == 1


def test_foreign_input_is_rejected_before_paid_start(multipart):
    _, _, _, client = multipart
    prepare(multipart)
    preview(multipart)
    client.productions["prod-1"]["multi_input_files"][0]["input_file"] = "another-episode/input.webm"
    with pytest.raises(AuphonicError, match="foreign|changed"):
        start(multipart)
    assert client.started == []


def test_incomplete_source_role_reconciliation_blocks_preparation(multipart):
    from podline.db import connect
    from podline.repository import list_recording_parts
    _, runtime, episode, client = multipart
    part = list_recording_parts(runtime.db_file, episode.session_uid)[0]
    with connect(runtime.db_file) as connection:
        for old, new in (("host1", "temporary"), ("host2", "host1"), ("temporary", "host2")):
            connection.execute(
                "UPDATE media_artifacts SET role=? WHERE part_id=? AND role=?",
                (new, part.part_id, old),
            )
        connection.commit()
    with pytest.raises(AuphonicError, match="reconcil|identity"):
        prepare(multipart)
    assert client.created == []


def test_missing_expected_input_is_rejected_before_paid_start(multipart):
    _, _, _, client = multipart
    prepare(multipart)
    preview(multipart)
    client.productions["prod-1"]["multi_input_files"][0].pop("input_file")
    with pytest.raises(AuphonicError, match="missing|attach"):
        start(multipart)
    assert client.started == []


def test_completed_sync_rejects_foreign_inputs(multipart):
    from podline.part_processing import sync_parts
    config, runtime, episode, client = multipart
    prepare(multipart)
    client.productions["prod-1"]["status"] = 3
    client.productions["prod-1"]["multi_input_files"][0]["input_file"] = "foreign/audio.webm"
    with pytest.raises(AuphonicError, match="foreign|changed"):
        sync_parts(config=config, runtime_paths=runtime, db_file=runtime.db_file,
                   session=episode, part_number=1, client=client)
    assert not [a for a in list_media_artifacts(runtime.db_file, episode.session_uid)
                if a.kind == "pre_edit_master"]


def test_interrupted_output_retry_does_not_modify_old_checksum_cache(
    multipart, monkeypatch,
):
    from podline import auphonic
    from podline.artifacts import cache_path_for
    from podline.part_processing import sync_parts
    from podline.storage import sha256_of_file
    config, runtime, episode, client = multipart
    prepare(multipart)
    client.productions["prod-1"]["status"] = 3
    store = auphonic.store_artifact_file

    def fail_transcript(**kwargs):
        if kwargs["kind"] == "transcript":
            raise RuntimeError("transcript persistence interrupted")
        return store(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(auphonic, "store_artifact_file", fail_transcript)
        with pytest.raises(AuphonicError, match="transcript persistence"):
            sync_parts(config=config, runtime_paths=runtime, db_file=runtime.db_file,
                       session=episode, part_number=1, client=client)
    old_master = next(a for a in list_media_artifacts(runtime.db_file, episode.session_uid)
                      if a.kind == "pre_edit_master")
    old_cache = cache_path_for(runtime, old_master.sha256, old_master.filename)
    assert sha256_of_file(old_cache) == old_master.sha256

    def changed_download(url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f"reprocessed:{url}".encode())

    monkeypatch.setattr(client, "download_file", changed_download)
    sync_parts(config=config, runtime_paths=runtime, db_file=runtime.db_file,
               session=episode, part_number=1, client=client)
    assert sha256_of_file(old_cache) == old_master.sha256


def test_unknown_creation_blocks_blind_retry_and_can_be_adopted(multipart):
    from podline.part_processing import sync_parts
    config, runtime, episode, client = multipart
    client.lose_create = True
    with pytest.raises(AuphonicError, match="create"):
        prepare(multipart)
    client.lose_create = False
    with pytest.raises(AuphonicError, match="reconcil"):
        prepare(multipart)
    assert len(client.created) == 1
    sync_parts(config=config, runtime_paths=runtime, db_file=runtime.db_file,
               session=episode, production_id="prod-1", part_number=1, client=client)
    prepare(multipart)
    assert len(client.created) == 3
    assert all(track.get("input_file") for production in client.productions.values()
               for track in production["multi_input_files"])


def test_unknown_creation_adoption_uses_preset_definition_when_uuid_is_not_returned(
    multipart, monkeypatch,
):
    from podline.part_processing import sync_parts
    config, runtime, episode, client = multipart
    client.lose_create = True
    with pytest.raises(AuphonicError, match="create"):
        prepare(multipart)
    original_get = client.get_production

    def without_template_uuid(production_id):
        result = original_get(production_id)
        result["data"].pop("preset", None)
        return result

    monkeypatch.setattr(client, "get_production", without_template_uuid)
    monkeypatch.setattr(client, "get_preset", lambda preset_id: {"data": {
        "multi_input_files": [dict(id=f"slot-{n}", type="multitrack") for n in (1, 2, 3)],
    }}, raising=False)
    client.lose_create = False
    sync_parts(config=config, runtime_paths=runtime, db_file=runtime.db_file,
               session=episode, production_id="prod-1", part_number=1, client=client)
    prepare(multipart)
    assert len(client.created) == 3


def test_rejected_create_can_resume_after_configuration_is_fixed(multipart, monkeypatch):
    _, _, _, client = multipart
    original = client.create_production
    rejected = False

    def reject_once(**kwargs):
        nonlocal rejected
        if not rejected:
            rejected = True
            error = AuphonicError("credentials rejected")
            error.rejected = True
            raise error
        return original(**kwargs)

    monkeypatch.setattr(client, "create_production", reject_once)
    with pytest.raises(AuphonicError, match="rejected"):
        prepare(multipart)
    prepare(multipart)
    assert len(client.created) == 3


def test_known_created_id_is_checkpointed_even_when_metadata_validation_fails(
    multipart, monkeypatch,
):
    from podline.repository import list_recording_parts
    _, runtime, episode, client = multipart
    original = client.create_production
    failed = False

    def invalid_metadata_once(**kwargs):
        nonlocal failed
        result = original(**kwargs)
        if not failed:
            failed = True
            error = AuphonicError("invalid preset metadata")
            error.production_id = result.production_id
            raise error
        return result

    monkeypatch.setattr(client, "create_production", invalid_metadata_once)
    with pytest.raises(AuphonicError, match="metadata"):
        prepare(multipart)
    assert list_recording_parts(runtime.db_file, episode.session_uid)[0].auphonic_production_id == "prod-1"
    prepare(multipart)
    assert len(client.created) == 3


def test_lost_start_response_inspects_without_restarting(multipart):
    _, _, _, client = multipart
    prepare(multipart)
    preview(multipart)
    client.lose_start = True
    with pytest.raises(AuphonicError, match="start"):
        start(multipart)
    client.lose_start = False
    start(multipart)
    assert client.started == ["prod-1", "prod-2", "prod-3"]


def test_single_part_reset_clears_legacy_production_projection(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    from podline.part_processing import prepare_parts, reset_parts
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    episode = ingest(config, runtime, PartsClient(session_payload((2,))))
    client = MultiProductionClient()
    prepared = prepare_parts(
        config=config, db_file=runtime.db_file, session=episode, client=client,
    )
    assert prepared.auphonic_production_id == "prod-1"
    reset = reset_parts(db_file=runtime.db_file, session=prepared)
    assert reset.auphonic_production_id is None


@pytest.mark.parametrize("risk", ["partial", "reconnect", "incomplete", "early-ending"])
def test_timing_risks_block_two_track_automatic_offsets(
    moto_s3, app_config_factory, runtime_factory, tmp_path, risk,
):
    from podline.part_processing import offset_parts, prepare_parts, preview_parts
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    payload = session_payload((2,))
    if risk == "partial":
        payload["tracks"][0]["partial"] = True
    elif risk == "reconnect":
        payload["tracks"][0]["segmentCount"] = 2
    elif risk == "incomplete":
        payload["tracks"][0]["incompleteSegmentCount"] = 1
    source = PartsClient(payload)
    episode = ingest(config, runtime, source)
    if risk == "early-ending":
        from podline.db import connect
        with connect(runtime.db_file) as connection:
            connection.execute(
                "UPDATE media_artifacts SET duration_seconds=20 WHERE role='host2'"
            )
            connection.commit()
    client = MultiProductionClient()
    prepare_parts(config=config, db_file=runtime.db_file, session=episode, client=client)
    with pytest.raises(AuphonicError, match="Part 1"):
        offset_parts(config=config, runtime_paths=runtime, db_file=runtime.db_file,
                     session=episode, apply=True, client=client)
    assert client.offsets == []
    # A warning blocks the unsafe calculation, not the existing review gate.
    assert preview_parts(config=config, db_file=runtime.db_file,
                         session=episode, client=client)
    from podline.part_processing import start_parts
    assert start_parts(
        config=config, runtime_paths=runtime, db_file=runtime.db_file,
        session=episode, client=client,
    ).status == "waiting_for_finalize"
    assert client.offsets == []


def test_unsupported_count_preserves_sources_and_creates_no_production(
    moto_s3, app_config_factory, runtime_factory, tmp_path,
):
    from podline.part_processing import prepare_parts
    config = app_config_factory(tmp_path)
    runtime = runtime_factory(config)
    payload = session_payload((3,))
    for number in (4, 5):
        payload["tracks"].append({
            **payload["tracks"][2], "id": f"track-1-{number}",
            "participantId": f"remote-{number}",
        })
    episode = ingest(config, runtime, PartsClient(payload))
    client = MultiProductionClient()
    with pytest.raises(AuphonicError, match="unsupported participant count 5"):
        prepare_parts(config=config, db_file=runtime.db_file,
                      session=episode, client=client)
    assert client.created == []
    assert len([a for a in list_media_artifacts(runtime.db_file, episode.session_uid)
                if a.kind == "raw_track"]) == 5


def test_bulk_offset_preflight_does_not_modify_healthy_siblings(multipart):
    from podline.part_processing import offset_parts
    from podline.repository import list_recording_parts, update_recording_part
    config, runtime, episode, client = multipart
    prepare(multipart)
    part = list_recording_parts(runtime.db_file, episode.session_uid)[1]
    update_recording_part(runtime.db_file, part.part_id,
                          timing_warnings=[{"track_id": "track-2-1", "reason": "partial"}])
    with pytest.raises(AuphonicError, match="Part 2"):
        offset_parts(config=config, runtime_paths=runtime, db_file=runtime.db_file,
                     session=episode, apply=True, client=client)
    assert client.offsets == []
    assert offset_parts(config=config, runtime_paths=runtime, db_file=runtime.db_file,
                        session=episode, apply=True, client=client, part_number=3)


def test_healthy_late_start_has_positive_part_scoped_offset(multipart):
    from podline.db import connect
    from podline.part_processing import offset_parts
    from podline.repository import list_recording_parts, update_recording_part
    config, runtime, episode, client = multipart
    prepare(multipart)
    part = list_recording_parts(runtime.db_file, episode.session_uid)[2]
    snapshot = part.source_snapshot
    next(track for track in snapshot["tracks"] if track["role"] == "host2")[
        "created_at"] = "2026-10-01T12:03:02Z"
    update_recording_part(runtime.db_file, part.part_id, source_snapshot=snapshot)
    with connect(runtime.db_file) as connection:
        connection.execute(
            "UPDATE media_artifacts SET duration_seconds=58 "
            "WHERE part_id=? AND role='host2' AND kind='raw_track'", (part.part_id,),
        )
        connection.commit()
    offset_parts(config=config, runtime_paths=runtime, db_file=runtime.db_file,
                 session=episode, apply=True, client=client, part_number=3)
    assert client.offsets == [("prod-3", "slot-2", 2.0)]


def test_contradictory_timing_warning_survives_unchanged_reingest(multipart):
    from podline.db import connect
    from podline.part_processing import offset_parts
    from podline.repository import list_recording_parts
    config, runtime, episode, client = multipart
    prepare(multipart)
    part = list_recording_parts(runtime.db_file, episode.session_uid)[2]
    with connect(runtime.db_file) as connection:
        connection.execute(
            "UPDATE media_artifacts SET duration_seconds=20 "
            "WHERE part_id=? AND role='host2'", (part.part_id,),
        )
        connection.commit()
    with pytest.raises(AuphonicError, match="Part 3"):
        offset_parts(config=config, runtime_paths=runtime, db_file=runtime.db_file,
                     session=episode, apply=True, client=client, part_number=3)
    before = list_recording_parts(runtime.db_file, episode.session_uid)[2].timing_warnings
    assert before
    ingest(config, runtime, PartsClient(session_payload()))
    after = list_recording_parts(runtime.db_file, episode.session_uid)[2].timing_warnings
    assert all(warning in after for warning in before)
