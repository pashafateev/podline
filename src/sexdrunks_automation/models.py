from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SessionStatus(str, Enum):
    QUEUED = "queued"
    AUPHONIC_STARTED = "auphonic_started"
    WAITING_FOR_FINALIZE = "waiting_for_finalize"
    FINALIZE_STARTED = "finalize_started"
    DRAFT_CREATED = "draft_created"
    FAILED = "failed"


@dataclass(frozen=True)
class RuntimePaths:
    home: str
    config_file: str
    state_dir: str
    db_file: str
    health_file: str
    logs_dir: str
    staging_dir: str


@dataclass(frozen=True)
class SessionRecord:
    session_uid: str
    recorder_name: Optional[str]
    source_path: str
    fingerprint: str
    status: str
    failure_reason: Optional[str]
    failed_at_stage: Optional[str]
    auphonic_production_id: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class HealthSnapshot:
    worker_status: str
    last_heartbeat: Optional[str]
    external_drive_mounted: bool
    queue_depth: int
    session_count: int
    last_error: Optional[str]
