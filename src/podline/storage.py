"""S3-backed media storage.

Objects are immutable and content-addressed:
    [{key_prefix}/]sessions/{session_uid}/{kind}/{sha256}/{filename}

The durable identity of an artifact is its s3:// URI plus its SHA-256.
Presigned URLs are ephemeral credentials — never persist or log them;
use redact_url() before including any URL in a log or error message.
"""

import hashlib
import os
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from podline.config import StorageConfig


class StorageError(RuntimeError):
    pass


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_session_key(
    *,
    key_prefix: str,
    session_uid: str,
    kind: str,
    sha256: str,
    filename: str,
) -> str:
    for label, value in (
        ("session_uid", session_uid),
        ("kind", kind),
        ("sha256", sha256),
        ("filename", filename),
    ):
        if not value or "/" in value:
            raise StorageError(f"Invalid {label} for S3 key: {value!r}")
    key = f"sessions/{session_uid}/{kind}/{sha256}/{filename}"
    prefix = key_prefix.strip("/")
    return f"{prefix}/{key}" if prefix else key


def parse_s3_uri(uri: str) -> Tuple[str, str]:
    parsed = urlsplit(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise StorageError(f"Not a valid s3:// URI: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def redact_url(url: str) -> str:
    """Strip the query string (where presigned-URL credentials live)."""
    parsed = urlsplit(str(url))
    if not parsed.query:
        return str(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")) + "?<redacted>"


def validate_storage_config(config: StorageConfig) -> None:
    if not config.enabled:
        raise StorageError("Storage is disabled in config.yaml (set storage.enabled)")
    if not config.bucket.strip():
        raise StorageError("storage.bucket is missing in config.yaml")


class MediaStorage:
    def __init__(self, config: StorageConfig) -> None:
        validate_storage_config(config)
        self._config = config
        self._client = boto3.client(
            "s3",
            region_name=config.region.strip() or None,
            config=BotoConfig(retries={"max_attempts": 5, "mode": "standard"}),
        )

    @property
    def bucket(self) -> str:
        return self._config.bucket

    def key_for(self, *, session_uid: str, kind: str, sha256: str, filename: str) -> str:
        return build_session_key(
            key_prefix=self._config.key_prefix,
            session_uid=session_uid,
            kind=kind,
            sha256=sha256,
            filename=filename,
        )

    def s3_uri(self, key: str) -> str:
        return f"s3://{self._config.bucket}/{key}"

    def head_object(self, key: str) -> Optional[Dict]:
        try:
            response = self._client.head_object(Bucket=self._config.bucket, Key=key)
        except ClientError as exc:
            if _client_error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise StorageError(f"S3 head_object failed for {key}: {exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"S3 head_object failed for {key}: {exc}") from exc
        return {
            "byte_size": int(response["ContentLength"]),
            "content_type": response.get("ContentType", ""),
        }

    def object_exists(self, key: str) -> bool:
        return self.head_object(key) is not None

    def upload_file(self, *, local_path: Path, key: str, content_type: str) -> str:
        """Upload a file and verify it landed. Idempotent for same-size objects.

        Keys are content-addressed, so an existing object with the expected
        size is the same content and the upload is skipped. An existing
        object with a different size means corruption or a hash collision in
        key construction; fail loudly rather than overwrite.
        """
        if not local_path.exists():
            raise StorageError(f"Cannot upload missing file: {local_path}")
        local_size = local_path.stat().st_size

        existing = self.head_object(key)
        if existing is not None:
            if existing["byte_size"] != local_size:
                raise StorageError(
                    f"S3 object {key} already exists with size {existing['byte_size']} "
                    f"but local file is {local_size} bytes; refusing to overwrite"
                )
            return self.s3_uri(key)

        try:
            self._client.upload_file(
                str(local_path),
                self._config.bucket,
                key,
                ExtraArgs={"ContentType": content_type},
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"S3 upload failed for {key}: {exc}") from exc

        uploaded = self.head_object(key)
        if uploaded is None or uploaded["byte_size"] != local_size:
            raise StorageError(
                f"S3 upload verification failed for {key}: expected {local_size} bytes, "
                f"found {uploaded['byte_size'] if uploaded else 'no object'}"
            )
        return self.s3_uri(key)

    def download_to_path(
        self,
        *,
        key: str,
        destination: Path,
        expected_sha256: str,
        expected_size: Optional[int] = None,
    ) -> Path:
        """Download an object with integrity verification and atomic placement.

        The object lands at `destination` only after its checksum matches;
        a partial or corrupt download never becomes visible.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        try:
            self._client.download_file(self._config.bucket, key, str(partial))
            actual_size = partial.stat().st_size
            if expected_size is not None and actual_size != expected_size:
                raise StorageError(
                    f"S3 download size mismatch for {key}: expected {expected_size}, got {actual_size}"
                )
            actual_sha256 = sha256_of_file(partial)
            if actual_sha256 != expected_sha256:
                raise StorageError(
                    f"S3 download checksum mismatch for {key}: expected {expected_sha256}, got {actual_sha256}"
                )
            os.replace(partial, destination)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"S3 download failed for {key}: {exc}") from exc
        finally:
            if partial.exists():
                partial.unlink()
        return destination

    def presigned_get_url(self, *, key: str, expires_seconds: int) -> str:
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._config.bucket, "Key": key},
                ExpiresIn=expires_seconds,
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"S3 presign failed for {key}: {exc}") from exc

    def check_access(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._config.bucket)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(
                f"S3 access check failed for bucket {self._config.bucket}: {exc}"
            ) from exc


def storage_health(config: StorageConfig) -> str:
    """One-line storage status for `pod health`."""
    if not config.enabled:
        return "disabled"
    try:
        MediaStorage(config).check_access()
    except StorageError as exc:
        return f"error: {exc}"
    return f"ok (bucket {config.bucket})"


def _client_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))
