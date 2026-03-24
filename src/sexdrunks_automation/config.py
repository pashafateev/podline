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
    poll_interval_seconds: int
    max_poll_attempts: int
    preset_ids: Dict[str, str]


@dataclass(frozen=True)
class PodbeanConfig:
    enabled: bool
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class AppConfig:
    runtime_home: Path
    paths: PathsConfig
    polling: PollingConfig
    notifications: NotificationsConfig
    auphonic: AuphonicConfig
    podbean: PodbeanConfig
    config_file: Path


def load_config(config_path: Path) -> AppConfig:
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

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
        ),
        config_file=config_path,
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
