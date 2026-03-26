from sexdrunks_automation.db import initialize_database
from sexdrunks_automation.ingest import scan_and_ingest
from sexdrunks_automation.repository import count_sessions, list_sessions


def test_scan_and_ingest_is_idempotent(fixture_raw_root, tmp_path) -> None:
    db_file = tmp_path / "state.db"
    initialize_database(str(db_file))

    first = scan_and_ingest(fixture_raw_root, str(db_file))
    second = scan_and_ingest(fixture_raw_root, str(db_file))

    assert first.discovered == 2
    assert first.created == 2
    assert first.duplicates == 0
    assert len(first.unsupported) == 2

    assert second.discovered == 2
    assert second.created == 0
    assert second.duplicates == 2
    assert len(second.unsupported) == 2
    assert count_sessions(str(db_file)) == 2

    sessions = list_sessions(str(db_file))
    assert {session.source_basename for session in sessions} == {
        "010124_013143",
        "010724_013643",
    }
    assert all(session.recorded_at is not None for session in sessions)
    assert all(session.recorded_at_source is not None for session in sessions)


def test_scan_and_ingest_discovers_multiple_flat_sessions(tmp_path) -> None:
    raw_root = tmp_path / "raw"
    db_file = tmp_path / "state.db"
    initialize_database(str(db_file))

    session_one = raw_root / "010124_013143"
    session_two = raw_root / "010724_013643"
    session_one.mkdir(parents=True)
    session_two.mkdir(parents=True)

    (session_one / "010124_013143_Tr1.WAV").write_bytes(b"alpha-track-1")
    (session_one / "010124_013143_Tr2.WAV").write_bytes(b"alpha-track-2")
    (session_two / "010724_013643_Tr1.WAV").write_bytes(b"beta-track-1-different")
    (session_two / "010724_013643_Tr2.WAV").write_bytes(b"beta-track-2-different")

    outcome = scan_and_ingest(raw_root, str(db_file))

    assert outcome.discovered == 2
    assert outcome.created == 2
    sessions = list_sessions(str(db_file))
    assert len(sessions) == 2
    assert len({session.fingerprint for session in sessions}) == 2
