from pathlib import Path

from podline.db import initialize_database
from podline.episode_meta import (
    PROJECT_ROOT,
    _parse_episode_meta_output,
    generate_episode_metadata_with_opencode,
)
from podline.ingest import scan_and_ingest
from podline.repository import (
    get_episode,
    list_sessions,
    update_session_status,
)
from podline.config import (
    AppConfig,
    PathsConfig,
    PollingConfig,
    NotificationsConfig,
    AuphonicConfig,
    PodbeanConfig,
    CozytrackConfig,
)


def _default_cozytrack_config() -> CozytrackConfig:
    return CozytrackConfig(
        base_url="http://localhost:3000",
        api_key="",
        download_subdir="cozytrack",
        request_timeout_seconds=600,
    )


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


def test_generate_episode_metadata_with_opencode_persists_episode(
    monkeypatch, fixture_raw_root, tmp_path: Path
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
        detail="ready for metadata",
        transcript_path=str(transcript),
        pre_edit_master_path=str(pre_edit),
        final_master_path=str(final_master),
    )
    session = next(
        item
        for item in list_sessions(str(db_file))
        if item.session_uid == session.session_uid
    )

    monkeypatch.setattr(
        "podline.episode_meta._resolve_opencode_binary",
        lambda: "/opt/homebrew/bin/opencode",
    )
    seen = {}
    monkeypatch.setattr(
        "podline.episode_meta._run_opencode_episode_meta",
        lambda opencode_binary, project_dir, transcript_text: (
            seen.update({"project_dir": project_dir})
            or "**Title:** Boba Boom\n\n**Description:**\nPasha and J. debate boba tea and trends."
        ),
    )

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
            enabled=False,
            client_id="",
            client_secret="",
            base_url="https://api.podbean.example/v1",
            podcast_id="",
        ),
        cozytrack=_default_cozytrack_config(),
        config_file=tmp_path / "config.yaml",
    )

    episode = generate_episode_metadata_with_opencode(
        config=config, db_file=str(db_file), session=session
    )

    assert episode.title == "Boba Boom"
    assert "boba tea" in episode.description
    assert seen["project_dir"] == PROJECT_ROOT
    saved = get_episode(str(db_file), session.session_uid)
    assert saved is not None
    assert saved.title == "Boba Boom"
