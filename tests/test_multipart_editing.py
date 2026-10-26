"""Workspace and final-master boundary tests; all media and services are local fakes."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from podline.artifacts import (
    ArtifactError,
    create_workspace,
    seed_cache,
    workspace_path_for,
)
from podline.episode_meta import EpisodeMetaError, generate_episode_metadata
from podline.finalize import FinalizeError, finalize_session
from podline.podbean import PodbeanError, create_podbean_draft
from podline.repository import get_media_artifact, get_session, list_media_artifacts
from podline.storage import sha256_of_file


@pytest.fixture
def editing_episode(
    tmp_path, app_config_factory, runtime_factory, session_factory
):
    # Import at fixture execution so the red run identifies the missing DB
    # contract without preventing collection of the original regression suite.
    from podline.repository import create_recording_part, register_media_artifact

    config = app_config_factory(tmp_path, podbean_enabled=True)
    runtime = runtime_factory(config)
    session = session_factory(
        runtime.db_file,
        session_uid="multipart-edit",
        status="waiting_for_finalize",
    )

    def part(number, *, complete=True, output_basename=None):
        return create_recording_part(
            runtime.db_file,
            session_uid=session.session_uid,
            part_number=number,
            take_id=f"take-{number}",
            source_snapshot={"tracks": [], "excluded_track_ids": []},
            status="complete" if complete else "started",
            ingest_complete=True,
            output_basename=output_basename or f"part-{number:02d}",
        )

    def artifact(
        owner,
        kind,
        filename,
        content,
        *,
        role="",
        duration=None,
        production=None,
        source_id=None,
        provider="auphonic",
    ):
        scope = owner.part_id if owner else "episode"
        source = tmp_path / "registered" / scope / filename
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        digest = sha256_of_file(source)
        record = register_media_artifact(
            runtime.db_file,
            session_uid=session.session_uid,
            part_id=owner.part_id if owner else None,
            kind=kind,
            role=role,
            filename=filename,
            content_type="application/octet-stream",
            byte_size=len(content),
            sha256=digest,
            duration_seconds=duration,
            s3_uri=f"s3://test-podline-media/{scope}/{digest}/{filename}",
            source_provider=provider,
            source_id=source_id or production or (
                f"prod-{owner.part_number}" if owner else None
            ),
            timestamp="2026-07-20T00:00:00+00:00",
        )
        seed_cache(runtime, source_path=source, sha256=digest)
        return record

    def outputs(owner):
        from podline.repository import update_recording_part

        update_recording_part(
            runtime.db_file,
            owner.part_id,
            auphonic_production_id=f"prod-{owner.part_number}",
        )
        artifact(
            owner, "pre_edit_master", f"{owner.output_basename}.mp3",
            f"mix-{owner.part_number}".encode(),
        )
        artifact(
            owner, "transcript", f"{owner.output_basename}.vtt",
            f"WEBVTT\n\nPart {owner.part_number} only\n".encode(),
        )

    def workspace(**kwargs):
        return create_workspace(
            config=config,
            runtime_paths=runtime,
            db_file=runtime.db_file,
            session_uid=session.session_uid,
            **kwargs,
        )

    return SimpleNamespace(
        config=config, runtime=runtime, session=session,
        part=part, artifact=artifact, outputs=outputs, workspace=workspace,
    )


def _manifest(workspace):
    return json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))


def test_workspace_lists_ordered_parts_and_exposes_completed_siblings(editing_episode):
    e = editing_episode
    one, two, three = e.part(1), e.part(2, complete=False), e.part(3)
    e.outputs(one)
    e.outputs(three)

    workspace = e.workspace()

    assert (workspace / "part-01.mp3").read_bytes() == b"mix-1"
    assert (workspace / "part-03.vtt").read_bytes() == b"WEBVTT\n\nPart 3 only\n"
    assert not (workspace / "part-02.mp3").exists()
    manifest = _manifest(workspace)
    assert [item["part_id"] for item in manifest["parts"]] == [
        one.part_id, two.part_id, three.part_id,
    ]
    assert manifest["ready_for_finalize"] is False
    assert manifest["parts"][1]["pending"] == ["pre_edit_master", "transcript"]
    assert manifest["parts"][0]["take_id"] == "take-1"
    assert manifest["parts"][0]["auphonic_production_id"] == "prod-1"
    assert manifest["parts"][0]["artifacts"]["pre_edit_master"]["sha256"] == (
        sha256_of_file(workspace / "part-01.mp3")
    )
    assert "transcript" not in manifest.get("artifacts", {})
    assert not list_media_artifacts(
        e.runtime.db_file, e.session.session_uid, kind="transcript", part_id=None,
    )


def test_workspace_readiness_does_not_accept_an_obsolete_part_pair(editing_episode):
    e = editing_episode
    one, two = e.part(1), e.part(2)
    e.outputs(one)
    e.outputs(two)
    e.artifact(
        two, "transcript", "part-02.vtt", b"previous-production",
        production="obsolete-production",
    )

    workspace = e.workspace()
    manifest = _manifest(workspace)

    assert manifest["ready_for_finalize"] is False
    assert "current_transcript" in manifest["parts"][1]["pending"]
    assert (workspace / "part-01.mp3").read_bytes() == b"mix-1"


def test_sources_only_before_processing_exports_exact_bytes_for_selected_part(
    editing_episode, monkeypatch,
):
    e = editing_episode
    one, two = e.part(1, complete=False), e.part(2, complete=False)
    e.artifact(one, "raw_track", "original.webm", b"one", role="host1", source_id="s1")
    raw = b"\x1aE\xdf\xa3unaltered-opus\x00\xff"
    record = e.artifact(
        two, "raw_track", "original.webm", raw, role="host1",
        source_id="source-2", provider="cozytrack",
    )
    monkeypatch.setattr(
        "podline.artifacts._materialize_aligned_stem",
        lambda *_a, **_kw: pytest.fail("raw source export must not align media"),
    )

    workspace = e.workspace(include_sources=True, part_number=2)
    target = workspace / "sources" / "part-02" / "host1-source-2.webm"

    assert target.read_bytes() == raw
    assert sha256_of_file(target) == record.sha256
    assert not (workspace / "sources" / "part-01").exists()
    assert not list(workspace.glob("*.mp3"))
    manifest = _manifest(workspace)
    assert len(manifest["parts"]) == 2
    assert manifest["parts"][1]["sources"][0]["source_id"] == "source-2"
    assert manifest["parts"][1]["sources"][0]["filename"] == (
        "sources/part-02/host1-source-2.webm"
    )
    assert get_session(e.runtime.db_file, e.session.session_uid) == e.session


def test_part_refresh_preserves_sibling_manifest_evidence_and_user_files(editing_episode):
    e = editing_episode
    one, two = e.part(1), e.part(2)
    e.outputs(one)
    e.outputs(two)
    e.artifact(one, "raw_track", "raw.webm", b"raw-1", role="host1", source_id="r1")
    e.artifact(two, "raw_track", "raw.webm", b"raw-2", role="host1", source_id="r2")
    workspace = e.workspace(include_sources=True)
    before = _manifest(workspace)["parts"][0]
    for name in ("edit.cpr", "assembled.wav", "notes.txt"):
        (workspace / name).write_bytes(b"user-owned")
    source_project = workspace / "sources" / "part-02" / "repair.cpr"
    source_project.write_bytes(b"source-project")
    # Part 1's DB output changed too, but the explicit selector must not
    # refresh its file or claim that its new hash is already materialized.
    e.artifact(one, "pre_edit_master", "part-01.mp3", b"not-selected")
    e.artifact(two, "pre_edit_master", "part-02.mp3", b"selected")

    e.workspace(include_sources=True, part_number=2)

    assert (workspace / "part-01.mp3").read_bytes() == b"mix-1"
    assert (workspace / "part-02.mp3").read_bytes() == b"selected"
    assert _manifest(workspace)["parts"][0] == before
    assert source_project.read_bytes() == b"source-project"
    for name in ("edit.cpr", "assembled.wav", "notes.txt"):
        assert (workspace / name).read_bytes() == b"user-owned"


def _stems(e, owner, *, durations=(10.0, 10.0)):
    for role, filename, duration in zip(
        ("host1", "guest2"), ("Jonathan.flac", "Guest.flac"), durations,
    ):
        e.artifact(
            owner, "pre_edit_stem", filename,
            f"{owner.part_number}:{role}".encode(),
            role=role, duration=duration,
        )
    return {
        "host1": {"track_id": "Jonathan", "input_filename": "host.webm", "offset_seconds": 0},
        "guest2": {"track_id": "Guest", "input_filename": "guest.webm", "offset_seconds": 1.8},
    }


def test_equal_role_stems_are_isolated_per_part_and_cleanup_is_scoped(editing_episode):
    from podline.repository import delete_media_artifacts

    e = editing_episode
    one, two = e.part(1), e.part(2)
    e.outputs(one)
    e.outputs(two)
    metadata = {one.part_id: _stems(e, one), two.part_id: _stems(e, two)}
    workspace = e.workspace(include_stems=True, stem_metadata=metadata)
    first = workspace / "stems" / "part-01" / "Jonathan.flac"
    second = workspace / "stems" / "part-02" / "Jonathan.flac"
    assert first.read_bytes() == b"1:host1"
    assert second.read_bytes() == b"2:host1"
    repair = json.loads((workspace / "repair-manifest-part-02.json").read_text())
    assert repair["part_id"] == two.part_id
    assert repair["auphonic_production_id"] == "prod-2"
    assert repair["stems"][1]["role"] == "host1"
    assert repair["stems"][1]["input_filename"] == "host.webm"
    first_repair = (workspace / "repair-manifest-part-01.json").read_bytes()
    user_stem = second.parent / "repaired.flac"
    user_stem.write_bytes(b"user-stem")
    delete_media_artifacts(
        e.runtime.db_file, session_uid=e.session.session_uid,
        kind="pre_edit_stem", part_id=two.part_id,
    )

    e.workspace(part_number=2)

    assert first.read_bytes() == b"1:host1"
    assert (workspace / "repair-manifest-part-01.json").read_bytes() == first_repair
    assert not second.exists()
    assert not (workspace / "repair-manifest-part-02.json").exists()
    assert user_stem.read_bytes() == b"user-stem"
    assert (workspace / "part-02.mp3").read_bytes() == b"mix-2"


@pytest.mark.parametrize("durations", [(10.0, 8.2), (10.0, 10.0), (None, None)])
def test_flagged_stems_keep_bytes_and_configured_offsets_without_timing_inference(
    editing_episode, monkeypatch, durations,
):
    from podline.repository import update_recording_part

    e = editing_episode
    one, two = e.part(1), e.part(2)
    e.outputs(one)
    e.outputs(two)
    warnings = [{"track_id": "reconnected", "reason": "multiple_segments"}]
    update_recording_part(e.runtime.db_file, two.part_id, timing_warnings=warnings)
    metadata = {one.part_id: _stems(e, one), two.part_id: _stems(e, two, durations=durations)}
    monkeypatch.setattr(
        "podline.artifacts._materialize_aligned_stem",
        lambda *_a, **_kw: pytest.fail("flagged parts must not shift/pad/trim stems"),
    )

    workspace = e.workspace(include_stems=True, stem_metadata=metadata)

    assert (workspace / "stems" / "part-02" / "Guest.flac").read_bytes() == b"2:guest2"
    repair = json.loads((workspace / "repair-manifest-part-02.json").read_text())
    assert repair["timing_warnings"] == warnings
    assert repair["offsets_rendered_by_auphonic"] is None
    assert repair["target_duration_seconds"] is None
    assert repair["final_loudness_check_required"] is True
    assert repair["stems"][0]["auphonic_offset_seconds"] == 1.8
    assert all(stem["podline_offset_applied_seconds"] == 0 for stem in repair["stems"])
    healthy = json.loads((workspace / "repair-manifest-part-01.json").read_text())
    assert healthy["offsets_rendered_by_auphonic"] is True


def test_flagged_single_part_keeps_flat_stems_and_original_sources(
    editing_episode, monkeypatch,
):
    from podline.repository import update_recording_part

    e = editing_episode
    one = e.part(1, output_basename=e.session.session_uid)
    e.outputs(one)
    e.artifact(None, "transcript", f"{e.session.session_uid}.vtt", b"WEBVTT\n")
    e.artifact(
        one, "raw_track", "original.webm", b"partial-original",
        role="guest2", source_id="partial-source",
    )
    metadata = _stems(e, one, durations=(10, 8.2))
    update_recording_part(
        e.runtime.db_file, one.part_id,
        timing_warnings=[{"track_id": "partial-source", "reason": "partial"}],
    )
    monkeypatch.setattr(
        "podline.artifacts._materialize_aligned_stem",
        lambda *_a, **_kw: pytest.fail("flagged single-part stems must not align"),
    )

    workspace = e.workspace(
        include_sources=True, include_stems=True, stem_metadata=metadata,
    )

    assert (workspace / "stems" / "Guest.flac").read_bytes() == b"1:guest2"
    assert (
        workspace / "sources" / "part-01" / "guest2-partial-source.webm"
    ).read_bytes() == b"partial-original"
    repair = json.loads((workspace / "repair-manifest.json").read_text())
    assert repair["offsets_rendered_by_auphonic"] is None
    assert repair["final_loudness_check_required"] is True


@pytest.mark.parametrize("legacy_manifest", [False, True])
def test_single_workspace_append_keeps_legacy_media_and_flat_stem_paths(
    editing_episode, legacy_manifest,
):
    e = editing_episode
    one = e.part(1, output_basename="old-session")
    e.outputs(one)
    # The single workflow retains its episode-current transcript alias.
    e.artifact(
        None, "transcript", "old-session.vtt", b"WEBVTT\n\nPart 1 only\n",
        production="prod-1",
    )
    metadata = _stems(e, one)
    workspace = e.workspace(include_stems=True, stem_metadata=metadata)
    if legacy_manifest:
        manifest = _manifest(workspace)
        manifest.pop("parts", None)
        (workspace / "manifest.json").write_text(json.dumps(manifest))
    project = workspace / "edit.cpr"
    project.write_bytes(b"references old-session.mp3 and stems/Jonathan.flac")
    original_stem = (workspace / "stems" / "Jonathan.flac").read_bytes()
    two = e.part(2)
    e.outputs(two)

    e.workspace(include_stems=True, stem_metadata={
        one.part_id: metadata, two.part_id: _stems(e, two),
    })

    assert (workspace / "old-session.mp3").read_bytes() == b"mix-1"
    assert (workspace / "old-session.vtt").exists()
    assert (workspace / "stems" / "Jonathan.flac").read_bytes() == original_stem
    assert (workspace / "repair-manifest.json").exists()
    assert not (workspace / "part-01.mp3").exists()
    assert not (workspace / "stems" / "part-01").exists()
    assert (workspace / "part-02.mp3").read_bytes() == b"mix-2"
    assert (workspace / "stems" / "part-02" / "Jonathan.flac").read_bytes() == b"2:host1"
    assert project.read_bytes().startswith(b"references old-session.mp3")
    assert _manifest(workspace)["parts"][0]["repair_manifest"] == "repair-manifest.json"


def test_single_sources_manifest_survives_default_refresh(editing_episode):
    e = editing_episode
    one = e.part(1, output_basename=e.session.session_uid)
    e.outputs(one)
    e.artifact(None, "transcript", f"{e.session.session_uid}.vtt", b"WEBVTT\n")
    e.artifact(
        one, "raw_track", "original.webm", b"exact-raw",
        role="host1", source_id="source-1",
    )
    workspace = e.workspace(include_sources=True)
    sources = _manifest(workspace)["parts"][0]["sources"]

    e.workspace()

    assert _manifest(workspace)["parts"][0]["sources"] == sources
    assert (workspace / sources[0]["filename"]).read_bytes() == b"exact-raw"


def test_workspace_sources_use_safe_collision_resistant_names(editing_episode):
    e = editing_episode
    one, two = e.part(1), e.part(2)
    for owner, role, source_id, filename in [
        (one, "host1", "../../outside", "input.webm"),
        (one, "guest2", "..\\..\\outside", "guest.wav"),
        (two, "host1", "normal", "input.opus"),
    ]:
        e.artifact(
            owner, "raw_track", filename, source_id.encode(),
            role=role, source_id=source_id,
        )

    workspace = e.workspace(include_sources=True)

    for part in _manifest(workspace)["parts"]:
        for source in part["sources"]:
            path = workspace / source["filename"]
            assert path.is_relative_to(workspace / "sources")
            assert path.read_bytes() == source["source_id"].encode()
            assert ".." not in path.name


def test_workspace_refuses_to_overwrite_user_symlink(editing_episode, tmp_path):
    e = editing_episode
    one, two = e.part(1), e.part(2)
    e.outputs(one)
    e.outputs(two)
    workspace = workspace_path_for(e.runtime, e.session.session_uid)
    workspace.mkdir(parents=True, exist_ok=True)
    user_file = tmp_path / "keep.mp3"
    user_file.write_bytes(b"do-not-follow")
    (workspace / "part-01.mp3").symlink_to(user_file)

    with pytest.raises(ArtifactError, match="symlink"):
        e.workspace()

    assert user_file.read_bytes() == b"do-not-follow"


@pytest.mark.parametrize("missing", ["pair", "transcript", "ingest"])
def test_finalize_rejects_missing_middle_part_before_storing_master(
    editing_episode, moto_s3, tmp_path, missing,
):
    from podline.repository import update_recording_part

    e = editing_episode
    one, two, three = e.part(1), e.part(2), e.part(3)
    e.outputs(one)
    e.outputs(three)
    if missing == "transcript":
        e.artifact(two, "pre_edit_master", "part-02.mp3", b"mix-2")
    if missing == "ingest":
        e.outputs(two)
        update_recording_part(e.runtime.db_file, two.part_id, ingest_complete=False)
    master = tmp_path / "assembled.wav"
    master.write_bytes(b"assembled-all-three")

    with pytest.raises(FinalizeError, match=r"Part 2|part 2"):
        finalize_session(
            config=e.config, runtime_paths=e.runtime, db_file=e.runtime.db_file,
            session=e.session, master_path=master,
        )

    assert get_media_artifact(
        e.runtime.db_file, e.session.session_uid, kind="final_master",
    ) is None
    assert get_session(e.runtime.db_file, e.session.session_uid) == e.session


@pytest.mark.parametrize("stale", ["status", "pre_edit_master", "transcript"])
def test_finalize_rejects_unfinished_or_obsolete_middle_part_outputs(
    editing_episode, moto_s3, tmp_path, stale,
):
    from podline.repository import update_recording_part

    e = editing_episode
    one, two, three = e.part(1), e.part(2), e.part(3)
    for owner in (one, two, three):
        e.outputs(owner)
    if stale == "status":
        update_recording_part(e.runtime.db_file, two.part_id, status="started")
        message = r"Part 2.*complete"
    else:
        extension = "mp3" if stale == "pre_edit_master" else "vtt"
        e.artifact(
            two, stale, f"part-02.{extension}", b"obsolete-output",
            production="replaced-production",
        )
        message = r"Part 2.*production"
    master = tmp_path / "assembled.wav"
    master.write_bytes(b"must-not-register")

    # The caller may still hold the old aggregate waiting_for_finalize record.
    with pytest.raises(FinalizeError, match=message):
        finalize_session(
            config=e.config, runtime_paths=e.runtime, db_file=e.runtime.db_file,
            session=e.session, master_path=master,
        )

    assert get_media_artifact(
        e.runtime.db_file, e.session.session_uid, kind="final_master",
    ) is None


def _finalize(e, tmp_path):
    master = tmp_path / "assembled.wav"
    master.write_bytes(b"editor-assembled-not-a-part")
    return finalize_session(
        config=e.config, runtime_paths=e.runtime, db_file=e.runtime.db_file,
        session=e.session, master_path=master,
    )


@pytest.mark.parametrize("consumer", ["metadata", "draft"])
@pytest.mark.parametrize("alias", [False, True])
def test_multipart_consumers_require_final_refresh_not_last_part_alias(
    editing_episode, moto_s3, tmp_path, consumer, alias,
):
    from podline.repository import upsert_episode_metadata

    e = editing_episode
    for number in (1, 2, 3):
        e.outputs(e.part(number))
    ready = _finalize(e, tmp_path)
    if alias:
        e.artifact(
            None, "transcript", "part-03.vtt", b"WEBVTT\n\nPart 3 only\n",
            production="prod-3",
        )
    upsert_episode_metadata(
        e.runtime.db_file, session_uid=e.session.session_uid,
        title="Stale part title", description="Must not publish",
    )
    client = _NoRemoteCalls()
    fn, error = (
        (generate_episode_metadata, EpisodeMetaError)
        if consumer == "metadata" else (create_podbean_draft, PodbeanError)
    )

    with pytest.raises(error, match="transcript-refresh"):
        fn(
            config=e.config, runtime_paths=e.runtime, db_file=e.runtime.db_file,
            session=ready, client=client,
        )


class _NoRemoteCalls:
    def __getattr__(self, name):
        pytest.fail(f"consumer must reject missing final refresh before remote call: {name}")


class _RefreshClient:
    def __init__(self):
        self.created = []
        self.started = []
        self.fail_download = True

    def create_single_input_production(self, **kwargs):
        self.created.append(kwargs)
        return "final-refresh"

    def start_production(self, production_id):
        self.started.append(production_id)

    def wait_for_production(self, production_id):
        from podline.auphonic import AuphonicOutputFile, AuphonicProduction

        return AuphonicProduction(
            production_id=production_id,
            output_files=[AuphonicOutputFile(
                kind="vtt", extension="vtt", download_url="https://fake.test/final.vtt",
            )],
        )

    def get_production(self, production_id):
        return {"data": {"status": "done", "output_files": [{
            "format": "subtitle", "ending": "vtt",
            "download_url": "https://fake.test/final.vtt",
        }]}}

    def download_file(self, url, destination):
        if self.fail_download:
            self.fail_download = False
            raise RuntimeError("interrupted final transcript download")
        destination.write_bytes(b"WEBVTT\n\nOnly the editor's assembled final cut\n")


class _DraftClient:
    def __init__(self):
        self.uploads = []
        self.drafts = []

    def get_access_token(self):
        return "fake-token"

    def list_episodes(self, token):
        return [{"episode_number": 10}]

    def authorize_upload(self, token, path, content_type):
        return {"presigned_url": f"https://fake.test/{path.name}", "file_key": path.name}

    def upload_presigned_file(self, url, path, content_type):
        self.uploads.append((path.name, path.read_bytes()))

    def create_draft_episode(self, token, **kwargs):
        self.drafts.append(kwargs)
        return {"episode": {"id": "one-draft", "url": "https://fake.test/draft"}}


def test_one_assembled_master_refresh_retry_metadata_and_draft(
    editing_episode, moto_s3, tmp_path,
):
    from podline.auphonic import refresh_transcript_with_auphonic
    from podline.episode_meta import EpisodeMetadataDraft
    from podline.repository import get_episode, upsert_episode_metadata

    e = editing_episode
    for number in (1, 2, 3):
        e.outputs(e.part(number))
    e.workspace()
    ready = _finalize(e, tmp_path)
    assert ready.status == "ready_for_draft"
    masters = list_media_artifacts(
        e.runtime.db_file, e.session.session_uid, kind="final_master", part_id=None,
    )
    assert len(masters) == 1
    assert masters[0].part_id is None
    assert masters[0].filename == "assembled.wav"
    assert get_episode(e.runtime.db_file, e.session.session_uid) is None
    upsert_episode_metadata(
        e.runtime.db_file, session_uid=e.session.session_uid,
        title="Old metadata", description="invalidated by refresh",
    )
    refresh = _RefreshClient()
    with pytest.raises(RuntimeError, match="interrupted final"):
        refresh_transcript_with_auphonic(
            config=e.config, runtime_paths=e.runtime, db_file=e.runtime.db_file,
            session=ready, client=refresh,
        )
    interrupted = get_session(e.runtime.db_file, e.session.session_uid)
    assert interrupted.transcript_refresh_production_id == "final-refresh"
    refreshed = refresh_transcript_with_auphonic(
        config=e.config, runtime_paths=e.runtime, db_file=e.runtime.db_file,
        session=interrupted, client=refresh,
    )
    assert len(refresh.created) == 1
    assert refresh.created[0]["input_key"].endswith("/assembled.wav")
    assert refresh.started == ["final-refresh"]
    assert get_episode(e.runtime.db_file, e.session.session_uid) is None
    transcript = get_media_artifact(
        e.runtime.db_file, e.session.session_uid, kind="transcript", part_id=None,
    )
    assert transcript.part_id is None
    assert transcript.source_id == "final-refresh"
    requests = []

    def parse(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(status="completed", output=[SimpleNamespace(
            type="message", content=[SimpleNamespace(
                type="output_text",
                parsed=EpisodeMetadataDraft(title="Final title", description="Final cut"),
            )],
        )])

    generate_episode_metadata(
        config=e.config, runtime_paths=e.runtime, db_file=e.runtime.db_file,
        session=refreshed,
        client=SimpleNamespace(responses=SimpleNamespace(parse=parse)),
    )
    assert requests[0]["input"][1]["content"] == (
        "WEBVTT\n\nOnly the editor's assembled final cut\n"
    )
    draft = _DraftClient()
    episode = create_podbean_draft(
        config=e.config, runtime_paths=e.runtime, db_file=e.runtime.db_file,
        session=refreshed, client=draft,
    )
    assert len(draft.drafts) == 1
    assert episode.podbean_post_id == "one-draft"
    assert draft.uploads == [
        ("assembled.wav", b"editor-assembled-not-a-part"),
        (transcript.filename, b"WEBVTT\n\nOnly the editor's assembled final cut\n"),
    ]
    assert get_session(e.runtime.db_file, e.session.session_uid).status == "draft_created"
