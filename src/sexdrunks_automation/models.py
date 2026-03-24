from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional


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
    source_basename: Optional[str]
    source_date_folder: Optional[str]
    source_path: str
    recorded_at: Optional[str]
    recorded_at_source: Optional[str]
    fingerprint: str
    status: str
    failure_reason: Optional[str]
    failed_at_stage: Optional[str]
    auphonic_production_id: Optional[str]
    pre_edit_master_path: Optional[str]
    transcript_path: Optional[str]
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


@dataclass(frozen=True)
class TrackFile:
    track_name: str
    track_number: Optional[int]
    role: Optional[str]
    path: Path
    file_size: int
    has_audio: bool


@dataclass(frozen=True)
class AssetRecord:
    role: str
    filename: str
    file_size: int
    local_path: str


@dataclass(frozen=True)
class ScanCandidate:
    source_basename: str
    session_path: Path
    session_folder_name: str
    date_folder_name: str
    recorded_at: Optional[str]
    recorded_at_source: Optional[str]
    spoken_tracks: List[TrackFile]
    mix_track: Optional[TrackFile]


@dataclass(frozen=True)
class UnsupportedSession:
    session_path: Path
    reason: str
    spoken_track_count: int


@dataclass(frozen=True)
class ScanResult:
    candidates: List[ScanCandidate]
    unsupported_sessions: List[UnsupportedSession]


@dataclass(frozen=True)
class ScanOutcome:
    discovered: int
    created: int
    duplicates: int
    unsupported: List[UnsupportedSession]
