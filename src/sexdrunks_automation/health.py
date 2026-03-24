import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sexdrunks_automation.config import AppConfig
from sexdrunks_automation.models import HealthSnapshot, RuntimePaths, SessionStatus
from sexdrunks_automation.repository import count_sessions, count_sessions_by_status


def write_health_snapshot(
    runtime_paths: RuntimePaths,
    config: AppConfig,
    worker_status: str = "stopped",
    last_error: Optional[str] = None,
) -> HealthSnapshot:
    snapshot = build_health_snapshot(runtime_paths, config, worker_status, last_error)
    payload = {
        "worker_status": snapshot.worker_status,
        "last_heartbeat": snapshot.last_heartbeat,
        "external_drive_mounted": snapshot.external_drive_mounted,
        "queue_depth": snapshot.queue_depth,
        "session_count": snapshot.session_count,
        "last_error": snapshot.last_error,
    }
    Path(runtime_paths.health_file).write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    return snapshot


def read_health_snapshot(
    runtime_paths: RuntimePaths, config: AppConfig
) -> HealthSnapshot:
    health_file = Path(runtime_paths.health_file)
    if not health_file.exists():
        return build_health_snapshot(runtime_paths, config, worker_status="stopped")

    data = json.loads(health_file.read_text(encoding="utf-8"))
    return HealthSnapshot(
        worker_status=data.get("worker_status", "stopped"),
        last_heartbeat=data.get("last_heartbeat"),
        external_drive_mounted=bool(data.get("external_drive_mounted", False)),
        queue_depth=int(data.get("queue_depth", 0)),
        session_count=int(data.get("session_count", 0)),
        last_error=data.get("last_error"),
    )


def build_health_snapshot(
    runtime_paths: RuntimePaths,
    config: AppConfig,
    worker_status: str,
    last_error: Optional[str] = None,
) -> HealthSnapshot:
    last_heartbeat = datetime.now(timezone.utc).isoformat()
    external_drive_mounted = config.paths.deliverables_root.exists()
    queue_depth = count_sessions_by_status(
        runtime_paths.db_file, SessionStatus.QUEUED.value
    )
    session_count = count_sessions(runtime_paths.db_file)
    return HealthSnapshot(
        worker_status=worker_status,
        last_heartbeat=last_heartbeat,
        external_drive_mounted=external_drive_mounted,
        queue_depth=queue_depth,
        session_count=session_count,
        last_error=last_error,
    )
