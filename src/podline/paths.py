from pathlib import Path

from podline.config import AppConfig
from podline.models import RuntimePaths


def build_runtime_paths(config: AppConfig) -> RuntimePaths:
    home = config.runtime_home
    return RuntimePaths(
        home=str(home),
        config_file=str(config.config_file),
        state_dir=str(home / "state"),
        db_file=str(home / "state" / "state.db"),
        health_file=str(home / "state" / "health.json"),
        logs_dir=str(home / "logs"),
        staging_dir=str(home / "staging"),
        cache_dir=str(home / "cache"),
    )


def ensure_runtime_dirs(runtime_paths: RuntimePaths) -> None:
    for path in (
        runtime_paths.home,
        runtime_paths.state_dir,
        runtime_paths.logs_dir,
        runtime_paths.staging_dir,
        runtime_paths.cache_dir,
    ):
        Path(path).mkdir(parents=True, exist_ok=True)
