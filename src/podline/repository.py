import json
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import uuid4

from podline.db import (
    connect,
    ensure_default_recording_part,
    recording_part_id,
    recording_part_status,
)
from podline.models import (
    ArtifactKind,
    AssetRecord,
    EpisodeRecord,
    MediaArtifactRecord,
    RecordingPartRecord,
    ScanCandidate,
    SessionRecord,
    SessionStatus,
)

_MEDIA_ARTIFACT_COLUMNS = """
    session_uid, kind, role, filename, content_type, byte_size, sha256,
    duration_seconds, s3_uri, source_provider, source_id, created_at, updated_at, part_id
"""


def _row_to_recording_part(row) -> RecordingPartRecord:
    values = dict(row)
    values["ingest_complete"] = bool(values["ingest_complete"])
    values["source_snapshot"] = json.loads(values["source_snapshot"])
    values["timing_warnings"] = json.loads(values["timing_warnings"])
    return RecordingPartRecord(**values)


def get_recording_part(db_file: str, part_id: str) -> Optional[RecordingPartRecord]:
    with connect(db_file) as connection:
        row = connection.execute(
            "SELECT * FROM recording_parts WHERE part_id = ?", (part_id,)
        ).fetchone()
    return _row_to_recording_part(row) if row is not None else None


def list_recording_parts(db_file: str, session_uid: str) -> List[RecordingPartRecord]:
    with connect(db_file) as connection:
        rows = connection.execute(
            "SELECT * FROM recording_parts WHERE session_uid = ? ORDER BY part_number",
            (session_uid,),
        ).fetchall()
    return [_row_to_recording_part(row) for row in rows]


def ensure_default_part(db_file: str, session_uid: str) -> RecordingPartRecord:
    """Return the sole part, or lazily project a directly registered session."""
    with connect(db_file) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = ensure_default_recording_part(connection, session_uid, ingest_complete=True)
    return _row_to_recording_part(row)


