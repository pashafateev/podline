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

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from podline.config import AppConfig
from podline.models import MediaArtifactRecord, RuntimePaths
from podline.repository import get_media_artifact, register_media_artifact
from podline.storage import MediaStorage, StorageError, parse_s3_uri, sha256_of_file


class ArtifactError(RuntimeError):
    pass


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
    _seed_cache(runtime_paths, source_path=source_path, sha256=sha256)
    return record


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


def cache_path_for(runtime_paths: RuntimePaths, sha256: str, filename: str) -> Path:
    return Path(runtime_paths.cache_dir) / sha256 / filename


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


def _seed_cache(
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
