from pathlib import Path

import pytest

from podline.finalize import (
    FinalizeError,
    finalize_session,
    get_episode_metadata,
)
from podline.repository import get_media_artifact
from podline.storage import sha256_of_file


def _waiting_session(
    tmp_path: Path, session_factory, artifact_registrar, runtime_paths
):
    session = session_factory(
        runtime_paths.db_file,
        session_uid="session-finalize-1",
        status="waiting_for_finalize",
    )
    pre_edit = tmp_path / "pre_edit_master.mp3"
    pre_edit.write_bytes(b"pre-edit")
    transcript = tmp_path / "transcript.vtt"
    transcript.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nPasha and J. talk about nightlife.\n",
        encoding="utf-8",
    )
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid=session.session_uid,
        kind="pre_edit_master",
        path=pre_edit,
    )
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid=session.session_uid,
        kind="transcript",
        path=transcript,
    )
    return session


def test_finalize_session_stores_master_artifact(
    moto_s3, app_config_factory, runtime_factory, session_factory, artifact_registrar, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _waiting_session(
        tmp_path, session_factory, artifact_registrar, runtime_paths
    )

    final_master = tmp_path / "episode_master.mp3"
    final_master.write_bytes(b"final-master")

    updated = finalize_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=session,
        master_path=final_master,
    )

    assert updated.status == "ready_for_draft"
    artifact = get_media_artifact(
        runtime_paths.db_file, session.session_uid, kind="final_master"
    )
    assert artifact is not None
    assert artifact.sha256 == sha256_of_file(final_master)
    assert artifact.filename == "episode_master.mp3"
    assert artifact.source_provider == "local_edit"
    assert get_episode_metadata(runtime_paths.db_file, updated.session_uid) is None


def test_finalize_requires_waiting_for_finalize_status(
    moto_s3, app_config_factory, runtime_factory, session_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = session_factory(runtime_paths.db_file, status="queued")
    final_master = tmp_path / "episode_master.mp3"
    final_master.write_bytes(b"final-master")

    with pytest.raises(FinalizeError, match="waiting_for_finalize"):
        finalize_session(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            master_path=final_master,
        )


def test_finalize_requires_pipeline_artifacts(
    moto_s3, app_config_factory, runtime_factory, session_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = session_factory(
        runtime_paths.db_file, status="waiting_for_finalize"
    )
    final_master = tmp_path / "episode_master.mp3"
    final_master.write_bytes(b"final-master")

    with pytest.raises(FinalizeError, match="has no transcript artifact"):
        finalize_session(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            master_path=final_master,
        )


def test_finalize_accepts_shell_escaped_spaces_in_master_path(
    moto_s3, app_config_factory, runtime_factory, session_factory, artifact_registrar, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = _waiting_session(
        tmp_path, session_factory, artifact_registrar, runtime_paths
    )

    actual_dir = tmp_path / "folder with spaces"
    actual_dir.mkdir()
    final_master = actual_dir / "episode master.mp3"
    final_master.write_bytes(b"final-master")

    escaped_path = Path(str(final_master).replace(" ", "\\ "))
    updated = finalize_session(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=session,
        master_path=escaped_path,
    )

    artifact = get_media_artifact(
        runtime_paths.db_file, updated.session_uid, kind="final_master"
    )
    assert artifact is not None
    assert artifact.filename == "episode master.mp3"
    assert artifact.sha256 == sha256_of_file(final_master)
