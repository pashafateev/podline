from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import yaml


@dataclass(frozen=True)
class PathsConfig:
    raw_root: Path
    deliverables_root: Path


@dataclass(frozen=True)
class PollingConfig:
    interval_seconds: int


@dataclass(frozen=True)
class NotificationsConfig:
    enabled: bool


@dataclass(frozen=True)
class AuphonicConfig:
    enabled: bool
    api_key: str
    base_url: str
    request_timeout_seconds: int
    poll_interval_seconds: int
    max_poll_attempts: int
    preset_ids: Dict[str, str]


@dataclass(frozen=True)
class PodbeanConfig:
    enabled: bool
    client_id: str
    client_secret: str
    base_url: str
    podcast_id: str


@dataclass(frozen=True)
class CozytrackConfig:
    base_url: str
    api_key: str
    download_subdir: str
    request_timeout_seconds: int


@dataclass(frozen=True)
class AppConfig:
    runtime_home: Path
    paths: PathsConfig
    polling: PollingConfig
    notifications: NotificationsConfig
    auphonic: AuphonicConfig
    podbean: PodbeanConfig
    cozytrack: CozytrackConfig
    config_file: Path


def load_config(config_path: Path) -> AppConfig:
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    local_config_path = config_path.with_name("config.local.yaml")
    if local_config_path.exists():
        with local_config_path.open("r", encoding="utf-8") as handle:
            local_data = yaml.safe_load(handle) or {}
        data = _deep_merge(data, local_data)

    return AppConfig(
        runtime_home=Path(_require(data, "runtime_home")),
        paths=PathsConfig(
            raw_root=Path(_require_nested(data, "paths", "raw_root")),
            deliverables_root=Path(_require_nested(data, "paths", "deliverables_root")),
        ),
        polling=PollingConfig(
            interval_seconds=int(_require_nested(data, "polling", "interval_seconds")),
        ),
        notifications=NotificationsConfig(
            enabled=bool(_require_nested(data, "notifications", "enabled")),
        ),
        auphonic=AuphonicConfig(
            enabled=bool(_require_nested(data, "auphonic", "enabled")),
            api_key=str(_require_nested(data, "auphonic", "api_key")),
            base_url=str(_require_nested(data, "auphonic", "base_url")),
            request_timeout_seconds=int(
                _require_nested(data, "auphonic", "request_timeout_seconds")
            ),
            poll_interval_seconds=int(
                _require_nested(data, "auphonic", "poll_interval_seconds")
            ),
            max_poll_attempts=int(
                _require_nested(data, "auphonic", "max_poll_attempts")
            ),
            preset_ids=dict(_require_nested(data, "auphonic", "preset_ids")),
        ),
        podbean=PodbeanConfig(
            enabled=bool(_require_nested(data, "podbean", "enabled")),
            client_id=str(_require_nested(data, "podbean", "client_id")),
            client_secret=str(_require_nested(data, "podbean", "client_secret")),
            base_url=str(_require_nested(data, "podbean", "base_url")),
            podcast_id=str(_require_nested(data, "podbean", "podcast_id")),
        ),
        cozytrack=_load_cozytrack_config(data),
        config_file=config_path,
    )


def _load_cozytrack_config(data: Dict[str, Any]) -> CozytrackConfig:
    section = data.get("cozytrack") or {}
    if not isinstance(section, dict):
        section = {}
    download_subdir = str(section.get("download_subdir", "cozytrack"))
    _validate_download_subdir(download_subdir)
    return CozytrackConfig(
        base_url=str(section.get("base_url", "http://localhost:3000")),
        api_key=str(section.get("api_key", "")),
        download_subdir=download_subdir,
        request_timeout_seconds=int(section.get("request_timeout_seconds", 600)),
    )


def _validate_download_subdir(value: str) -> None:
    stripped = value.strip()
    if not stripped:
        return
    candidate = Path(stripped)
    if candidate.is_absolute():
        raise ValueError(
            f"cozytrack.download_subdir must be a relative path, got absolute: {value!r}"
        )
    if any(part == ".." for part in candidate.parts):
        raise ValueError(
            f"cozytrack.download_subdir must not contain '..' segments: {value!r}"
        )


def _require(data: Dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ValueError(f"Missing required config key: {key}")
    return data[key]


def _require_nested(data: Dict[str, Any], section: str, key: str) -> Any:
    section_data = data.get(section)
    if not isinstance(section_data, dict):
        raise ValueError(f"Missing required config section: {section}")
    if key not in section_data:
        raise ValueError(f"Missing required config key: {section}.{key}")
    return section_data[key]


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