def create_recording_part(
    db_file: str,
    *,
    session_uid: str,
    part_number: int,
    take_id: Optional[str] = None,
    source_snapshot: Optional[dict] = None,
    started_at: Optional[str] = None,
    stopped_at: Optional[str] = None,
    take_status: Optional[str] = None,
    status: str = "queued",
    ingest_complete: bool = True,
    output_basename: Optional[str] = None,
) -> RecordingPartRecord:
    if type(part_number) is not int or part_number <= 0:
        raise ValueError("part_number must be a positive integer")
    if source_snapshot is None:
        source_snapshot = {}
    if not isinstance(source_snapshot, dict):
        raise ValueError("source_snapshot must be a dict")
    if not isinstance(ingest_complete, bool):
        raise ValueError("ingest_complete must be a bool")
    timestamp = datetime.now(timezone.utc).isoformat()
    part_id = recording_part_id(session_uid, part_number)
    with connect(db_file) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_session(connection, session_uid)
        connection.execute(
            """
            INSERT INTO recording_parts (
                part_id, session_uid, part_number, take_id, status, ingest_complete,
                source_snapshot, started_at, stopped_at, take_status, output_basename,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                part_id, session_uid, part_number, take_id, status, int(ingest_complete),
                json.dumps(source_snapshot), started_at, stopped_at, take_status,
                output_basename, timestamp, timestamp,
            ),
        )
        row = connection.execute(
            "SELECT * FROM recording_parts WHERE part_id = ?", (part_id,)
        ).fetchone()
    return _row_to_recording_part(row)


def update_recording_part(db_file: str, part_id: str, **fields) -> RecordingPartRecord:
    """Update only mutable checkpoints; explicit None clears optional fields."""
    allowed = {
        "take_id", "status", "auphonic_production_id", "ingest_complete",
        "source_snapshot", "timing_warnings", "started_at", "stopped_at",
        "take_status", "output_basename", "operation", "failure_reason",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown or immutable part fields: {sorted(unknown)}")
    for key, value_type in (("source_snapshot", dict), ("timing_warnings", list)):
        if key in fields:
            if not isinstance(fields[key], value_type):
                raise ValueError(f"{key} must be a {value_type.__name__}")
            fields[key] = json.dumps(fields[key])
    if "ingest_complete" in fields:
        if not isinstance(fields["ingest_complete"], bool):
            raise ValueError("ingest_complete must be a bool")
        fields["ingest_complete"] = int(fields["ingest_complete"])
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with connect(db_file) as connection:
        cursor = connection.execute(
            f"UPDATE recording_parts SET {assignments} WHERE part_id = ?",
            [*fields.values(), part_id],
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown part {part_id}")
        row = connection.execute(
            "SELECT * FROM recording_parts WHERE part_id = ?", (part_id,)
        ).fetchone()
    return _row_to_recording_part(row)


def add_recording_part_timing_warnings(db_file: str, part_id: str, warnings: list):
    """Retain known evidence across independent ingest/offset checkpoints."""
    if not isinstance(warnings, list):
        raise ValueError("timing_warnings must be a list")
    with connect(db_file) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT timing_warnings FROM recording_parts WHERE part_id = ?", (part_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown part {part_id}")
        merged = json.loads(row["timing_warnings"])
        for warning in warnings:
            if warning not in merged:
                merged.append(warning)
        connection.execute(
            "UPDATE recording_parts SET timing_warnings = ?, updated_at = ? WHERE part_id = ?",
            (json.dumps(merged), datetime.now(timezone.utc).isoformat(), part_id),
        )
    return merged


def _write_part_source_metadata(connection, session_uid, metadata, part_id=None):
    """Commit recorder evidence with the initial source association, not after it."""
    if metadata is None:
        return
    if part_id is None:
        part = ensure_default_recording_part(connection, session_uid, ingest_complete=True)
    else:
        part = connection.execute(
            "SELECT * FROM recording_parts WHERE part_id=? AND session_uid=?",
            (part_id, session_uid),
        ).fetchone()
        if part is None:
            raise ValueError("Source metadata part does not belong to this episode")
    warnings = json.loads(part["timing_warnings"])
    for warning in metadata.get("timing_warnings", []):
        if warning not in warnings:
            warnings.append(warning)
    snapshot = {**json.loads(part["source_snapshot"]), **metadata["source_snapshot"]}
    connection.execute(
        "UPDATE recording_parts SET take_id=?, source_snapshot=?, timing_warnings=?, "
        "started_at=?, stopped_at=?, take_status=? WHERE part_id=?",
        (metadata.get("take_id"), json.dumps(snapshot), json.dumps(warnings),
         metadata.get("started_at"), metadata.get("stopped_at"), metadata.get("take_status"),
         part["part_id"]),
    )
    connection.execute(
        "UPDATE sessions SET participant_roles=? WHERE session_uid=?",
        (json.dumps(metadata.get("participant_roles", {})), session_uid),
    )


def claim_recording_part_operation(db_file: str, part_id: str, operation: str) -> bool:
    """Claim idle work once. An uncertain claim never expires automatically."""
    if not operation:
        raise ValueError("operation must not be empty")
    with connect(db_file) as connection:
        cursor = connection.execute(
            "UPDATE recording_parts SET operation = ?, updated_at = ? "
            "WHERE part_id = ? AND operation IS NULL "
            "AND (? != 'create' OR auphonic_production_id IS NULL)",
            (operation, datetime.now(timezone.utc).isoformat(), part_id, operation),
        )
        if cursor.rowcount == 1:
            return True
        if connection.execute(
            "SELECT 1 FROM recording_parts WHERE part_id = ?", (part_id,)
        ).fetchone() is None:
            raise ValueError(f"Unknown part {part_id}")
    return False


def _require_session(connection, session_uid: str):
    row = connection.execute(
        "SELECT * FROM sessions WHERE session_uid = ?", (session_uid,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown session {session_uid}")
    return row


def get_participant_roles(db_file: str, session_uid: str) -> Dict[str, str]:
    with connect(db_file) as connection:
        row = _require_session(connection, session_uid)
    return json.loads(row["participant_roles"])


def set_participant_roles(
    db_file: str, session_uid: str, roles: Dict[str, str]
) -> Dict[str, str]:
    if not isinstance(roles, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in roles.items()
    ):
        raise ValueError("Participant roles must map string participant IDs to roles")
    with connect(db_file) as connection:
        cursor = connection.execute(
            "UPDATE sessions SET participant_roles = ? WHERE session_uid = ?",
            (json.dumps(roles), session_uid),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown session {session_uid}")
    return dict(roles)


_PART_OWNED_KINDS = {
    ArtifactKind.RAW_TRACK.value,
    ArtifactKind.PRE_EDIT_MASTER.value,
    ArtifactKind.PRE_EDIT_STEM.value,
}


def _resolve_artifact_part_id(
    connection, session_uid: str, kind: Optional[str], part_id: Optional[str]
) -> Optional[str]:
    if part_id is not None:
        part = connection.execute(
            "SELECT session_uid FROM recording_parts WHERE part_id = ?", (part_id,)
        ).fetchone()
        if part is None or part["session_uid"] != session_uid:
            raise ValueError(f"Part {part_id} does not belong to session {session_uid}")
        if kind == ArtifactKind.FINAL_MASTER.value:
            raise ValueError("final_master artifacts must be episode-scoped")
        return part_id
    if kind not in _PART_OWNED_KINDS:
        return None
    if connection.execute(
        "SELECT 1 FROM sessions WHERE session_uid = ?", (session_uid,)
    ).fetchone() is None:
        # Existing storage-only callers use orphan artifact rows. They cannot
        # acquire a concrete part without a real parent session.
        return None
    return ensure_default_recording_part(
        connection, session_uid, ingest_complete=True
    )["part_id"]


def resolve_artifact_part_id(
    db_file: str, session_uid: str, *, kind: str, part_id: Optional[str] = None
) -> Optional[str]:
    """Resolve/validate ownership before uploading; mutations recheck in-transaction."""
    with connect(db_file) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return _resolve_artifact_part_id(connection, session_uid, kind, part_id)


def list_sessions(db_file: str) -> List[SessionRecord]:
    query = """
    SELECT session_uid, source_basename, source_date_folder, source_path,
           recorded_at, recorded_at_source, fingerprint, status,
           failure_reason, failed_at_stage, auphonic_production_id,
           transcript_refresh_production_id,
           pre_edit_master_path, transcript_path, final_master_path,
           cozytrack_session_id, created_at, updated_at
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
           transcript_refresh_production_id,
           pre_edit_master_path, transcript_path, final_master_path,
           cozytrack_session_id, created_at, updated_at
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
           transcript_refresh_production_id,
           pre_edit_master_path, transcript_path, final_master_path,
           cozytrack_session_id, created_at, updated_at
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
    cozytrack_session_id: Optional[str] = None,
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
                final_master_path,
                cozytrack_session_id,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                None,
                cozytrack_session_id,
                timestamp,
                timestamp,
            ),
        )
        ensure_default_recording_part(connection, session_uid, ingest_complete=True)

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
    final_master_path: Optional[str] = None,
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
                final_master_path = COALESCE(?, final_master_path),
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
                final_master_path,
                failure_reason,
                failed_at_stage,
                session_uid,
            ),
        )
        _project_legacy_session_state(connection, session_uid)
        connection.execute(
            """
            INSERT INTO events (session_uid, from_state, to_state, timestamp, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_uid, from_state, to_state, timestamp, detail),
        )
        connection.commit()


def _project_legacy_session_state(connection, session_uid: str) -> None:
    """Keep old single-part callers compatible without touching owned snapshots."""
    parts = connection.execute(
        "SELECT part_id, source_snapshot FROM recording_parts WHERE session_uid = ?",
        (session_uid,),
    ).fetchall()
    if len(parts) != 1 or json.loads(parts[0]["source_snapshot"]):
        return
    session = _require_session(connection, session_uid)
    connection.execute(
        "UPDATE recording_parts SET status = ?, auphonic_production_id = ?, "
        "failure_reason = ?, updated_at = ? WHERE part_id = ?",
        (
            recording_part_status(session["status"]), session["auphonic_production_id"],
            session["failure_reason"], session["updated_at"], parts[0]["part_id"],
        ),
    )


def reconcile_artifact_roles(
    db_file: str,
    *,
    session_uid: str,
    status: str,
    role_updates: Dict[str, str],
    timestamp: str,
    detail: str,
    part_id: Optional[str] = None,
) -> None:
    """Rewrite raw-track roles in place (media_artifacts and legacy assets).

    role_updates maps current role -> corrected role. All renames go through
    a temporary phase inside one transaction so pure swaps neither trip the
    scoped unique index nor cascade into each other. Legacy assets are a
    single-part projection and must never be rewritten for a multipart episode.
    """
    items = sorted(role_updates.items())
    kind = ArtifactKind.RAW_TRACK.value
    with connect(db_file) as connection:
        connection.execute("BEGIN IMMEDIATE")
        part_id = _resolve_artifact_part_id(connection, session_uid, kind, part_id)
        if not items:
            return
        update_assets = connection.execute(
            "SELECT COUNT(*) FROM recording_parts WHERE session_uid = ?", (session_uid,)
        ).fetchone()[0] <= 1
        prefix = f"__reconcile_{uuid4().hex}_"
        for index, (old_role, _new_role) in enumerate(items):
            placeholder = f"{prefix}{index}__"
            cursor = connection.execute(
                """
                UPDATE media_artifacts
                SET role = ?, updated_at = ?
                WHERE session_uid = ? AND part_id IS ? AND kind = ? AND role = ?
                """,
                (placeholder, timestamp, session_uid, part_id, kind, old_role),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Expected exactly one {kind} artifact with role {old_role!r} "
                    f"for {session_uid}; found {cursor.rowcount}"
                )
            if update_assets:
                connection.execute(
                    "UPDATE assets SET role = ? WHERE session_uid = ? AND role = ?",
                    (placeholder, session_uid, old_role),
                )
        for index, (_old_role, new_role) in enumerate(items):
            placeholder = f"{prefix}{index}__"
            connection.execute(
                """
                UPDATE media_artifacts
                SET role = ?
                WHERE session_uid = ? AND part_id IS ? AND kind = ? AND role = ?
                """,
                (new_role, session_uid, part_id, kind, placeholder),
            )
            if update_assets:
                connection.execute(
                    "UPDATE assets SET role = ? WHERE session_uid = ? AND role = ?",
                    (new_role, session_uid, placeholder),
                )
        connection.execute(
            """
            INSERT INTO events (session_uid, from_state, to_state, timestamp, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_uid, status, status, timestamp, detail),
        )
        connection.commit()


