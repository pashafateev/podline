"""End-to-end regression for the S3-native pipeline.

Drives one session through every stage with no external drive paths at all:
import → auphonic prepare/offset/preview/start → workspace → finalize →
transcript refresh → episode metadata → Podbean draft. Auphonic, Podbean,
and the OpenAI API are faked; S3 is moto; everything else is real.
"""

from pathlib import Path
from types import SimpleNamespace

from podline.artifacts import workspace_path_for
from podline.auphonic import (
    AuphonicOutputFile,
    AuphonicPreparedProduction,
    AuphonicProduction,
    offset_session_auphonic_production,
    prepare_session_with_auphonic,
    preview_prepared_session_with_auphonic,
    refresh_transcript_with_auphonic,
    start_prepared_session_with_auphonic,
)
from podline.episode_meta import EpisodeMetadataDraft, generate_episode_metadata
from podline.finalize import finalize_session
from podline.ingest import import_local_folder
from podline.podbean import create_podbean_draft
from podline.repository import (
    get_media_artifact,
    get_session,
    list_media_artifacts,
    upsert_episode_metadata,
)


class WorkflowAuphonicClient:
    """Multitrack + single-input fake reflecting attach state."""

    def __init__(self) -> None:
        self.tracks = []
        self.started = []
        self.single_created = []

    def create_production(self, *, title, track_count, preset_id):
        assert preset_id == "preset-2"
        return AuphonicPreparedProduction(
            production_id="prod-flow",
            upload_track_ids=["Jonathan", "Pasha"],
            status_page="https://example.test/status/prod-flow",
            edit_page="https://example.test/edit/prod-flow",
        )

    def attach_service_input(self, production_id, track_id, service_id, input_key):
        assert service_id == "svc-podline"
        self.tracks.append(
            {"id": track_id, "type": "multitrack", "input_file": input_key}
        )
        return {"status_code": 200}

    def create_single_input_production(
        self, *, title, preset_id, service_id, input_key
    ):
        assert preset_id == "transcript-preset"
        assert input_key.endswith(".wav") or input_key.endswith(".mp3")
        self.single_created.append(input_key)
        return "prod-flow-refresh"

    def start_production(self, production_id):
        self.started.append(production_id)

    def wait_for_production(self, production_id):
        if production_id == "prod-flow-refresh":
            return AuphonicProduction(
                production_id=production_id,
                output_files=[
                    AuphonicOutputFile(
                        kind="vtt",
                        download_url="https://example.test/refreshed.vtt",
                        extension="vtt",
                    )
                ],
            )
        return AuphonicProduction(
            production_id=production_id,
            output_files=[
                AuphonicOutputFile(
                    kind="mp3",
                    download_url="https://example.test/master.mp3",
                    extension="mp3",
                ),
                AuphonicOutputFile(
                    kind="vtt",
                    download_url="https://example.test/transcript.vtt",
                    extension="vtt",
                ),
            ],
        )

    def download_file(self, url, destination):
        if url.endswith(".mp3"):
            destination.write_bytes(b"auphonic-pre-edit-master")
        elif url.endswith("refreshed.vtt"):
            destination.write_text(
                "WEBVTT\n\n00:00.000 --> 00:01.000\nFinal cut transcript\n",
                encoding="utf-8",
            )
        else:
            destination.write_text(
                "WEBVTT\n\n00:00.000 --> 00:01.000\nOriginal transcript\n",
                encoding="utf-8",
            )

    def get_production(self, production_id):
        return {
            "status_code": 200,
            "error_message": "",
            "data": {
                "uuid": production_id,
                "status": 10,
                "status_string": "Production Not Started Yet",
                "start_allowed": True,
                "change_allowed": True,
                "edit_page": f"https://example.test/edit/{production_id}",
                "status_page": f"https://example.test/status/{production_id}",
                "warning_message": "",
                "multi_input_files": [dict(track) for track in self.tracks],
                "output_files": [],
            },
        }

    def update_multi_input_file_offset(self, production_id, track_id, offset):
        for track in self.tracks:
            if track["id"] == track_id:
                track["offset"] = offset
        return {"status_code": 200, "data": {"id": track_id, "offset": offset}}


class WorkflowPodbeanClient:
    def __init__(self) -> None:
        self.uploaded = []

    def get_access_token(self):
        return "token"

    def list_episodes(self, access_token, limit=100):
        return [{"episode_number": 15}]

    def authorize_upload(self, access_token, file_path, content_type):
        return {
            "presigned_url": f"https://upload.test/{file_path.name}",
            "file_key": f"key-{file_path.name}",
        }

    def upload_presigned_file(self, presigned_url, file_path, content_type):
        assert file_path.exists()
        self.uploaded.append(file_path.name)

    def create_draft_episode(self, access_token, **kwargs):
        return {"episode": {"id": "pb-flow", "url": "https://podbean.test/pb-flow"}}


