from pathlib import Path

import pytest

from podline.episode_meta import (
    EpisodeMetaError,
    PROJECT_ROOT,
    _parse_episode_meta_output,
    generate_episode_metadata_with_opencode,
)
from podline.repository import get_episode


def test_parse_episode_meta_output() -> None:
    title, description = _parse_episode_meta_output(
        "**Title:** Boba Boom\n\n**Description:**\nPasha and J. debate boba tea and trends."
    )

    assert title == "Boba Boom"
    assert description == "Pasha and J. debate boba tea and trends."


def test_episode_meta_prompt_uses_j_initial_for_cohost() -> None:
    prompt = (PROJECT_ROOT / ".opencode/commands/episode-meta.md").read_text(
        encoding="utf-8"
    )

    assert "Use `J.`" in prompt
    assert "Jonathan" not in prompt


def test_generate_episode_metadata_reads_transcript_artifact(
    monkeypatch,
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path: Path,
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = session_factory(
        runtime_paths.db_file,
        session_uid="session-meta-1",
        status="ready_for_draft",
    )
    transcript = tmp_path / "transcript.vtt"
    transcript.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n", encoding="utf-8"
    )
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid=session.session_uid,
        kind="transcript",
        path=transcript,
    )

    monkeypatch.setattr(
        "podline.episode_meta._resolve_opencode_binary",
        lambda: "/opt/homebrew/bin/opencode",
    )
    seen = {}

    def fake_run(opencode_binary, project_dir, transcript_text):
        seen["project_dir"] = project_dir
        seen["transcript_text"] = transcript_text
        return "**Title:** Boba Boom\n\n**Description:**\nPasha and J. debate boba tea and trends."

    monkeypatch.setattr(
        "podline.episode_meta._run_opencode_episode_meta", fake_run
    )

    episode = generate_episode_metadata_with_opencode(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=session,
    )

    assert episode.title == "Boba Boom"
    assert "boba tea" in episode.description
    assert seen["project_dir"] == PROJECT_ROOT
    assert "hello" in seen["transcript_text"]
    saved = get_episode(runtime_paths.db_file, session.session_uid)
    assert saved is not None
    assert saved.title == "Boba Boom"


def test_generate_episode_metadata_requires_transcript_artifact(
    app_config_factory, runtime_factory, session_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = session_factory(
        runtime_paths.db_file,
        session_uid="session-meta-2",
        status="ready_for_draft",
    )

    with pytest.raises(EpisodeMetaError, match="No transcript artifact"):
        generate_episode_metadata_with_opencode(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
        )