def backfill_artifact_source_ids(
    db_file: str,
    *,
    session_uid: str,
    source_ids_by_sha256: Dict[str, str],
    timestamp: str,
    part_id: Optional[str] = None,
) -> int:
    """Fill missing raw-track source ids by content hash.

    Only artifacts without a source id are touched — an existing id is
    provenance and never overwritten. Returns the number of updated rows.
    """
    with connect(db_file) as connection:
        connection.execute("BEGIN IMMEDIATE")
        part_id = _resolve_artifact_part_id(
            connection, session_uid, ArtifactKind.RAW_TRACK.value, part_id
        )
        return _backfill_artifact_source_ids(
            connection, session_uid, part_id, source_ids_by_sha256, timestamp
        )


def _backfill_artifact_source_ids(
    connection, session_uid, part_id, source_ids_by_sha256, timestamp
) -> int:
    updated = 0
    kind = ArtifactKind.RAW_TRACK.value
    for sha256, source_id in sorted(source_ids_by_sha256.items()):
        cursor = connection.execute(
            """
            UPDATE media_artifacts
            SET source_id = ?, updated_at = ?
            WHERE session_uid = ? AND part_id IS ? AND kind = ? AND sha256 = ?
              AND (source_id IS NULL OR source_id = '')
            """,
            (source_id, timestamp, session_uid, part_id, kind, sha256),
        )
        updated += cursor.rowcount
    return updated


