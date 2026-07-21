import logging
from pathlib import Path

import boto3
import pytest
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError, ConnectionClosedError, EndpointConnectionError
from moto import mock_aws

from podline.config import StorageConfig
from podline.storage import (
    MediaStorage,
    StorageError,
    build_session_key,
    parse_s3_uri,
    redact_url,
    sha256_of_file,
    storage_health,
    validate_storage_config,
)

BUCKET = "test-podline-media"


@pytest.fixture(autouse=True)
def aws_test_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def _storage_config(bucket: str = BUCKET) -> StorageConfig:
    return StorageConfig(enabled=True, bucket=bucket, region="us-east-1", key_prefix="")


def _make_storage() -> MediaStorage:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    return MediaStorage(_storage_config())


def _wrapped_upload_error(*, code: str, status: int) -> S3UploadFailedError:
    client_error = ClientError(
        {
            "Error": {"Code": code, "Message": f"{code} during multipart upload"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "UploadPart",
    )
    wrapped = S3UploadFailedError(f"multipart transfer failed: {client_error}")
    wrapped.__cause__ = client_error
    return wrapped


def test_build_session_key_shapes_content_addressed_paths() -> None:
    key = build_session_key(
        key_prefix="",
        session_uid="abc123",
        kind="raw_track",
        sha256="deadbeef",
        filename="Tr1.webm",
    )
    assert key == "sessions/abc123/raw_track/deadbeef/Tr1.webm"

    prefixed = build_session_key(
        key_prefix="podline/",
        session_uid="abc123",
        kind="raw_track",
        sha256="deadbeef",
        filename="Tr1.webm",
    )
    assert prefixed == "podline/sessions/abc123/raw_track/deadbeef/Tr1.webm"


def test_build_session_key_rejects_invalid_segments() -> None:
    with pytest.raises(StorageError):
        build_session_key(
            key_prefix="",
            session_uid="abc/123",
            kind="raw_track",
            sha256="deadbeef",
            filename="Tr1.webm",
        )
    with pytest.raises(StorageError):
        build_session_key(
            key_prefix="",
            session_uid="abc123",
            kind="raw_track",
            sha256="deadbeef",
            filename="",
        )


def test_parse_s3_uri_roundtrip() -> None:
    bucket, key = parse_s3_uri("s3://my-bucket/sessions/a/b/c.webm")
    assert bucket == "my-bucket"
    assert key == "sessions/a/b/c.webm"

    for invalid in ("https://example.com/x", "s3://bucket-only", "not-a-uri"):
        with pytest.raises(StorageError):
            parse_s3_uri(invalid)


def test_redact_url_strips_query_credentials() -> None:
    signed = "https://bucket.s3.amazonaws.com/k/file.webm?X-Amz-Signature=secret&X-Amz-Credential=AKIA"
    redacted = redact_url(signed)
    assert "secret" not in redacted
    assert "AKIA" not in redacted
    assert redacted == "https://bucket.s3.amazonaws.com/k/file.webm?<redacted>"
    assert redact_url("https://example.com/plain") == "https://example.com/plain"


def test_validate_storage_config_requires_bucket_but_not_enabled() -> None:
    # enabled=false pauses ingest only; a configured bucket stays usable for
    # sessions that are already S3-native.
    validate_storage_config(
        StorageConfig(enabled=False, bucket="b", region="", key_prefix="")
    )
    with pytest.raises(StorageError):
        validate_storage_config(
            StorageConfig(enabled=True, bucket="  ", region="", key_prefix="")
        )


@mock_aws
def test_upload_file_is_idempotent_for_same_content(tmp_path: Path) -> None:
    storage = _make_storage()
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    key = "sessions/s1/raw_track/aaa/track.webm"

    first_uri = storage.upload_file(
        local_path=source,
        key=key,
        content_type="audio/webm",
        sha256=sha256_of_file(source),
    )
    second_uri = storage.upload_file(
        local_path=source,
        key=key,
        content_type="audio/webm",
        sha256=sha256_of_file(source),
    )

    assert first_uri == second_uri == f"s3://{BUCKET}/{key}"
    head = storage.head_object(key)
    assert head is not None
    assert head["byte_size"] == len(b"opus-bytes")
    assert head["content_type"] == "audio/webm"
    assert head["sha256"] == sha256_of_file(source)


@mock_aws
def test_upload_file_refuses_to_overwrite_different_content(tmp_path: Path) -> None:
    storage = _make_storage()
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    key = "sessions/s1/raw_track/aaa/track.webm"
    storage.upload_file(
        local_path=source,
        key=key,
        content_type="audio/webm",
        sha256=sha256_of_file(source),
    )

    changed = tmp_path / "changed.webm"
    changed.write_bytes(b"different-and-longer-bytes")
    with pytest.raises(StorageError, match="refusing to overwrite"):
        storage.upload_file(
            local_path=changed,
            key=key,
            content_type="audio/webm",
            sha256=sha256_of_file(changed),
        )


@mock_aws
def test_upload_file_rejects_missing_source(tmp_path: Path) -> None:
    storage = _make_storage()
    with pytest.raises(StorageError, match="missing file"):
        storage.upload_file(
            local_path=tmp_path / "nope.webm",
            key="sessions/s1/raw_track/aaa/nope.webm",
            content_type="audio/webm",
            sha256="0" * 64,
        )


@mock_aws
def test_upload_file_refuses_same_size_object_with_different_content(
    tmp_path: Path,
) -> None:
    storage = _make_storage()
    good = tmp_path / "track.webm"
    good.write_bytes(b"opus-bytes")
    key = "sessions/s1/raw_track/aaa/track.webm"

    # A same-size object planted at our content-addressed key, without our
    # checksum metadata (e.g. a foreign writer or an interrupted migration).
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=BUCKET, Key=key, Body=b"evil-bytes"
    )
    assert len(b"evil-bytes") == len(b"opus-bytes")

    with pytest.raises(StorageError, match="refusing to reuse"):
        storage.upload_file(
            local_path=good,
            key=key,
            content_type="audio/webm",
            sha256=sha256_of_file(good),
        )

    # Same size and a checksum recorded by us, but for different content.
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=BUCKET,
        Key=key,
        Body=b"evil-bytes",
        Metadata={"sha256": "f" * 64},
    )
    with pytest.raises(StorageError, match="refusing to reuse"):
        storage.upload_file(
            local_path=good,
            key=key,
            content_type="audio/webm",
            sha256=sha256_of_file(good),
        )


