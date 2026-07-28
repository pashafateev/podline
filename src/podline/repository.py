from typing import Dict, List, Optional

from podline.db import connect
from podline.models import (
    ArtifactKind,
    AssetRecord,
    EpisodeRecord,
    MediaArtifactRecord,
    ScanCandidate,
    SessionRecord,
    SessionStatus,
)

_MEDIA_ARTIFACT_COLUMNS = """
    session_uid, kind, role, filename, content_type, byte_size, sha256,
    duration_seconds, s3_uri, source_provider, source_id, created_at, updated_at
"""


def list_sessions(db_file: str) -> List[SessionRecord]:
    query = """
    SELECT session_uid, source_basename, source_date_folder, source_path,
           recorded_at, recorded_at_source, fingerprint, status,
           failure_reason, failed_at_stage, auphonic_production_id,
           auphonic_offsets_verified_production_id,
           transcript_refresh_production_id,
           transcript_refresh_source_sha256,
           transcript_refreshed_from_sha256,
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
           auphonic_offsets_verified_production_id,
           transcript_refresh_production_id,
           transcript_refresh_source_sha256,
           transcript_refreshed_from_sha256,
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
           auphonic_offsets_verified_production_id,
           transcript_refresh_production_id,
           transcript_refresh_source_sha256,
           transcript_refreshed_from_sha256,
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
        connection.execute(
            """
            INSERT INTO events (session_uid, from_state, to_state, timestamp, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_uid, from_state, to_state, timestamp, detail),
        )
        connection.commit()


def reconcile_artifact_roles(
    db_file: str,
    *,
    session_uid: str,
    status: str,
    role_updates: Dict[str, str],
    timestamp: str,
    detail: str,
) -> None:
    """Rewrite raw-track roles in place (media_artifacts and legacy assets).

    role_updates maps current role -> corrected role. All renames go through
    a temporary phase inside one transaction so pure swaps neither trip the
    (session_uid, kind, role) unique index nor cascade into each other.
    """
    if not role_updates:
        return
    items = sorted(role_updates.items())
    kind = ArtifactKind.RAW_TRACK.value
    with connect(db_file) as connection:
        for index, (old_role, _new_role) in enumerate(items):
            placeholder = f"__reconcile_{index}__"
            cursor = connection.execute(
                """
                UPDATE media_artifacts
                SET role = ?, updated_at = ?
                WHERE session_uid = ? AND kind = ? AND role = ?
                """,
                (placeholder, timestamp, session_uid, kind, old_role),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Expected exactly one {kind} artifact with role {old_role!r} "
                    f"for {session_uid}; found {cursor.rowcount}"
                )
            connection.execute(
                "UPDATE assets SET role = ? WHERE session_uid = ? AND role = ?",
                (placeholder, session_uid, old_role),
            )
        for index, (_old_role, new_role) in enumerate(items):
            placeholder = f"__reconcile_{index}__"
            connection.execute(
                """
                UPDATE media_artifacts
                SET role = ?
                WHERE session_uid = ? AND kind = ? AND role = ?
                """,
                (new_role, session_uid, kind, placeholder),
            )
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
) -> int:
    """Fill missing raw-track source ids by content hash.

    Only artifacts without a source id are touched — an existing id is
    provenance and never overwritten. Returns the number of updated rows.
    """
    if not source_ids_by_sha256:
        return 0
    updated = 0
    kind = ArtifactKind.RAW_TRACK.value
    with connect(db_file) as connection:
        for sha256, source_id in sorted(source_ids_by_sha256.items()):
            cursor = connection.execute(
                """
                UPDATE media_artifacts
                SET source_id = ?, updated_at = ?
                WHERE session_uid = ? AND kind = ? AND sha256 = ?
                  AND (source_id IS NULL OR source_id = '')
                """,
                (source_id, timestamp, session_uid, kind, sha256),
            )
            updated += cursor.rowcount
        connection.commit()
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
            SET status = ?,
                auphonic_production_id = NULL,
                auphonic_offsets_verified_production_id = NULL,
                failure_reason = NULL,
                failed_at_stage = NULL,
                updated_at = ?
            WHERE session_uid = ? AND status = ?
            """,
            (SessionStatus.QUEUED.value, timestamp, session_uid, from_state),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Could not reset session {session_uid} from {from_state} to queued; "
                "its status changed underneath the reset"
            )
        connection.execute(
            """
            INSERT INTO events (session_uid, from_state, to_state, timestamp, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_uid, from_state, SessionStatus.QUEUED.value, timestamp, detail),
        )
        connection.commit()