def reset_session_to_queued(
    db_file: str,
    *,
    session_uid: str,
    from_state: str,
    timestamp: str,
    detail: str,
) -> None:
    """Requeue a session and clear its Auphonic production id.

    Guarded by the caller's view of the current status so a concurrent
    transition fails loudly instead of silently resetting the wrong state.
    """
    with connect(db_file) as connection:
        cursor = connection.execute(
            """
            UPDATE sessions
            SET status = ?, auphonic_production_id = NULL, updated_at = ?
            WHERE session_uid = ? AND status = ?
            """,
            (SessionStatus.QUEUED.value, timestamp, session_uid, from_state),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Could not reset session {session_uid} from {from_state} to queued; "
                "its status changed underneath the reset"
            )
        _project_legacy_session_state(connection, session_uid)
        connection.execute(
            """
            INSERT INTO events (session_uid, from_state, to_state, timestamp, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_uid, from_state, SessionStatus.QUEUED.value, timestamp, detail),
        )
        connection.commit()


def record_transcript_refresh_production(
    db_file: str,
    *,
    session_uid: str,
    status: str,
    production_id: str,
    timestamp: str,
) -> None:
    with connect(db_file) as connection:
        cursor = connection.execute(
            """
            UPDATE sessions
            SET transcript_refresh_production_id = ?, updated_at = ?
            WHERE session_uid = ? AND transcript_refresh_production_id IS NULL
            """,
            (production_id, timestamp, session_uid),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Could not record transcript refresh production for {session_uid}"
            )
        connection.execute(
            """
            INSERT INTO events (session_uid, from_state, to_state, timestamp, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_uid,
                status,
                status,
                timestamp,
                f"created Auphonic transcript refresh production {production_id}",
            ),
        )
        connection.commit()


