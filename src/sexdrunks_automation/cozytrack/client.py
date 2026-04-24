from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

import httpx

from sexdrunks_automation.config import AppConfig


class CozytrackError(RuntimeError):
    pass


@dataclass(frozen=True)
class CozytrackTrack:
    id: str
    participant_name: Optional[str]
    status: Optional[str]
    created_at: Optional[str]


@dataclass(frozen=True)
class CozytrackSession:
    id: str
    name: Optional[str]
    status: Optional[str]
    finalized_at: Optional[str]
    tracks: List[CozytrackTrack]


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
    return CozytrackSession(
        id=str(payload["id"]),
        name=_optional_str(payload.get("name")),
        status=_optional_str(payload.get("status")),
        finalized_at=_optional_str(payload.get("finalizedAt")),
        tracks=tracks,
    )


def _parse_track(payload: Dict[str, Any]) -> CozytrackTrack:
    if "id" not in payload:
        raise CozytrackError(f"Cozytrack track payload missing 'id': {payload!r:.300}")
    return CozytrackTrack(
        id=str(payload["id"]),
        participant_name=_optional_str(payload.get("participantName")),
        status=_optional_str(payload.get("status")),
        created_at=_optional_str(payload.get("createdAt")),
    )


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)
