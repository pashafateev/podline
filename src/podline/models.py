from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional


class SessionStatus(str, Enum):
    QUEUED = "queued"
    AUPHONIC_PREPARED = "auphonic_prepared"
    AUPHONIC_PREVIEWED = "auphonic_previewed"
    AUPHONIC_STARTED = "auphonic_started"
    WAITING_FOR_FINALIZE = "waiting_for_finalize"
    FINALIZE_STARTED = "finalize_started"
    READY_FOR_DRAFT = "ready_for_draft"
    DRAFT_CREATED = "draft_created"
    FAILED = "failed"


class ArtifactKind(str, Enum):
    RAW_TRACK = "raw_track"
    PRE_EDIT_MASTER = "pre_edit_master"
    PRE_EDIT_STEM = "pre_edit_stem"
    TRANSCRIPT = "transcript"
    FINAL_MASTER = "final_master"


@dataclass(frozen=True)
class MediaArtifactRecord:
    session_uid: str
    kind: str
    role: str
    filename: str
    content_type: str
    byte_size: int
    sha256: str
    duration_seconds: Optional[float]
    s3_uri: str
    source_provider: Optional[str]
    source_id: Optional[str]
    created_at: str
    updated_at: str
    part_id: Optional[str] = None


@dataclass(frozen=True)
class RecordingPartRecord:
    part_id: str
    session_uid: str
    part_number: int
    take_id: Optional[str]
    status: str
    auphonic_production_id: Optional[str]
    ingest_complete: bool
    source_snapshot: dict
    timing_warnings: list
    started_at: Optional[str]
    stopped_at: Optional[str]
    take_status: Optional[str]
    output_basename: Optional[str]
    operation: Optional[str]
    failure_reason: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RuntimePaths:
    home: str
    config_file: str
    state_dir: str
    db_file: str
    health_file: str
    logs_dir: str
    staging_dir: str
    cache_dir: str
    workspaces_dir: str


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
    transcript_refresh_production_id: Optional[str]
    pre_edit_master_path: Optional[str]
    transcript_path: Optional[str]
    final_master_path: Optional[str]
    cozytrack_session_id: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class EpisodeRecord:
    session_uid: str
    episode_number: Optional[int]
    podbean_post_id: Optional[str]
    draft_url: Optional[str]
    title: Optional[str]
    description: Optional[str]


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
    activity_rms_db: Optional[float]


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