class WorkflowOpenAIClient:
    def __init__(self) -> None:
        self.responses = self

    def parse(self, **kwargs):
        transcript_input = kwargs["input"][1]
        assert transcript_input["role"] == "user"
        assert "Final cut transcript" in transcript_input["content"]
        return SimpleNamespace(
            status="completed",
            incomplete_details=None,
            output=[
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(
                            type="output_text",
                            parsed=EpisodeMetadataDraft(
                                title="Flow",
                                description="Full S3 pipeline.",
                            ),
                        )
                    ],
                )
            ],
        )


def test_full_s3_pipeline_without_any_drive_paths(
    moto_s3,
    app_config_factory,
    runtime_factory,
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = app_config_factory(tmp_path, podbean_enabled=True)
    runtime_paths = runtime_factory(config)
    db_file = runtime_paths.db_file

    # 1. Import a local two-track recording; source folder is not needed after.
    folder = tmp_path / "recordings" / "010124_013143"
    folder.mkdir(parents=True)
    (folder / "010124_013143_Tr1.WAV").write_bytes(b"track-one-bytes")
    (folder / "010124_013143_Tr2.WAV").write_bytes(b"track-two-bytes-longer")
    durations = {"Tr1": 101.5, "Tr2": 99.699}
    session, created = import_local_folder(
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        folder=folder,
        duration_probe=lambda path: durations[path.stem.rsplit("_", 1)[-1]],
    )
    assert created is True

    client = WorkflowAuphonicClient()

    # 2. Prepare: attach S3 keys through the registered service.
    prepared = prepare_session_with_auphonic(
        config=config, db_file=db_file, session=session, client=client
    )
    assert prepared.status == "auphonic_prepared"
    assert [t["id"] for t in client.tracks] == ["Jonathan", "Pasha"]

    # 3. Offset from stored durations (no probing, no local files).
    result = offset_session_auphonic_production(
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        session=prepared,
        apply=True,
        client=client,
        duration_probe=lambda path: (_ for _ in ()).throw(
            AssertionError("must not probe")
        ),
    )
    assert result.applied is True
    assert result.plan.targets[0].offset_seconds > 0

    # 4. Preview, then start; outputs persist to S3 and the workspace appears.
    preview = preview_prepared_session_with_auphonic(
        config=config, db_file=db_file, session=prepared, client=client
    )
    started = start_prepared_session_with_auphonic(
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        session=preview.session,
        client=client,
    )
    assert started.status == "waiting_for_finalize"
    workspace = workspace_path_for(runtime_paths, session.session_uid)
    pre_edit_name = f"{session.session_uid}_pre_edit.mp3"
    assert (workspace / pre_edit_name).read_bytes() == b"auphonic-pre-edit-master"

    # 5. Finalize with a master exported into the workspace.
    exported_master = workspace / "sexdrunks_ep99_master.wav"
    exported_master.write_bytes(b"edited-final-master")
    finalized = finalize_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        session=started,
        master_path=exported_master,
    )
    assert finalized.status == "ready_for_draft"

    # 6. Transcript refresh replaces the transcript artifact from the master.
    old_transcript = get_media_artifact(
        db_file, session.session_uid, kind="transcript"
    )
    refreshed = refresh_transcript_with_auphonic(
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        session=finalized,
        client=client,
    )
    new_transcript = get_media_artifact(
        db_file, session.session_uid, kind="transcript"
    )
    assert new_transcript.sha256 != old_transcript.sha256
    assert client.single_created[0].endswith("/sexdrunks_ep99_master.wav")

    # 7. Metadata generation reads the refreshed transcript artifact.
    generate_episode_metadata(
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        session=refreshed,
        client=WorkflowOpenAIClient(),
    )

    # 8. Podbean draft uploads materialized artifacts.
    podbean = WorkflowPodbeanClient()
    episode = create_podbean_draft(
        config=config,
        runtime_paths=runtime_paths,
        db_file=db_file,
        session=refreshed,
        client=podbean,
    )
    assert episode.episode_number == 16
    assert podbean.uploaded == [
        "sexdrunks_ep99_master.wav",
        f"{session.session_uid}.vtt",
    ]

    final = get_session(db_file, session.session_uid)
    assert final.status == "draft_created"
    kinds = {a.kind for a in list_media_artifacts(db_file, session.session_uid)}
    assert kinds == {"raw_track", "pre_edit_master", "transcript", "final_master"}
