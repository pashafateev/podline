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
    assert {session.recorder_name for session in sessions} == {
        "010124_013143",
        "010724_013643",
    }
