"""S3-backed media storage.

Objects are immutable and content-addressed:
    [{key_prefix}/]sessions/{session_uid}/{kind}/{sha256}/{filename}

The durable identity of an artifact is its s3:// URI plus its SHA-256.
Presigned URLs are ephemeral credentials — never persist or log them;
use redact_url() before including any URL in a log or error message.
"""

import hashlib
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import boto3
from boto3.exceptions import Boto3Error
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from podline.config import StorageConfig

# ClientError and the boto3 transfer-layer errors (S3UploadFailedError,
# S3TransferFailedError) do NOT inherit from BotoCoreError; every S3 call
# must catch all three families or errors escape unwrapped.
_S3_ERRORS = (BotoCoreError, ClientError, Boto3Error)

_UPLOAD_ATTEMPTS = 3
_UPLOAD_BACKOFF_BASE_SECONDS = 1.0
_UPLOAD_BACKOFF_CAP_SECONDS = 4.0
_RETRYABLE_HTTP_STATUS_CODES = {408, 429}
_RETRYABLE_S3_ERROR_CODES = {
    "internalerror",
    "requesttimeout",
    "requesttimeoutexception",
    "serviceunavailable",
    "slowdown",
}
_PERMANENT_S3_ERROR_CODES = {
    # Authorization and credentials.
    "accessdenied",
    "accountproblem",
    "authorizationheadermalformed",
    "expiredtoken",
    "invalidaccesskeyid",
    "signaturedoesnotmatch",
    # Configuration and endpoint selection.
    "illegallocationconstraintexception",
    "incorrectendpoint",
    "invalidbucketname",
    "nosuchbucket",
    "permanentredirect",
    # Invalid requests, integrity failures, and content conflicts.
    "baddigest",
    "checksummismatch",
    "conditionalrequestconflict",
    "entitytoolarge",
    "entitytoosmall",
    "invalidargument",
    "invaliddigest",
    "invalidpart",
    "invalidpartorder",
    "invalidrequest",
    "malformedxml",
    "missingcontentlength",
    "nosuchupload",
    "operationaborted",
    "preconditionfailed",
}
_RETRYABLE_CONNECTION_ERRORS = (
    ConnectionClosedError,
    EndpointConnectionError,
    ConnectTimeoutError,
    ReadTimeoutError,
)
_EMBEDDED_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+")

logger = logging.getLogger("podline")


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


