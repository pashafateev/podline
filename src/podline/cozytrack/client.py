from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

import httpx

from podline.config import AppConfig


class CozytrackError(RuntimeError):
    pass


_HOST_PARTICIPANT_ID = "host"
_HOST_LOCAL_PARTICIPANT_PREFIX = "host-local-ch-"


def is_host_participant(participant_id: str) -> bool:
    """Return whether a stable Cozytrack participant id is host-local."""
    normalized = participant_id.strip()
    return normalized == _HOST_PARTICIPANT_ID or normalized.startswith(
        _HOST_LOCAL_PARTICIPANT_PREFIX
    )


def host_channel_number(participant_id: str) -> Optional[int]:
    """Host-local channel number for a stable Cozytrack participant id.

    The plain host id maps to channel 1. Returns None for remote
    participants and for host-local ids without a positive integer
    channel suffix.
    """
    normalized = participant_id.strip()
    if normalized == _HOST_PARTICIPANT_ID:
        return 1
    if normalized.startswith(_HOST_LOCAL_PARTICIPANT_PREFIX):
        suffix = normalized[len(_HOST_LOCAL_PARTICIPANT_PREFIX) :]
        if suffix.isdigit() and int(suffix) > 0:
            return int(suffix)
    return None


@dataclass(frozen=True)
class CozytrackTrack:
    id: str
    participant_name: Optional[str]
    status: Optional[str]
    created_at: Optional[str]
    participant_id: Optional[str] = None
    duration_ms: Optional[int] = None
    take_id: Optional[str] = None
    partial: Optional[bool] = None
    segment_count: Optional[int] = None
    incomplete_segment_count: Optional[int] = None
    updated_at: Optional[str] = None
    s3_purged_at: Optional[str] = None


@dataclass(frozen=True)
class CozytrackTake:
    id: str
    started_at: Optional[str]
    stopped_at: Optional[str]
    status: Optional[str]


@dataclass(frozen=True)
class CozytrackSession:
    id: str
    name: Optional[str]
    status: Optional[str]
    finalized_at: Optional[str]
    tracks: List[CozytrackTrack]
    recording_takes: Optional[List[CozytrackTake]] = None


class CozytrackClientProtocol(Protocol):
    def list_ready_sessions(self) -> List[CozytrackSession]: ...

    def get_session(self, session_id: str) -> CozytrackSession: ...

    def get_track_download_url(self, track_id: str) -> str: ...

    def download_to_file(self, url: str, destination: Path) -> None: ...


class CozytrackClient:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        if not config.cozytrack.api_key.strip():
            raise CozytrackError(
                "Cozytrack api_key is missing in config (set cozytrack.api_key in config.local.yaml)"
            )
        self._client = httpx.Client(
            base_url=config.cozytrack.base_url.rstrip("/"),
            headers={"X-API-Key": config.cozytrack.api_key},
            timeout=httpx.Timeout(
                config.cozytrack.request_timeout_seconds, connect=30.0
            ),
        )

    def list_ready_sessions(self) -> List[CozytrackSession]:
        payload = self._get_json("/api/ingest/sessions", params={"status": "ready"})
        if not isinstance(payload, list):
            raise CozytrackError(
                f"Cozytrack /api/ingest/sessions returned non-list payload: {payload!r:.300}"
            )
        return [_parse_session(item) for item in payload]

    def get_session(self, session_id: str) -> CozytrackSession:
        payload = self._get_json(f"/api/ingest/sessions/{session_id}")
        if not isinstance(payload, dict):
            raise CozytrackError(
                f"Cozytrack /api/ingest/sessions/{session_id} returned non-object payload"
            )
        return _parse_session(payload)

    def get_track_download_url(self, track_id: str) -> str:
        payload = self._get_json(f"/api/ingest/tracks/{track_id}/download")
        if not isinstance(payload, dict) or "url" not in payload:
            raise CozytrackError(
                f"Cozytrack download response for track {track_id} missing 'url'"
            )
        return str(payload["url"])

    def download_to_file(self, url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Presigned S3 URLs must not carry our API key header.
        with httpx.Client(
            timeout=httpx.Timeout(
                self._config.cozytrack.request_timeout_seconds, connect=30.0
            )
        ) as anonymous:
            with anonymous.stream("GET", url, follow_redirects=True) as response:
                if response.status_code >= 400:
                    body = response.read().decode("utf-8", errors="replace")[:500]
                    raise CozytrackError(
                        f"Cozytrack S3 download failed with status {response.status_code}: {body}"
                    )
                with destination.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)

    def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise CozytrackError(f"Cozytrack request to {path} failed: {exc}") from exc

        if response.status_code >= 400:
            body_preview = response.text[:500]
            raise CozytrackError(
                f"Cozytrack {path} returned status {response.status_code}: {body_preview}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise CozytrackError(
                f"Cozytrack {path} returned non-JSON response: {response.text[:300]}"
            ) from exc


def _parse_session(payload: Dict[str, Any]) -> CozytrackSession:
    if "id" not in payload:
        raise CozytrackError(f"Cozytrack session payload missing 'id': {payload!r:.300}")
    raw_tracks = payload.get("tracks") or []
    tracks = [_parse_track(item) for item in raw_tracks if isinstance(item, dict)]
    raw_takes = payload.get("recordingTakes")
    takes = None
    if raw_takes is not None:
        if not isinstance(raw_takes, list):
            raise CozytrackError("Cozytrack recordingTakes must be an array")
        takes = []
        for item in raw_takes:
            if not isinstance(item, dict) or not item.get("id"):
                raise CozytrackError("Cozytrack take payload is missing id")
            takes.append(CozytrackTake(
                id=str(item["id"]),
                started_at=_optional_str(item.get("startedAt")),
                stopped_at=_optional_str(item.get("stoppedAt")),
                status=_optional_str(item.get("status")),
            ))
    return CozytrackSession(
        id=str(payload["id"]),
        name=_optional_str(payload.get("name")),
        status=_optional_str(payload.get("status")),
        finalized_at=_optional_str(payload.get("finalizedAt")),
        tracks=tracks,
        recording_takes=takes,
    )


def _parse_track(payload: Dict[str, Any]) -> CozytrackTrack:
    if "id" not in payload:
        raise CozytrackError(f"Cozytrack track payload missing 'id': {payload!r:.300}")
    return CozytrackTrack(
        id=str(payload["id"]),
        participant_name=_optional_str(payload.get("participantName")),
        status=_optional_str(payload.get("status")),
        created_at=_optional_str(payload.get("createdAt")),
        participant_id=_optional_str(payload.get("participantId")),
        duration_ms=_optional_int(payload.get("durationMs")),
        take_id=_optional_str(payload.get("takeId")),
        partial=_optional_bool(payload.get("partial")),
        segment_count=_optional_count(payload.get("segmentCount")),
        incomplete_segment_count=_optional_count(payload.get("incompleteSegmentCount")),
        updated_at=_optional_str(payload.get("updatedAt")),
        s3_purged_at=_optional_str(payload.get("s3PurgedAt")),
    )


def _optional_bool(value: Any) -> Optional[bool]:
    if value is None or isinstance(value, bool):
        return value
    raise CozytrackError(f"Expected boolean value, received {value!r}")


def _optional_count(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CozytrackError(f"Expected nonnegative segment count, received {value!r}")
    return value


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CozytrackError(f"Expected integer value, received {value!r}") from exc