def record_auphonic_offsets_verified(
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
            SET auphonic_offsets_verified_production_id = ?, updated_at = ?
            WHERE session_uid = ?
              AND status = ?
              AND auphonic_production_id = ?
            """,
            (production_id, timestamp, session_uid, status, production_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Could not record verified Auphonic offsets for {session_uid}; "
                "the production or session status changed"
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
                f"verified offsets for Auphonic production {production_id}",
            ),
        )
        connection.commit()


def record_transcript_refresh_production(
    db_file: str,
    *,
    session_uid: str,
    status: str,
    production_id: str,
    final_master_sha256: str,
    timestamp: str,
) -> None:
    with connect(db_file) as connection:
        cursor = connection.execute(
            """
            UPDATE sessions
            SET transcript_refresh_production_id = ?,
                transcript_refresh_source_sha256 = ?,
                updated_at = ?
            WHERE session_uid = ? AND transcript_refresh_production_id IS NULL
            """,
            (production_id, final_master_sha256, timestamp, session_uid),
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
            SET transcript_refresh_production_id = NULL,
                transcript_refresh_source_sha256 = NULL,
                updated_at = ?
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
    final_master_sha256: str,
    timestamp: str,
) -> None:
    with connect(db_file) as connection:
        final_master = connection.execute(
            """
            SELECT sha256
            FROM media_artifacts
            WHERE session_uid = ? AND kind = ?
            """,
            (session_uid, ArtifactKind.FINAL_MASTER.value),
        ).fetchone()
        if (
            final_master is None
            or final_master["sha256"] != final_master_sha256
        ):
            raise RuntimeError(
                f"Could not complete transcript refresh for {session_uid}; "
                "the current final master changed"
            )
        cursor = connection.execute(
            """
            UPDATE sessions
            SET transcript_refresh_production_id = NULL,
                transcript_refresh_source_sha256 = NULL,
                transcript_refreshed_from_sha256 = ?,
                updated_at = ?,
                failure_reason = NULL,
                failed_at_stage = NULL
            WHERE session_uid = ?
              AND transcript_refresh_production_id = ?
              AND transcript_refresh_source_sha256 = ?
            """,
            (
                final_master_sha256,
                timestamp,
                session_uid,
                production_id,
                final_master_sha256,
            ),
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
        auphonic_offsets_verified_production_id=row[
            "auphonic_offsets_verified_production_id"
        ],
        transcript_refresh_production_id=row[
            "transcript_refresh_production_id"
        ],
        transcript_refresh_source_sha256=row[
            "transcript_refresh_source_sha256"
        ],
        transcript_refreshed_from_sha256=row[
            "transcript_refreshed_from_sha256"
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
        backfilled = 0
        kind = ArtifactKind.RAW_TRACK.value
        for sha256, source_id in sorted((source_ids_by_sha256 or {}).items()):
            id_cursor = connection.execute(
                """
                UPDATE media_artifacts
                SET source_id = ?, updated_at = ?
                WHERE session_uid = ? AND kind = ? AND sha256 = ?
                  AND (source_id IS NULL OR source_id = '')
                """,
                (source_id, timestamp, session_uid, kind, sha256),
            )
            backfilled += id_cursor.rowcount
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
           auphonic_offsets_verified_production_id,
           transcript_refresh_production_id,
           transcript_refresh_source_sha256,
           transcript_refreshed_from_sha256,
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
) -> None:
    connection.execute(
        """
        INSERT INTO media_artifacts (
            session_uid, kind, role, filename, content_type, byte_size,
            sha256, duration_seconds, s3_uri, source_provider, source_id,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_uid, kind, role) DO UPDATE SET
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
) -> MediaArtifactRecord:
    """Register the current artifact for (session, kind, role), replacing any prior row.

    Rows are replace-in-place; superseded S3 objects stay in the bucket and
    serve as history.
    """
    with connect(db_file) as connection:
        _insert_media_artifact(
            connection,
            session_uid=session_uid,
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

    record = get_media_artifact(db_file, session_uid, kind=kind, role=role)
    if record is None:
        raise RuntimeError(
            f"Failed to register media artifact {kind}/{role or 'default'} for {session_uid}"
        )
    return record


def replace_media_artifact_set(
    db_file: str,
    *,
    session_uid: str,
    kind: str,
    artifacts: List[dict],
    timestamp: str,
) -> List[MediaArtifactRecord]:
    """Atomically replace every current role for one artifact kind.

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
        connection.execute(
            f"DELETE FROM media_artifacts WHERE session_uid = ? AND kind = ? "
            f"AND role NOT IN ({placeholders})",
            [session_uid, kind, *roles],
        )
        for artifact in artifacts:
            _insert_media_artifact(
                connection,
                session_uid=session_uid,
                kind=kind,
                timestamp=timestamp,
                **artifact,
            )
        connection.commit()

    return list_media_artifacts(db_file, session_uid, kind=kind)


def delete_media_artifacts(db_file: str, *, session_uid: str, kind: str) -> None:
    """Remove the current rows for an optional artifact kind."""
    with connect(db_file) as connection:
        connection.execute(
            "DELETE FROM media_artifacts WHERE session_uid = ? AND kind = ?",
            (session_uid, kind),
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
        for artifact in artifacts:
            _insert_media_artifact(
                connection,
                session_uid=session_uid,
                timestamp=timestamp,
                **artifact,
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
) -> None:
    with connect(db_file) as connection:
        cursor = connection.execute(
            """
            UPDATE media_artifacts
            SET duration_seconds = ?, updated_at = ?
            WHERE session_uid = ? AND kind = ? AND role = ?
            """,
            (duration_seconds, timestamp, session_uid, kind, role),
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
) -> Optional[MediaArtifactRecord]:
    query = f"""
    SELECT {_MEDIA_ARTIFACT_COLUMNS}
    FROM media_artifacts
    WHERE session_uid = ? AND kind = ? AND role = ?
    """
    with connect(db_file) as connection:
        row = connection.execute(query, (session_uid, kind, role)).fetchone()
    return _row_to_media_artifact(row) if row else None


def list_media_artifacts(
    db_file: str,
    session_uid: str,
    *,
    kind: Optional[str] = None,
) -> List[MediaArtifactRecord]:
    query = f"""
    SELECT {_MEDIA_ARTIFACT_COLUMNS}
    FROM media_artifacts
    WHERE session_uid = ?
    """
    params = [session_uid]
    if kind is not None:
        query += " AND kind = ?"
        params.append(kind)
    query += " ORDER BY kind ASC, role ASC"
    with connect(db_file) as connection:
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