def _sanitize_error_text(value: object) -> str:
    """Redact query strings from every URL embedded in exception text."""

    return _EMBEDDED_URL_RE.sub(lambda match: redact_url(match.group(0)), str(value))


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield an exception and its explicit or implicit causes without cycles."""

    current: Optional[BaseException] = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_retryable_upload_error(exc: BaseException) -> bool:
    """Classify SDK upload failures from types and structured responses only."""

    chain = tuple(_exception_chain(exc))
    for nested in chain:
        if isinstance(nested, ClientError):
            code = _client_error_code(nested).lower()
            if code in _PERMANENT_S3_ERROR_CODES:
                return False

    for nested in chain:
        if isinstance(nested, _RETRYABLE_CONNECTION_ERRORS):
            return True
        if isinstance(nested, ClientError):
            code = _client_error_code(nested).lower()
            status = _client_error_status(nested)
            if code in _RETRYABLE_S3_ERROR_CODES:
                return True
            if status in _RETRYABLE_HTTP_STATUS_CODES or (
                status is not None and 500 <= status <= 599
            ):
                return True
    return False


def _upload_retry_delay(attempt: int) -> float:
    ceiling = min(
        _UPLOAD_BACKOFF_CAP_SECONDS,
        _UPLOAD_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
    )
    return (ceiling / 2) + random.uniform(0, ceiling / 2)


def validate_storage_config(config: StorageConfig) -> None:
    # storage.enabled is deliberately NOT checked here: it is an ingest-only
    # pause switch (enforced in podline.ingest). Sessions already registered
    # in S3 must stay readable and processable while intake is paused.
    if not config.bucket.strip():
        raise StorageError("storage.bucket is missing in config.yaml")


class MediaStorage:
    def __init__(self, config: StorageConfig) -> None:
        validate_storage_config(config)
        self._config = config
        try:
            # Client construction resolves credentials, so provider problems
            # (missing profile, expired login, absent optional deps) surface
            # here rather than at the first request.
            session = boto3.session.Session(
                profile_name=config.profile.strip() or None,
                region_name=config.region.strip() or None,
            )
            self._client = session.client(
                "s3",
                config=BotoConfig(retries={"max_attempts": 5, "mode": "standard"}),
            )
        except _S3_ERRORS as exc:
            raise StorageError(
                f"Could not create S3 client: {_sanitize_error_text(exc)}"
            ) from None

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
            raise StorageError(
                f"S3 head_object failed for {key}: {_sanitize_error_text(exc)}"
            ) from None
        except (BotoCoreError, Boto3Error) as exc:
            raise StorageError(
                f"S3 head_object failed for {key}: {_sanitize_error_text(exc)}"
            ) from None
        return {
            "byte_size": int(response["ContentLength"]),
            "content_type": response.get("ContentType", ""),
            "sha256": response.get("Metadata", {}).get("sha256", ""),
        }

    def object_exists(self, key: str) -> bool:
        return self.head_object(key) is not None

    def upload_file(
        self, *, local_path: Path, key: str, content_type: str, sha256: str
    ) -> str:
        """Upload a file and verify it landed. Idempotent for identical content.

        The file's SHA-256 is stamped as object metadata. An existing object
        at the key is accepted only when both its size and its recorded
        checksum match the local file (multipart ETags are not content
        hashes, so metadata is the only reliable identity); anything else —
        wrong size, wrong hash, or an object some other writer created
        without our metadata — fails loudly rather than being reused or
        overwritten.
        """
        if not local_path.exists():
            raise StorageError(f"Cannot upload missing file: {local_path}")
        local_size = local_path.stat().st_size

        existing = self.head_object(key)
        if existing is not None:
            self._validate_existing_object(
                key=key,
                existing=existing,
                local_size=local_size,
                sha256=sha256,
            )
            return self.s3_uri(key)

        for attempt in range(1, _UPLOAD_ATTEMPTS + 1):
            try:
                # Each call creates a new boto3 transfer manager, so a retry
                # starts a complete, fresh multipart transfer while boto3
                # remains responsible for creation and abort cleanup.
                self._client.upload_file(
                    str(local_path),
                    self._config.bucket,
                    key,
                    ExtraArgs={
                        "ContentType": content_type,
                        "Metadata": {"sha256": sha256},
                    },
                )
                break
            except _S3_ERRORS as exc:
                if not _is_retryable_upload_error(exc):
                    raise StorageError(
                        f"S3 upload failed for {key}: {_sanitize_error_text(exc)}"
                    ) from None

                # The transfer may have completed even when its response was
                # lost. Reconcile before deciding whether another upload is
                # safe or necessary.
                recovered = self.head_object(key)
                if recovered is not None:
                    self._validate_existing_object(
                        key=key,
                        existing=recovered,
                        local_size=local_size,
                        sha256=sha256,
                    )
                    return self.s3_uri(key)

                if attempt == _UPLOAD_ATTEMPTS:
                    raise StorageError(
                        f"S3 upload failed for {key} after {_UPLOAD_ATTEMPTS} attempts: "
                        f"{_sanitize_error_text(exc)}"
                    ) from None

                delay = _upload_retry_delay(attempt)
                logger.warning(
                    "Transient S3 upload failure for %s on attempt %d/%d; "
                    "retrying the full transfer in %.2fs",
                    key,
                    attempt,
                    _UPLOAD_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)

        uploaded = self.head_object(key)
        if uploaded is None:
            raise StorageError(
                f"S3 upload verification failed for {key}: expected {local_size} bytes "
                f"with checksum {sha256}, found no object"
            )
        if uploaded["byte_size"] != local_size or uploaded["sha256"] != sha256:
            raise StorageError(
                f"S3 upload verification failed for {key}: expected {local_size} bytes "
                f"with checksum {sha256}, found {uploaded['byte_size']} bytes with "
                f"checksum {uploaded['sha256'] or 'missing'}"
            )
        return self.s3_uri(key)

    @staticmethod
    def _validate_existing_object(
        *, key: str, existing: Dict, local_size: int, sha256: str
    ) -> None:
        if existing["byte_size"] != local_size:
            raise StorageError(
                f"S3 object {key} already exists with size {existing['byte_size']} "
                f"but local file is {local_size} bytes; refusing to overwrite"
            )
        if existing["sha256"] != sha256:
            raise StorageError(
                f"S3 object {key} already exists with checksum "
                f"{existing['sha256'] or 'missing'} but local file is {sha256}; "
                "refusing to reuse or overwrite it"
            )

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
        except _S3_ERRORS as exc:
            raise StorageError(
                f"S3 download failed for {key}: {_sanitize_error_text(exc)}"
            ) from None
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
        except _S3_ERRORS as exc:
            raise StorageError(
                f"S3 presign failed for {key}: {_sanitize_error_text(exc)}"
            ) from None

    def check_access(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._config.bucket)
        except _S3_ERRORS as exc:
            raise StorageError(
                f"S3 access check failed for bucket {self._config.bucket}: "
                f"{_sanitize_error_text(exc)}"
            ) from None


def storage_health(config: StorageConfig) -> str:
    """One-line storage status for `pod health`."""
    if not config.bucket.strip():
        return "not configured"
    try:
        MediaStorage(config).check_access()
    except StorageError as exc:
        return f"error: {exc}"
    suffix = "" if config.enabled else ", ingest paused"
    return f"ok (bucket {config.bucket}{suffix})"


def _client_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _client_error_status(exc: ClientError) -> Optional[int]:
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None