def discard_transcript_refresh_production(
    db_file: str,
    *,
    session_uid: str,
    status: str,
    production_id: str,
    timestamp: str,
    detail: str,
) -> None:
    with connect(db_file) as connection:
        cursor = connection.execute(
            """
            UPDATE sessions
            SET transcript_refresh_production_id = NULL, updated_at = ?
            WHERE session_uid = ? AND transcript_refresh_production_id = ?
            """,
            (timestamp, session_uid, production_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Could not discard transcript refresh production for {session_uid}"
            )
        connection.execute(
            """
            INSERT INTO events (session_uid, from_state, to_state, timestamp, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_uid, status, status, timestamp, detail),
        )
        connection.commit()


def complete_transcript_refresh(
    db_file: str,
    *,
    session_uid: str,
    status: str,
    production_id: str,
    timestamp: str,
) -> None:
    with connect(db_file) as connection:
        cursor = connection.execute(
            """
            UPDATE sessions
            SET transcript_refresh_production_id = NULL,
                updated_at = ?,
                failure_reason = NULL,
                failed_at_stage = NULL
            WHERE session_uid = ? AND transcript_refresh_production_id = ?
            """,
            (timestamp, session_uid, production_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Could not complete transcript refresh for {session_uid}"
            )
        connection.execute(
            "DELETE FROM episodes WHERE session_uid = ?",
            (session_uid,),
        )
        connection.execute(
            """
            INSERT INTO events (session_uid, from_state, to_state, timestamp, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_uid,
                status,
                status,
                timestamp,
                f"refreshed transcript from final master using Auphonic production {production_id}; episode metadata invalidated",
            ),
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
        transcript_refresh_production_id=row[
            "transcript_refresh_production_id"
        ],
        pre_edit_master_path=row["pre_edit_master_path"],
        transcript_path=row["transcript_path"],
        final_master_path=row["final_master_path"],
        cozytrack_session_id=row["cozytrack_session_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_cozytrack_session_ids(db_file: str) -> List[str]:
    with connect(db_file) as connection:
        rows = connection.execute(
            "SELECT cozytrack_session_id FROM sessions "
            "WHERE cozytrack_session_id IS NOT NULL"
        ).fetchall()
    return [row["cozytrack_session_id"] for row in rows]


def set_cozytrack_session_id(
    db_file: str,
    *,
    session_uid: str,
    cozytrack_session_id: str,
    status: str,
    timestamp: str,
    source_ids_by_sha256: Optional[Dict[str, str]] = None,
    part_id: Optional[str] = None,
    part_metadata: Optional[dict] = None,
) -> bool:
    """Associate a Cozytrack session with an existing unassociated session.

    Guarded so an already-associated session is never rewritten; returns
    False in that case. When source_ids_by_sha256 is given, the raw-track
    source ids are persisted in the same transaction as the association —
    an interrupted run must never leave an associated session without its
    source ids, because later ingests skip downloading and would have
    nothing left to join on.
    """
    with connect(db_file) as connection:
        connection.execute("BEGIN IMMEDIATE")
        part_id = _resolve_artifact_part_id(
            connection, session_uid, ArtifactKind.RAW_TRACK.value, part_id
        )
        cursor = connection.execute(
            """
            UPDATE sessions
            SET cozytrack_session_id = ?, updated_at = ?
            WHERE session_uid = ? AND cozytrack_session_id IS NULL
            """,
            (cozytrack_session_id, timestamp, session_uid),
        )
        if cursor.rowcount != 1:
            return False
        backfilled = _backfill_artifact_source_ids(
            connection, session_uid, part_id, source_ids_by_sha256 or {}, timestamp
        )
        _write_part_source_metadata(connection, session_uid, part_metadata, part_id)
        detail = (
            f"associated Cozytrack session {cozytrack_session_id} with existing content"
        )
        if backfilled:
            detail += f"; backfilled source ids for {backfilled} raw tracks"
        connection.execute(
            """
            INSERT INTO events (session_uid, from_state, to_state, timestamp, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_uid, status, status, timestamp, detail),
        )
        connection.commit()
    return True


def get_session_by_cozytrack_id(
    db_file: str, cozytrack_session_id: str
) -> Optional[SessionRecord]:
    query = """
    SELECT session_uid, source_basename, source_date_folder, source_path,
           recorded_at, recorded_at_source, fingerprint, status,
           failure_reason, failed_at_stage, auphonic_production_id,
           transcript_refresh_production_id,
           pre_edit_master_path, transcript_path, final_master_path,
           cozytrack_session_id, created_at, updated_at
    FROM sessions
    WHERE cozytrack_session_id = ?
    """
    with connect(db_file) as connection:
        row = connection.execute(query, (cozytrack_session_id,)).fetchone()
    return _row_to_session(row) if row else None


def _insert_media_artifact(
    connection,
    *,
    session_uid: str,
    kind: str,
    role: str,
    filename: str,
    content_type: str,
    byte_size: int,
    sha256: str,
    duration_seconds: Optional[float],
    s3_uri: str,
    source_provider: Optional[str],
    source_id: Optional[str],
    timestamp: str,
    part_id: Optional[str] = None,
) -> MediaArtifactRecord:
    part_id = _resolve_artifact_part_id(connection, session_uid, kind, part_id)
    conflict_target = (
        "(session_uid, kind, role) WHERE part_id IS NULL"
        if part_id is None else
        "(session_uid, part_id, kind, role) WHERE part_id IS NOT NULL"
    )
    connection.execute(
        f"""
        INSERT INTO media_artifacts (
            session_uid, part_id, kind, role, filename, content_type, byte_size,
            sha256, duration_seconds, s3_uri, source_provider, source_id,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT {conflict_target} DO UPDATE SET
            filename = excluded.filename,
            content_type = excluded.content_type,
            byte_size = excluded.byte_size,
            sha256 = excluded.sha256,
            duration_seconds = excluded.duration_seconds,
            s3_uri = excluded.s3_uri,
            source_provider = excluded.source_provider,
            source_id = excluded.source_id,
            updated_at = excluded.updated_at
        """,
        (
            session_uid,
            part_id,
            kind,
            role,
            filename,
            content_type,
            byte_size,
            sha256,
            duration_seconds,
            s3_uri,
            source_provider,
            source_id,
            timestamp,
            timestamp,
        ),
    )
    row = connection.execute(
        f"SELECT {_MEDIA_ARTIFACT_COLUMNS} FROM media_artifacts "
        "WHERE session_uid = ? AND part_id IS ? AND kind = ? AND role = ?",
        (session_uid, part_id, kind, role),
    ).fetchone()
    return _row_to_media_artifact(row)


def register_media_artifact(
    db_file: str,
    *,
    session_uid: str,
    kind: str,
    role: str,
    filename: str,
    content_type: str,
    byte_size: int,
    sha256: str,
    duration_seconds: Optional[float],
    s3_uri: str,
    source_provider: Optional[str],
    source_id: Optional[str],
    timestamp: str,
    part_id: Optional[str] = None,
) -> MediaArtifactRecord:
    """Upsert one (session, scope, kind, role), preserving its row identity.

    Rows are replace-in-place; superseded S3 objects stay in the bucket and
    serve as history.
    """
    with connect(db_file) as connection:
        connection.execute("BEGIN IMMEDIATE")
        record = _insert_media_artifact(
            connection,
            session_uid=session_uid,
            part_id=part_id,
            kind=kind,
            role=role,
            filename=filename,
            content_type=content_type,
            byte_size=byte_size,
            sha256=sha256,
            duration_seconds=duration_seconds,
            s3_uri=s3_uri,
            source_provider=source_provider,
            source_id=source_id,
            timestamp=timestamp,
        )
        connection.commit()
    return record


def replace_media_artifact_set(
    db_file: str,
    *,
    session_uid: str,
    kind: str,
    artifacts: List[dict],
    timestamp: str,
    part_id: Optional[str] = None,
) -> List[MediaArtifactRecord]:
    """Atomically replace every current role for one artifact kind and scope.

    Callers upload and verify the complete set before entering this function.
    The transaction then removes obsolete roles and upserts the new set as one
    unit, so readers never observe a partial stem set.
    """
    if not artifacts:
        raise ValueError("Artifact set must not be empty")
    roles = [str(artifact.get("role") or "") for artifact in artifacts]
    if any(not role for role in roles):
        raise ValueError("Every artifact in a role-keyed set must have a role")
    if len(set(roles)) != len(roles):
        raise ValueError("Artifact set contains duplicate roles")

    placeholders = ", ".join("?" for _role in roles)
    with connect(db_file) as connection:
        connection.execute("BEGIN IMMEDIATE")
        part_id = _resolve_artifact_part_id(connection, session_uid, kind, part_id)
        connection.execute(
            f"DELETE FROM media_artifacts WHERE session_uid = ? AND part_id IS ? AND kind = ? "
            f"AND role NOT IN ({placeholders})",
            [session_uid, part_id, kind, *roles],
        )
        records = []
        for artifact in artifacts:
            records.append(
                _insert_media_artifact(
                    connection,
                    session_uid=session_uid,
                    part_id=part_id,
                    kind=kind,
                    timestamp=timestamp,
                    **artifact,
                )
            )
        connection.commit()

    return sorted(records, key=lambda record: record.role)


def delete_media_artifacts(
    db_file: str, *, session_uid: str, kind: str, part_id: Optional[str] = None
) -> None:
    """Remove the current rows for an optional artifact kind in one scope."""
    with connect(db_file) as connection:
        connection.execute("BEGIN IMMEDIATE")
        part_id = _resolve_artifact_part_id(connection, session_uid, kind, part_id)
        connection.execute(
            "DELETE FROM media_artifacts WHERE session_uid = ? AND part_id IS ? AND kind = ?",
            (session_uid, part_id, kind),
        )
        connection.commit()


def create_session_with_artifacts(
    db_file: str,
    *,
    session_uid: str,
    fingerprint: str,
    candidate: ScanCandidate,
    timestamp: str,
    artifacts: List[dict],
    cozytrack_session_id: Optional[str] = None,
    part_metadata: Optional[dict] = None,
) -> SessionRecord:
    """Create an S3-native session: session row + artifact rows, one commit.

    No `assets` rows are written — S3-native sessions resolve media through
    media_artifacts only. Each artifact dict holds the keyword arguments of
    `_insert_media_artifact` minus session_uid/timestamp.
    """
    with connect(db_file) as connection:
        connection.execute(
            """
            INSERT INTO sessions (
                session_uid, source_basename, source_date_folder, source_path,
                recorded_at, recorded_at_source, fingerprint, status,
                failure_reason, failed_at_stage, auphonic_production_id,
                pre_edit_master_path, transcript_path, final_master_path,
                cozytrack_session_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                None,
                cozytrack_session_id,
                timestamp,
                timestamp,
            ),
        )
        ensure_default_recording_part(connection, session_uid, ingest_complete=True)
        for artifact in artifacts:
            _insert_media_artifact(
                connection,
                session_uid=session_uid,
                timestamp=timestamp,
                **artifact,
            )
        _write_part_source_metadata(connection, session_uid, part_metadata)
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
                f"session registered with {len(artifacts)} S3 artifacts",
            ),
        )
        connection.commit()

    created = get_session(db_file, session_uid)
    if created is None:
        raise RuntimeError(f"Failed to load created session {session_uid}")
    return created


def update_media_artifact_duration(
    db_file: str,
    *,
    session_uid: str,
    kind: str,
    role: str,
    duration_seconds: float,
    timestamp: str,
    part_id: Optional[str] = None,
) -> None:
    with connect(db_file) as connection:
        connection.execute("BEGIN IMMEDIATE")
        part_id = _resolve_artifact_part_id(connection, session_uid, kind, part_id)
        cursor = connection.execute(
            """
            UPDATE media_artifacts
            SET duration_seconds = ?, updated_at = ?
            WHERE session_uid = ? AND part_id IS ? AND kind = ? AND role = ?
            """,
            (duration_seconds, timestamp, session_uid, part_id, kind, role),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Could not persist duration for artifact {kind}/{role or 'default'} "
                f"of session {session_uid}"
            )
        connection.commit()


def get_media_artifact(
    db_file: str,
    session_uid: str,
    *,
    kind: str,
    role: str = "",
    part_id: Optional[str] = None,
) -> Optional[MediaArtifactRecord]:
    query = f"""
    SELECT {_MEDIA_ARTIFACT_COLUMNS}
    FROM media_artifacts
    WHERE session_uid = ? AND part_id IS ? AND kind = ? AND role = ?
    """
    with connect(db_file) as connection:
        part_id = _resolve_artifact_part_id(connection, session_uid, kind, part_id)
        row = connection.execute(query, (session_uid, part_id, kind, role)).fetchone()
    return _row_to_media_artifact(row) if row else None


def list_media_artifacts(
    db_file: str,
    session_uid: str,
    *,
    kind: Optional[str] = None,
    part_id: Optional[str] = None,
) -> List[MediaArtifactRecord]:
    """List one scope, except an unqualified kind=None is an episode aggregate."""
    query = f"""
    SELECT {_MEDIA_ARTIFACT_COLUMNS}
    FROM media_artifacts
    WHERE session_uid = ?
    """
    params = [session_uid]
    if kind is not None:
        query += " AND kind = ?"
        params.append(kind)
    with connect(db_file) as connection:
        if kind is not None or part_id is not None:
            part_id = _resolve_artifact_part_id(connection, session_uid, kind, part_id)
            query += " AND part_id IS ?"
            params.append(part_id)
        query += " ORDER BY kind ASC, part_id ASC, role ASC"
        rows = connection.execute(query, params).fetchall()
    return [_row_to_media_artifact(row) for row in rows]


def _row_to_media_artifact(row) -> MediaArtifactRecord:
    return MediaArtifactRecord(
        session_uid=row["session_uid"],
        kind=row["kind"],
        role=row["role"],
        filename=row["filename"],
        content_type=row["content_type"],
        byte_size=row["byte_size"],
        sha256=row["sha256"],
        duration_seconds=row["duration_seconds"],
        s3_uri=row["s3_uri"],
        source_provider=row["source_provider"],
        source_id=row["source_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        part_id=row["part_id"],
    )


def upsert_episode_metadata(
    db_file: str,
    *,
    session_uid: str,
    title: str,
    description: str,
) -> EpisodeRecord:
    with connect(db_file) as connection:
        connection.execute(
            """
            INSERT INTO episodes (session_uid, title, description)
            VALUES (?, ?, ?)
            ON CONFLICT(session_uid) DO UPDATE SET
                title = excluded.title,
                description = excluded.description
            """,
            (session_uid, title, description),
        )
        connection.commit()

    episode = get_episode(db_file, session_uid)
    if episode is None:
        raise RuntimeError(f"Failed to upsert episode metadata for {session_uid}")
    return episode


def get_episode(db_file: str, session_uid: str) -> Optional[EpisodeRecord]:
    with connect(db_file) as connection:
        row = connection.execute(
            """
            SELECT session_uid, episode_number, podbean_post_id, draft_url, title, description
            FROM episodes
            WHERE session_uid = ?
            """,
            (session_uid,),
        ).fetchone()
    if row is None:
        return None
    return EpisodeRecord(
        session_uid=row["session_uid"],
        episode_number=row["episode_number"],
        podbean_post_id=row["podbean_post_id"],
        draft_url=row["draft_url"],
        title=row["title"],
        description=row["description"],
    )