@mock_aws
def test_upload_file_retries_connection_closed_then_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    storage = _make_storage()
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    key = "sessions/s1/raw_track/aaa/track.webm"
    original_upload = storage._client.upload_file
    attempts = []
    delays = []

    def _flaky_upload(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            raise ConnectionClosedError(endpoint_url="https://s3.test/upload")
        return original_upload(*args, **kwargs)

    monkeypatch.setattr(storage._client, "upload_file", _flaky_upload)
    monkeypatch.setattr("time.sleep", delays.append)

    uri = storage.upload_file(
        local_path=source,
        key=key,
        content_type="audio/webm",
        sha256=sha256_of_file(source),
    )

    assert uri == f"s3://{BUCKET}/{key}"
    assert len(attempts) == 2
    assert len(delays) == 1


@mock_aws
def test_upload_file_retries_wrapped_slowdown_then_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    storage = _make_storage()
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    key = "sessions/s1/raw_track/aaa/track.webm"
    original_upload = storage._client.upload_file
    attempts = []

    def _flaky_upload(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            raise _wrapped_upload_error(code="SlowDown", status=503)
        return original_upload(*args, **kwargs)

    monkeypatch.setattr(storage._client, "upload_file", _flaky_upload)
    monkeypatch.setattr("time.sleep", lambda _delay: None)

    uri = storage.upload_file(
        local_path=source,
        key=key,
        content_type="audio/webm",
        sha256=sha256_of_file(source),
    )

    assert uri == f"s3://{BUCKET}/{key}"
    assert len(attempts) == 2


@mock_aws
def test_upload_file_accepts_ambiguous_success_without_uploading_again(
    tmp_path: Path, monkeypatch
) -> None:
    storage = _make_storage()
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    key = "sessions/s1/raw_track/aaa/track.webm"
    original_upload = storage._client.upload_file
    attempts = []
    delays = []

    def _store_then_disconnect(*args, **kwargs):
        attempts.append(args)
        original_upload(*args, **kwargs)
        raise ConnectionClosedError(
            endpoint_url="https://s3.test/upload?uploadId=secret-id&partNumber=7"
        )

    monkeypatch.setattr(storage._client, "upload_file", _store_then_disconnect)
    monkeypatch.setattr("time.sleep", delays.append)

    uri = storage.upload_file(
        local_path=source,
        key=key,
        content_type="audio/webm",
        sha256=sha256_of_file(source),
    )

    assert uri == f"s3://{BUCKET}/{key}"
    assert len(attempts) == 1
    assert delays == []


@mock_aws
def test_upload_file_exhausts_transient_retries_as_storage_error(
    tmp_path: Path, monkeypatch
) -> None:
    storage = _make_storage()
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    attempts = []
    delays = []

    def _unavailable(*args, **kwargs):
        attempts.append(args)
        raise EndpointConnectionError(endpoint_url="https://s3.test/upload")

    monkeypatch.setattr(storage._client, "upload_file", _unavailable)
    monkeypatch.setattr("time.sleep", delays.append)

    with pytest.raises(StorageError, match="after 3 attempts"):
        storage.upload_file(
            local_path=source,
            key="sessions/s1/raw_track/aaa/track.webm",
            content_type="audio/webm",
            sha256=sha256_of_file(source),
        )

    assert len(attempts) == 3
    assert len(delays) == 2


@mock_aws
def test_upload_file_does_not_retry_permanent_access_denied(
    tmp_path: Path, monkeypatch
) -> None:
    storage = _make_storage()
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    attempts = []
    delays = []

    def _denied(*_args, **_kwargs):
        attempts.append(_args)
        raise _wrapped_upload_error(code="AccessDenied", status=403)

    monkeypatch.setattr(storage._client, "upload_file", _denied)
    monkeypatch.setattr("time.sleep", delays.append)
    with pytest.raises(StorageError, match="S3 upload failed"):
        storage.upload_file(
            local_path=source,
            key="sessions/s1/raw_track/aaa/track.webm",
            content_type="audio/webm",
            sha256=sha256_of_file(source),
        )

    assert len(attempts) == 1
    assert delays == []


@mock_aws
def test_upload_file_redacts_endpoint_queries_from_logs_and_final_error(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    storage = _make_storage()
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    endpoint = (
        "https://bucket.s3.test/track.webm"
        "?uploadId=secret-multipart-id&partNumber=42&X-Amz-Signature=secret-signature"
    )

    def _disconnect(*_args, **_kwargs):
        raise ConnectionClosedError(endpoint_url=endpoint)

    monkeypatch.setattr(storage._client, "upload_file", _disconnect)
    monkeypatch.setattr("time.sleep", lambda _delay: None)
    caplog.set_level(logging.WARNING, logger="podline")

    with pytest.raises(StorageError) as exc_info:
        storage.upload_file(
            local_path=source,
            key="sessions/s1/raw_track/aaa/track.webm",
            content_type="audio/webm",
            sha256=sha256_of_file(source),
        )

    visible_text = f"{caplog.text}\n{exc_info.value}"
    assert "uploadId" not in visible_text
    assert "partNumber" not in visible_text
    assert "secret-multipart-id" not in visible_text
    assert "secret-signature" not in visible_text
    assert "https://bucket.s3.test/track.webm?<redacted>" in str(exc_info.value)


@mock_aws
def test_download_to_path_wraps_transfer_errors(tmp_path: Path, monkeypatch) -> None:
    from boto3.exceptions import S3TransferFailedError

    storage = _make_storage()

    def _denied(*_args, **_kwargs):
        raise S3TransferFailedError("AccessDenied while downloading")

    monkeypatch.setattr(storage._client, "download_file", _denied)
    with pytest.raises(StorageError, match="S3 download failed"):
        storage.download_to_path(
            key="sessions/s1/raw_track/aaa/track.webm",
            destination=tmp_path / "downloads" / "track.webm",
            expected_sha256="0" * 64,
        )


@mock_aws
def test_download_to_path_verifies_checksum(tmp_path: Path) -> None:
    storage = _make_storage()
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    expected_sha = sha256_of_file(source)
    key = "sessions/s1/raw_track/aaa/track.webm"
    storage.upload_file(
        local_path=source,
        key=key,
        content_type="audio/webm",
        sha256=sha256_of_file(source),
    )

    destination = tmp_path / "downloads" / "track.webm"
    result = storage.download_to_path(
        key=key,
        destination=destination,
        expected_sha256=expected_sha,
        expected_size=len(b"opus-bytes"),
    )

    assert result == destination
    assert destination.read_bytes() == b"opus-bytes"


@mock_aws
def test_download_to_path_rejects_corrupt_object_without_leaving_files(
    tmp_path: Path,
) -> None:
    storage = _make_storage()
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    expected_sha = sha256_of_file(source)
    key = "sessions/s1/raw_track/aaa/track.webm"
    storage.upload_file(
        local_path=source,
        key=key,
        content_type="audio/webm",
        sha256=sha256_of_file(source),
    )

    # Corrupt the stored object while keeping the original expectations.
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=BUCKET, Key=key, Body=b"corrupted!"
    )

    destination = tmp_path / "downloads" / "track.webm"
    with pytest.raises(StorageError, match="checksum mismatch"):
        storage.download_to_path(
            key=key,
            destination=destination,
            expected_sha256=expected_sha,
        )

    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()


@mock_aws
def test_download_to_path_rejects_size_mismatch(tmp_path: Path) -> None:
    storage = _make_storage()
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    key = "sessions/s1/raw_track/aaa/track.webm"
    storage.upload_file(
        local_path=source,
        key=key,
        content_type="audio/webm",
        sha256=sha256_of_file(source),
    )

    destination = tmp_path / "downloads" / "track.webm"
    with pytest.raises(StorageError, match="size mismatch"):
        storage.download_to_path(
            key=key,
            destination=destination,
            expected_sha256=sha256_of_file(source),
            expected_size=len(b"opus-bytes") + 5,
        )
    assert not destination.exists()


@mock_aws
def test_presigned_get_url_is_signed_and_redactable(tmp_path: Path) -> None:
    storage = _make_storage()
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    key = "sessions/s1/raw_track/aaa/track.webm"
    storage.upload_file(
        local_path=source,
        key=key,
        content_type="audio/webm",
        sha256=sha256_of_file(source),
    )

    url = storage.presigned_get_url(key=key, expires_seconds=600)

    assert url.startswith("https://")
    assert "?" in url
    assert "Signature" in url or "X-Amz-Signature" in url
    assert redact_url(url).endswith("?<redacted>")


def test_media_storage_wraps_missing_profile_in_storage_error() -> None:
    config = StorageConfig(
        enabled=True,
        bucket=BUCKET,
        region="us-east-1",
        key_prefix="",
        profile="podline-test-profile-that-does-not-exist-7f3a",
    )
    with pytest.raises(StorageError, match="Could not create S3 client"):
        MediaStorage(config)


def test_storage_health_wraps_client_construction_failures(monkeypatch) -> None:
    from botocore.exceptions import BotoCoreError

    def _broken_session(*_args, **_kwargs):
        raise BotoCoreError()

    monkeypatch.setattr("podline.storage.boto3.session.Session", _broken_session)
    status = storage_health(_storage_config())
    assert status.startswith("error: Could not create S3 client")


@mock_aws
def test_storage_health_reports_ok_and_errors() -> None:
    assert (
        storage_health(
            StorageConfig(enabled=False, bucket="", region="", key_prefix="")
        )
        == "not configured"
    )

    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    assert storage_health(_storage_config()).startswith("ok")
    assert storage_health(_storage_config(bucket="missing-bucket")).startswith(
        "error"
    )

    paused = StorageConfig(
        enabled=False, bucket=BUCKET, region="us-east-1", key_prefix=""
    )
    assert storage_health(paused) == f"ok (bucket {BUCKET}, ingest paused)"


@mock_aws
def test_media_storage_stays_usable_while_ingest_is_paused(tmp_path: Path) -> None:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    paused = MediaStorage(
        StorageConfig(enabled=False, bucket=BUCKET, region="us-east-1", key_prefix="")
    )
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    key = "sessions/s1/raw_track/aaa/track.webm"

    paused.upload_file(
        local_path=source,
        key=key,
        content_type="audio/webm",
        sha256=sha256_of_file(source),
    )
    destination = tmp_path / "downloads" / "track.webm"
    paused.download_to_path(
        key=key,
        destination=destination,
        expected_sha256=sha256_of_file(source),
    )
    assert destination.read_bytes() == b"opus-bytes"
