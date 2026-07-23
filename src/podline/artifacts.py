"""Store and materialize media artifacts.

`store_artifact_file` is the single write path: hash → upload → verify →
register the DB row → seed the local cache. `materialize_artifact` is the
single read path: DB row → verified cache copy, downloading from S3 only
when needed. All pipeline stages that need a local file for an artifact go
through these two functions.

The cache lives under the runtime home, keyed by content hash
(cache/{sha256}/{filename}), so entries are immutable and safe to reuse
after checksum verification. There is no automatic eviction; `pod
cache-clear` empties it.
"""

import json
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from podline.config import AppConfig
from podline.models import MediaArtifactRecord, RuntimePaths
from podline.repository import (
    get_media_artifact,
    get_session,
    list_media_artifacts,
    register_media_artifact,
    replace_media_artifact_set,
)
from podline.storage import MediaStorage, StorageError, parse_s3_uri, sha256_of_file


class ArtifactError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceCleanupCandidate:
    session_uid: str
    path: Path
    status: Optional[str]
    age_days: float
    size_bytes: int
    eligible: bool
    reason: str


def store_artifact_file(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session_uid: str,
    kind: str,
    source_path: Path,
    content_type: str,
    role: str = "",
    duration_seconds: Optional[float] = None,
    source_provider: Optional[str] = None,
    source_id: Optional[str] = None,
    storage: Optional[MediaStorage] = None,
) -> MediaArtifactRecord:
    if not source_path.exists():
        raise ArtifactError(f"Cannot store missing file: {source_path}")

    storage = storage or MediaStorage(config.storage)
    sha256 = sha256_of_file(source_path)
    byte_size = source_path.stat().st_size
    key = storage.key_for(
        session_uid=session_uid,
        kind=kind,
        sha256=sha256,
        filename=source_path.name,
    )
    try:
        s3_uri = storage.upload_file(
            local_path=source_path,
            key=key,
            content_type=content_type,
            sha256=sha256,
        )
    except StorageError as exc:
        raise ArtifactError(str(exc)) from exc

    record = register_media_artifact(
        db_file,
        session_uid=session_uid,
        kind=kind,
        role=role,
        filename=source_path.name,
        content_type=content_type,
        byte_size=byte_size,
        sha256=sha256,
        duration_seconds=duration_seconds,
        s3_uri=s3_uri,
        source_provider=source_provider,
        source_id=source_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    seed_cache(runtime_paths, source_path=source_path, sha256=sha256)
    return record


def store_artifact_files(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session_uid: str,
    kind: str,
    files: List[dict],
    expected_roles: List[str],
    storage: Optional[MediaStorage] = None,
) -> List[MediaArtifactRecord]:
    """Upload, verify, and atomically register a complete role-keyed set."""
    roles = [str(item.get("role") or "") for item in files]
    if len(set(roles)) != len(roles):
        raise ArtifactError(f"Duplicate roles in {kind} artifact set")
    expected = set(expected_roles)
    actual = set(roles)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ArtifactError(
            f"Incomplete {kind} artifact set; missing={missing or 'none'} "
            f"extra={extra or 'none'}"
        )
    if any(not Path(item["source_path"]).exists() for item in files):
        missing_paths = [
            str(item["source_path"])
            for item in files
            if not Path(item["source_path"]).exists()
        ]
        raise ArtifactError(f"Cannot store missing files: {missing_paths}")

    storage = storage or MediaStorage(config.storage)
    timestamp = datetime.now(timezone.utc).isoformat()
    uploaded = []
    for item in files:
        source_path = Path(item["source_path"])
        sha256 = sha256_of_file(source_path)
        byte_size = source_path.stat().st_size
        content_type = str(item.get("content_type") or content_type_for(source_path))
        key = storage.key_for(
            session_uid=session_uid,
            kind=kind,
            sha256=sha256,
            filename=source_path.name,
        )
        try:
            s3_uri = storage.upload_file(
                local_path=source_path,
                key=key,
                content_type=content_type,
                sha256=sha256,
            )
        except StorageError as exc:
            raise ArtifactError(str(exc)) from exc
        uploaded.append(
            {
                "role": str(item["role"]),
                "filename": source_path.name,
                "content_type": content_type,
                "byte_size": byte_size,
                "sha256": sha256,
                "duration_seconds": item.get("duration_seconds"),
                "s3_uri": s3_uri,
                "source_provider": item.get("source_provider"),
                "source_id": item.get("source_id"),
            }
        )

    records = replace_media_artifact_set(
        db_file,
        session_uid=session_uid,
        kind=kind,
        artifacts=uploaded,
        timestamp=timestamp,
    )
    for item in files:
        # Cache population is recoverable and happens only after the complete
        # durable set is current in the database.
        source_path = Path(item["source_path"])
        matching = next(row for row in records if row.role == item["role"])
        seed_cache(runtime_paths, source_path=source_path, sha256=matching.sha256)
    return records


def materialize_artifact(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session_uid: str,
    kind: str,
    role: str = "",
    storage: Optional[MediaStorage] = None,
) -> Path:
    artifact = get_media_artifact(db_file, session_uid, kind=kind, role=role)
    if artifact is None:
        raise ArtifactError(
            f"No {kind} artifact registered for session {session_uid}"
        )
    return materialize_artifact_record(
        config=config,
        runtime_paths=runtime_paths,
        artifact=artifact,
        storage=storage,
    )


def materialize_artifact_record(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    artifact: MediaArtifactRecord,
    storage: Optional[MediaStorage] = None,
) -> Path:
    cached = cache_path_for(runtime_paths, artifact.sha256, artifact.filename)
    if cached.exists():
        if sha256_of_file(cached) == artifact.sha256:
            return cached
        # A corrupt or partial cache entry is replaceable by definition.
        cached.unlink()

    storage = storage or MediaStorage(config.storage)
    bucket, key = parse_s3_uri(artifact.s3_uri)
    if bucket != storage.bucket:
        # v1 is deliberately single-bucket: a mismatch means config.local.yaml
        # points somewhere other than where this artifact durably lives.
        raise ArtifactError(
            f"Artifact {artifact.kind}/{artifact.role or 'default'} for session "
            f"{artifact.session_uid} lives in s3://{bucket} but storage is "
            f"configured for bucket {storage.bucket}"
        )
    try:
        return storage.download_to_path(
            key=key,
            destination=cached,
            expected_sha256=artifact.sha256,
            expected_size=artifact.byte_size,
        )
    except StorageError as exc:
        raise ArtifactError(str(exc)) from exc


def content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".m4a":
        return "audio/mp4"
    if suffix == ".webm":
        return "audio/webm"
    if suffix == ".opus":
        return "audio/ogg"
    if suffix == ".flac":
        return "audio/flac"
    if suffix in {".aif", ".aiff"}:
        return "audio/aiff"
    if suffix == ".vtt":
        return "text/vtt"
    return "application/octet-stream"


def cache_path_for(runtime_paths: RuntimePaths, sha256: str, filename: str) -> Path:
    return Path(runtime_paths.cache_dir) / sha256 / filename


def workspace_path_for(runtime_paths: RuntimePaths, session_uid: str) -> Path:
    return Path(runtime_paths.workspaces_dir) / session_uid


def create_workspace(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session_uid: str,
    storage: Optional[MediaStorage] = None,
    include_stems: bool = False,
    stem_metadata: Optional[Dict[str, Dict[str, object]]] = None,
) -> Path:
    """Materialize the current pre-edit master and transcript for editing.

    Workspaces persist until removed by hand and are never subject to cache
    clearing; re-running refreshes the copies to the current artifacts.
    """
    from podline.models import ArtifactKind

    workspace = workspace_path_for(runtime_paths, session_uid)
    workspace.mkdir(parents=True, exist_ok=True)

    manifest_entries = {}
    for kind in (ArtifactKind.PRE_EDIT_MASTER.value, ArtifactKind.TRANSCRIPT.value):
        artifact = get_media_artifact(db_file, session_uid, kind=kind)
        if artifact is None:
            raise ArtifactError(
                f"No {kind} artifact registered for session {session_uid}; "
                "run the Auphonic stage before creating a workspace"
            )
        cached = materialize_artifact_record(
            config=config,
            runtime_paths=runtime_paths,
            artifact=artifact,
            storage=storage,
        )
        target = workspace / artifact.filename
        if not target.exists() or sha256_of_file(target) != artifact.sha256:
            shutil.copy2(cached, target)
        manifest_entries[kind] = {
            "filename": artifact.filename,
            "sha256": artifact.sha256,
            "s3_uri": artifact.s3_uri,
        }

    manifest = workspace / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "session_uid": session_uid,
                "artifacts": manifest_entries,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    registered_stems = list_media_artifacts(
        db_file, session_uid, kind=ArtifactKind.PRE_EDIT_STEM.value
    )
    if not registered_stems or (
        not include_stems
        and not _stem_workspace_matches_registered(workspace, registered_stems)
    ):
        _remove_obsolete_stem_workspace_outputs(workspace)
    if include_stems:
        _materialize_workspace_stems(
            config=config,
            runtime_paths=runtime_paths,
            db_file=db_file,
            session_uid=session_uid,
            workspace=workspace,
            stem_metadata=stem_metadata or {},
            storage=storage,
        )
    return workspace


def _stem_workspace_matches_registered(
    workspace: Path, stems: List[MediaArtifactRecord]
) -> bool:
    manifest = _read_json_object(workspace / "repair-manifest.json")
    production_ids = {stem.source_id for stem in stems}
    if (
        len(production_ids) != 1
        or None in production_ids
        or manifest.get("auphonic_production_id") not in production_ids
    ):
        return False
    manifest_by_role = {
        str(entry.get("role")): entry
        for entry in manifest.get("stems", [])
        if isinstance(entry, dict) and entry.get("role")
    }
    if set(manifest_by_role) != {stem.role for stem in stems}:
        return False
    return all(
        manifest_by_role[stem.role].get("filename") == stem.filename
        and manifest_by_role[stem.role].get("source_sha256") == stem.sha256
        for stem in stems
    )


def _remove_obsolete_stem_workspace_outputs(workspace: Path) -> None:
    repair_manifest_path = workspace / "repair-manifest.json"
    previous_manifest = _read_json_object(repair_manifest_path)
    managed_filenames = {
        Path(str(entry.get("filename") or "")).name
        for entry in previous_manifest.get("stems", [])
        if isinstance(entry, dict) and entry.get("filename")
    }
    repair_manifest_path.unlink(missing_ok=True)
    stems_dir = workspace / "stems"
    if not stems_dir.is_dir():
        return
    for filename in managed_filenames:
        entry = stems_dir / filename
        if entry.is_file() or entry.is_symlink():
            entry.unlink()
    try:
        stems_dir.rmdir()
    except OSError:
        pass


def _materialize_workspace_stems(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session_uid: str,
    workspace: Path,
    stem_metadata: Dict[str, Dict[str, object]],
    storage: Optional[MediaStorage],
) -> None:
    from podline.models import ArtifactKind

    stems = list_media_artifacts(
        db_file, session_uid, kind=ArtifactKind.PRE_EDIT_STEM.value
    )
    if not stems:
        raise ArtifactError(
            f"No pre_edit_stem artifacts registered for session {session_uid}; "
            "request Individual Tracks / FLAC in Auphonic and run auphonic-sync"
        )
    roles = {stem.role for stem in stems}
    metadata_roles = set(stem_metadata)
    if roles != metadata_roles:
        raise ArtifactError(
            "Auphonic stem metadata does not match the persisted roles; "
            f"missing={sorted(roles - metadata_roles) or 'none'} "
            f"extra={sorted(metadata_roles - roles) or 'none'}"
        )

    production_ids = {stem.source_id for stem in stems if stem.source_id}
    if len(production_ids) != 1:
        raise ArtifactError(
            "Persisted stems must all reference one Auphonic production"
        )
    production_id = next(iter(production_ids))

    durations = {}
    offsets = {}
    for stem in stems:
        if stem.duration_seconds is None or not math.isfinite(stem.duration_seconds):
            raise ArtifactError(
                f"Stem {stem.filename} has no valid duration for alignment"
            )
        duration = float(stem.duration_seconds)
        offset = float(stem_metadata[stem.role].get("offset_seconds") or 0)
        if duration < 0 or offset < 0 or not math.isfinite(offset):
            raise ArtifactError(
                f"Stem {stem.filename} has invalid duration/offset metadata"
            )
        durations[stem.role] = duration
        offsets[stem.role] = offset

    raw_values = list(durations.values())
    shifted_values = [durations[role] + offsets[role] for role in roles]
    raw_spread = max(raw_values) - min(raw_values)
    shifted_spread = max(shifted_values) - min(shifted_values)
    alignment_tolerance_seconds = 0.05
    has_offsets = any(offset > 0 for offset in offsets.values())
    apply_offsets = (
        has_offsets
        and shifted_spread + alignment_tolerance_seconds < raw_spread
    )
    offsets_rendered_by_auphonic = (
        has_offsets
        and raw_spread + alignment_tolerance_seconds < shifted_spread
    )
    target_duration = max(shifted_values) if apply_offsets else None

    stems_dir = workspace / "stems"
    stems_dir.mkdir(parents=True, exist_ok=True)
    repair_manifest_path = workspace / "repair-manifest.json"
    previous_manifest = _read_json_object(repair_manifest_path)
    previous_entries = {
        str(entry.get("role")): entry
        for entry in previous_manifest.get("stems", [])
        if isinstance(entry, dict) and entry.get("role")
    }
    current_filenames = {stem.filename for stem in stems}
    for entry in previous_entries.values():
        previous_filename = str(entry.get("filename") or "")
        if previous_filename and previous_filename not in current_filenames:
            stale = stems_dir / Path(previous_filename).name
            if stale.exists() and stale.parent == stems_dir:
                stale.unlink()

    manifest_stems = []
    for stem in sorted(stems, key=lambda item: item.role):
        cached = materialize_artifact_record(
            config=config,
            runtime_paths=runtime_paths,
            artifact=stem,
            storage=storage,
        )
        target = stems_dir / stem.filename
        podline_offset = offsets[stem.role] if apply_offsets else 0.0
        previous = previous_entries.get(stem.role, {})
        reusable = (
            target.exists()
            and previous.get("source_sha256") == stem.sha256
            and previous.get("podline_offset_applied_seconds") == podline_offset
            and previous_manifest.get("target_duration_seconds") == target_duration
            and previous.get("sha256") == sha256_of_file(target)
        )
        if not reusable:
            if apply_offsets:
                _materialize_aligned_stem(
                    cached,
                    target,
                    offset_seconds=offsets[stem.role],
                    target_duration_seconds=float(target_duration),
                )
            elif not target.exists() or sha256_of_file(target) != stem.sha256:
                shutil.copy2(cached, target)

        manifest_stems.append(
            {
                "role": stem.role,
                "filename": stem.filename,
                "track_id": str(stem_metadata[stem.role].get("track_id") or ""),
                "sha256": sha256_of_file(target),
                "source_sha256": stem.sha256,
                "auphonic_production_id": production_id,
                "auphonic_offset_seconds": offsets[stem.role],
                "podline_offset_applied_seconds": podline_offset,
                "loudness_normalized": False,
            }
        )

    repair_manifest_path.write_text(
        json.dumps(
            {
                "session_uid": session_uid,
                "auphonic_production_id": production_id,
                "loudness_normalized": False,
                "final_loudness_check_required": True,
                "offsets_rendered_by_auphonic": offsets_rendered_by_auphonic,
                "target_duration_seconds": target_duration,
                "stems": manifest_stems,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _read_json_object(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _materialize_aligned_stem(
    source: Path,
    destination: Path,
    *,
    offset_seconds: float,
    target_duration_seconds: float,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ArtifactError(
            "ffmpeg is required to apply Auphonic offsets to repair stems"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.stem}.podline-tmp{destination.suffix}"
    )
    if temporary.exists():
        temporary.unlink()
    delay_ms = int(round(offset_seconds * 1000))
    audio_filter = (
        f"adelay={delay_ms}:all=1,apad,"
        f"atrim=duration={target_duration_seconds:.6f}"
    )
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-af",
            audio_filter,
            "-c:a",
            "flac",
            str(temporary),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not temporary.exists():
        temporary.unlink(missing_ok=True)
        raise ArtifactError(
            f"ffmpeg could not align repair stem {source.name}: "
            f"{result.stderr.strip() or 'unknown error'}"
        )
    temporary.replace(destination)


def plan_workspace_cleanup(
    runtime_paths: RuntimePaths,
    db_file: str,
    *,
    older_than_days: float,
    now: Optional[float] = None,
) -> List[WorkspaceCleanupCandidate]:
    """Decide which workspaces are safe to remove.

    A workspace is eligible only when its session reached draft_created AND
    nothing inside it was modified for older_than_days. Sessions mid-pipeline,
    recently touched workspaces, and directories without a session record are
    kept. Media re-materializes from S3; the only unrecoverable content is a
    Cubase project saved inside the workspace, which the age window guards.
    """
    # Written as a negated >= so NaN is rejected too: every comparison against
    # NaN is False, so `age_days < NaN` would otherwise make each released
    # workspace — including one saved minutes ago — instantly eligible.
    if not (older_than_days >= 0):
        raise ArtifactError(
            f"Workspace retention must be at least 0 days, got {older_than_days:g}"
        )
    now = time.time() if now is None else now
    workspaces_root = Path(runtime_paths.workspaces_dir)
    if not workspaces_root.exists():
        return []

    candidates = []
    for entry in sorted(workspaces_root.iterdir()):
        if not entry.is_dir():
            continue
        session = get_session(db_file, entry.name)
        newest_mtime = max(
            [entry.stat().st_mtime]
            + [path.stat().st_mtime for path in entry.rglob("*")],
        )
        age_days = max(0.0, (now - newest_mtime) / 86400)
        size_bytes = sum(
            path.stat().st_size for path in entry.rglob("*") if path.is_file()
        )

        if session is None:
            eligible, reason = False, "no session record; remove by hand if unwanted"
        elif session.status != "draft_created":
            eligible, reason = False, f"session is {session.status}, not draft_created"
        elif age_days < older_than_days:
            eligible, reason = (
                False,
                f"modified {age_days:.1f}d ago (< {older_than_days:g}d)",
            )
        else:
            eligible, reason = True, f"draft created; untouched for {age_days:.1f}d"

        candidates.append(
            WorkspaceCleanupCandidate(
                session_uid=entry.name,
                path=entry,
                status=session.status if session else None,
                age_days=age_days,
                size_bytes=size_bytes,
                eligible=eligible,
                reason=reason,
            )
        )
    return candidates


def apply_workspace_cleanup(
    candidates: List[WorkspaceCleanupCandidate],
) -> int:
    """Remove eligible workspaces; returns bytes freed."""
    freed = 0
    for candidate in candidates:
        if not candidate.eligible:
            continue
        shutil.rmtree(candidate.path)
        freed += candidate.size_bytes
    return freed


def cache_usage_bytes(runtime_paths: RuntimePaths) -> int:
    cache_dir = Path(runtime_paths.cache_dir)
    if not cache_dir.exists():
        return 0
    return sum(
        path.stat().st_size for path in cache_dir.rglob("*") if path.is_file()
    )


def clear_cache(runtime_paths: RuntimePaths) -> int:
    """Remove every cache entry; returns bytes freed."""
    freed = cache_usage_bytes(runtime_paths)
    cache_dir = Path(runtime_paths.cache_dir)
    if cache_dir.exists():
        for entry in cache_dir.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    return freed


def seed_cache(
    runtime_paths: RuntimePaths, *, source_path: Path, sha256: str
) -> None:
    """Register a just-uploaded local file as the cache entry for its hash.

    Hardlink when possible so seeding large masters costs no disk; fall back
    to a copy across filesystems. Cache seeding is best-effort — the object
    is already durable in S3.
    """
    cached = cache_path_for(runtime_paths, sha256, source_path.name)
    if cached.exists():
        return
    cached.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source_path, cached)
    except OSError:
        try:
            shutil.copy2(source_path, cached)
        except OSError:
            if cached.exists():
                cached.unlink()
