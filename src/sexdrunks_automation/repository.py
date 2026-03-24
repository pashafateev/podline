from typing import List, Optional

from sexdrunks_automation.db import connect
from sexdrunks_automation.models import (
    AssetRecord,
    ScanCandidate,
    SessionRecord,
    SessionStatus,
)


def list_sessions(db_file: str) -> List[SessionRecord]:
    query = """
    SELECT session_uid, source_basename, source_date_folder, source_path,
           recorded_at, recorded_at_source, fingerprint, status,
           failure_reason, failed_at_stage, auphonic_production_id,
           pre_edit_master_path, transcript_path,
           created_at, updated_at
    FROM sessions
    ORDER BY created_at DESC
    """
    with connect(db_file) as connection:
        rows = connection.execute(query).fetchall()
    return [_row_to_session(row) for row in rows]


def get_session(db_file: str, session_uid: str) -> Optional[SessionRecord]:
    query = """
    SELECT session_uid, source_basename, source_date_folder, source_path,
           recorded_at, recorded_at_source, fingerprint, status,
           failure_reason, failed_at_stage, auphonic_production_id,
           pre_edit_master_path, transcript_path,
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


def get_session_by_fingerprint(
    db_file: str, fingerprint: str
) -> Optional[SessionRecord]:
    query = """
    SELECT session_uid, source_basename, source_date_folder, source_path,
           recorded_at, recorded_at_source, fingerprint, status,
           failure_reason, failed_at_stage, auphonic_production_id,
           pre_edit_master_path, transcript_path,
           created_at, updated_at
    FROM sessions
    WHERE fingerprint = ?
    """
    with connect(db_file) as connection:
        row = connection.execute(query, (fingerprint,)).fetchone()
    return _row_to_session(row) if row else None


def create_session(
    db_file: str,
    *,
    session_uid: str,
    fingerprint: str,
    candidate: ScanCandidate,
    timestamp: str,
) -> SessionRecord:
    with connect(db_file) as connection:
        connection.execute(
            """
            INSERT INTO sessions (
                session_uid,
                source_basename,
                source_date_folder,
                source_path,
                recorded_at,
                recorded_at_source,
                fingerprint,
                status,
                failure_reason,
                failed_at_stage,
                auphonic_production_id,
                pre_edit_master_path,
                transcript_path,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_uid,
                candidate.source_basename,
                candidate.date_folder_name,
                str(candidate.session_path),
                candidate.recorded_at,
                candidate.recorded_at_source,
                fingerprint,
                SessionStatus.QUEUED.value,
                None,
                None,
                None,
                None,
                None,
                timestamp,
                timestamp,
            ),
        )

        for track in candidate.spoken_tracks:
            connection.execute(
                """
                INSERT INTO assets (session_uid, role, filename, file_size, local_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_uid,
                    track.role,
                    track.path.name,
                    track.file_size,
                    str(track.path),
                ),
            )

        connection.execute(
            """
            INSERT INTO events (session_uid, from_state, to_state, timestamp, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_uid,
                None,
                SessionStatus.QUEUED.value,
                timestamp,
                "session discovered by scan",
            ),
        )
        connection.commit()

    created = get_session(db_file, session_uid)
    if created is None:
        raise RuntimeError(f"Failed to load created session {session_uid}")
    return created


def list_assets_for_session(db_file: str, session_uid: str) -> List[AssetRecord]:
    query = """
    SELECT role, filename, file_size, local_path
    FROM assets
    WHERE session_uid = ?
    ORDER BY role ASC, filename ASC
    """
    with connect(db_file) as connection:
        rows = connection.execute(query, (session_uid,)).fetchall()
    return [
        AssetRecord(
            role=row["role"],
            filename=row["filename"],
            file_size=row["file_size"],
            local_path=row["local_path"],
        )
        for row in rows
    ]


def update_session_status(
    db_file: str,
    *,
    session_uid: str,
    from_state: Optional[str],
    to_state: str,
    timestamp: str,
    detail: str,
    auphonic_production_id: Optional[str] = None,
    pre_edit_master_path: Optional[str] = None,
    transcript_path: Optional[str] = None,
    failure_reason: Optional[str] = None,
    failed_at_stage: Optional[str] = None,
) -> None:
    with connect(db_file) as connection:
        connection.execute(
            """
            UPDATE sessions
            SET status = ?,
                updated_at = ?,
                auphonic_production_id = COALESCE(?, auphonic_production_id),
                pre_edit_master_path = COALESCE(?, pre_edit_master_path),
                transcript_path = COALESCE(?, transcript_path),
                failure_reason = ?,
                failed_at_stage = ?
            WHERE session_uid = ?
            """,
            (
                to_state,
                timestamp,
                auphonic_production_id,
                pre_edit_master_path,
                transcript_path,
                failure_reason,
                failed_at_stage,
                session_uid,
            ),
        )
        connection.execute(
            """
            INSERT INTO events (session_uid, from_state, to_state, timestamp, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_uid, from_state, to_state, timestamp, detail),
        )
        connection.commit()


def _row_to_session(row) -> SessionRecord:
    return SessionRecord(
        session_uid=row["session_uid"],
        source_basename=row["source_basename"],
        source_date_folder=row["source_date_folder"],
        source_path=row["source_path"],
        recorded_at=row["recorded_at"],
        recorded_at_source=row["recorded_at_source"],
        fingerprint=row["fingerprint"],
        status=row["status"],
        failure_reason=row["failure_reason"],
        failed_at_stage=row["failed_at_stage"],
        auphonic_production_id=row["auphonic_production_id"],
        pre_edit_master_path=row["pre_edit_master_path"],
        transcript_path=row["transcript_path"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
