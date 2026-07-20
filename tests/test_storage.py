from pathlib import Path

import boto3
import pytest
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


def test_validate_storage_config_requires_enabled_and_bucket() -> None:
    with pytest.raises(StorageError):
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
        local_path=source, key=key, content_type="audio/webm"
    )
    second_uri = storage.upload_file(
        local_path=source, key=key, content_type="audio/webm"
    )

    assert first_uri == second_uri == f"s3://{BUCKET}/{key}"
    head = storage.head_object(key)
    assert head is not None
    assert head["byte_size"] == len(b"opus-bytes")
    assert head["content_type"] == "audio/webm"


@mock_aws
def test_upload_file_refuses_to_overwrite_different_content(tmp_path: Path) -> None:
    storage = _make_storage()
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    key = "sessions/s1/raw_track/aaa/track.webm"
    storage.upload_file(local_path=source, key=key, content_type="audio/webm")

    changed = tmp_path / "changed.webm"
    changed.write_bytes(b"different-and-longer-bytes")
    with pytest.raises(StorageError, match="refusing to overwrite"):
        storage.upload_file(local_path=changed, key=key, content_type="audio/webm")


@mock_aws
def test_upload_file_rejects_missing_source(tmp_path: Path) -> None:
    storage = _make_storage()
    with pytest.raises(StorageError, match="missing file"):
        storage.upload_file(
            local_path=tmp_path / "nope.webm",
            key="sessions/s1/raw_track/aaa/nope.webm",
            content_type="audio/webm",
        )


@mock_aws
def test_download_to_path_verifies_checksum(tmp_path: Path) -> None:
    storage = _make_storage()
    source = tmp_path / "track.webm"
    source.write_bytes(b"opus-bytes")
    expected_sha = sha256_of_file(source)
    key = "sessions/s1/raw_track/aaa/track.webm"
    storage.upload_file(local_path=source, key=key, content_type="audio/webm")

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
    storage.upload_file(local_path=source, key=key, content_type="audio/webm")

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
    storage.upload_file(local_path=source, key=key, content_type="audio/webm")

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
    storage.upload_file(local_path=source, key=key, content_type="audio/webm")

    url = storage.presigned_get_url(key=key, expires_seconds=600)

    assert url.startswith("https://")
    assert "?" in url
    assert "Signature" in url or "X-Amz-Signature" in url
    assert redact_url(url).endswith("?<redacted>")


def test_storage_health_wraps_client_construction_failures(monkeypatch) -> None:
    from botocore.exceptions import BotoCoreError

    def _broken_client(*_args, **_kwargs):
        raise BotoCoreError()

    monkeypatch.setattr("podline.storage.boto3.client", _broken_client)
    status = storage_health(_storage_config())
    assert status.startswith("error: Could not create S3 client")


@mock_aws
def test_storage_health_reports_ok_and_errors() -> None:
    assert (
        storage_health(
            StorageConfig(enabled=False, bucket="", region="", key_prefix="")
        )
        == "disabled"
    )

    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    assert storage_health(_storage_config()).startswith("ok")
    assert storage_health(_storage_config(bucket="missing-bucket")).startswith(
        "error"
    )
