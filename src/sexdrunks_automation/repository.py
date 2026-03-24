from typing import List, Optional

from sexdrunks_automation.db import connect
from sexdrunks_automation.models import SessionRecord


def list_sessions(db_file: str) -> List[SessionRecord]:
    query = """
    SELECT session_uid, recorder_name, source_path, fingerprint, status,
           failure_reason, failed_at_stage, auphonic_production_id,
           created_at, updated_at
    FROM sessions
    ORDER BY created_at DESC
    """
    with connect(db_file) as connection:
        rows = connection.execute(query).fetchall()
    return [_row_to_session(row) for row in rows]


def get_session(db_file: str, session_uid: str) -> Optional[SessionRecord]:
    query = """
    SELECT session_uid, recorder_name, source_path, fingerprint, status,
           failure_reason, failed_at_stage, auphonic_production_id,
           created_at, updated_at
    FROM sessions
    WHERE session_uid = ?
    """
    with connect(db_file) as connection:
        row = connection.execute(query, (session_uid,)).fetchone()
    return _row_to_session(row) if row else None


def count_sessions(db_file: str) -> int:
    with connect(db_file) as connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()
    return int(row["count"])


def count_sessions_by_status(db_file: str, status: str) -> int:
    with connect(db_file) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM sessions WHERE status = ?",
            (status,),
        ).fetchone()
    return int(row["count"])


def _row_to_session(row) -> SessionRecord:
    return SessionRecord(
        session_uid=row["session_uid"],
        recorder_name=row["recorder_name"],
        source_path=row["source_path"],
        fingerprint=row["fingerprint"],
        status=row["status"],
        failure_reason=row["failure_reason"],
        failed_at_stage=row["failed_at_stage"],
        auphonic_production_id=row["auphonic_production_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
