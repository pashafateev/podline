from pathlib import Path

from sexdrunks_automation.db import initialize_database
from sexdrunks_automation.finalize import (
    FinalizeError,
    finalize_session,
    get_episode_metadata,
)
from sexdrunks_automation.ingest import scan_and_ingest
from sexdrunks_automation.repository import (
    get_session,
    list_sessions,
    update_session_status,
)


def test_finalize_session_generates_episode_metadata(
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
        "WEBVTT\n\n00:00.000 --> 00:01.000\nPasha and Jonathan talk about nightlife and embarrassment.\n",
        encoding="utf-8",
    )
    pre_edit = tmp_path / "pre_edit_master.mp3"
    pre_edit.write_bytes(b"pre-edit")
    update_session_status(
        str(db_file),
        session_uid=session.session_uid,
        from_state=session.status,
        to_state="waiting_for_finalize",
        timestamp="now",
        detail="ready for finalize",
        transcript_path=str(transcript),
        pre_edit_master_path=str(pre_edit),
    )
    waiting = get_session(str(db_file), session.session_uid)
    assert waiting is not None

    final_master = tmp_path / "episode_master.mp3"
    final_master.write_bytes(b"final-master")

    updated = finalize_session(
        db_file=str(db_file),
        session=waiting,
        master_path=final_master,
    )

    assert updated.status == "ready_for_draft"
    assert updated.final_master_path == str(final_master.resolve())
    episode = get_episode_metadata(str(db_file), updated.session_uid)
    assert episode is not None
    assert episode.title.startswith("Sex Drunks - ")
    assert "Transcript excerpt" in episode.description


def test_finalize_requires_waiting_for_finalize_status(
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
    final_master = tmp_path / "episode_master.mp3"
    final_master.write_bytes(b"final-master")

    try:
        finalize_session(
            db_file=str(db_file),
            session=session,
            master_path=final_master,
        )
    except FinalizeError as exc:
        assert "waiting_for_finalize" in str(exc)
    else:
        raise AssertionError("Expected FinalizeError for non-finalizable session")
