import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from sexdrunks_automation.models import (
    ScanCandidate,
    ScanResult,
    TrackFile,
    UnsupportedSession,
)

TRACK_PATTERN = re.compile(r"(?P<basename>.+)_Tr(?P<track>LR|\d+)\.(?P<ext>wav|WAV)$")
ROLE_MAP = {
    1: "host1",
    2: "host2",
    3: "guest1",
    4: "guest2",
}


def scan_raw_sessions(raw_root: Path) -> ScanResult:
    candidates: List[ScanCandidate] = []
    unsupported_sessions: List[UnsupportedSession] = []

    if not raw_root.exists():
        return ScanResult(
            candidates=candidates, unsupported_sessions=unsupported_sessions
        )

    for date_dir in sorted(path for path in raw_root.iterdir() if path.is_dir()):
        for session_dir in sorted(path for path in date_dir.iterdir() if path.is_dir()):
            parsed = _parse_session_dir(date_dir.name, session_dir)
            if isinstance(parsed, UnsupportedSession):
                unsupported_sessions.append(parsed)
            elif parsed is not None:
                candidates.append(parsed)

    return ScanResult(candidates=candidates, unsupported_sessions=unsupported_sessions)


def _parse_session_dir(date_folder_name: str, session_dir: Path):
    track_files: List[TrackFile] = []
    for entry in sorted(path for path in session_dir.iterdir() if path.is_file()):
        parsed_track = _parse_track_file(entry)
        if parsed_track is not None:
            track_files.append(parsed_track)

    if not track_files:
        return None

    recorder_names = {
        path.name.split("_Tr", 1)[0] for path in (track.path for track in track_files)
    }
    source_basename = sorted(recorder_names)[0]
    spoken_tracks = sorted(
        (track for track in track_files if track.track_number is not None),
        key=lambda item: item.track_number,
    )
    mix_track = next(
        (track for track in track_files if track.track_name == "TrLR"), None
    )
    spoken_track_count = len(spoken_tracks)

    if spoken_track_count < 2:
        return UnsupportedSession(
            session_path=session_dir,
            reason="expected 2 to 4 spoken tracks, found fewer than 2",
            spoken_track_count=spoken_track_count,
        )

    if spoken_track_count > 4:
        return UnsupportedSession(
            session_path=session_dir,
            reason="expected 2 to 4 spoken tracks, found more than 4",
            spoken_track_count=spoken_track_count,
        )

    recorded_at, recorded_at_source = _infer_recorded_at(
        spoken_tracks=spoken_tracks,
        mix_track=mix_track,
        session_dir=session_dir,
    )

    return ScanCandidate(
        source_basename=source_basename,
        session_path=session_dir,
        session_folder_name=session_dir.name,
        date_folder_name=date_folder_name,
        recorded_at=recorded_at,
        recorded_at_source=recorded_at_source,
        spoken_tracks=spoken_tracks,
        mix_track=mix_track,
    )


def _parse_track_file(path: Path) -> Optional[TrackFile]:
    match = TRACK_PATTERN.match(path.name)
    if match is None:
        return None

    track = match.group("track")
    if track == "LR":
        return TrackFile(
            track_name="TrLR",
            track_number=None,
            role=None,
            path=path,
            file_size=path.stat().st_size,
        )

    track_number = int(track)
    return TrackFile(
        track_name=f"Tr{track_number}",
        track_number=track_number,
        role=ROLE_MAP.get(track_number, f"guest{track_number - 2}"),
        path=path,
        file_size=path.stat().st_size,
    )


def _infer_recorded_at(
    spoken_tracks: List[TrackFile],
    mix_track: Optional[TrackFile],
    session_dir: Path,
) -> tuple[Optional[str], Optional[str]]:
    candidates = [track.path for track in spoken_tracks]
    if mix_track is not None:
        candidates.append(mix_track.path)

    birth_times = [_get_birth_time(path) for path in candidates]
    available_birth_times = [value for value in birth_times if value is not None]
    if available_birth_times:
        return min(available_birth_times).isoformat(), "file_birth_time"

    directory_birth_time = _get_birth_time(session_dir)
    if directory_birth_time is not None:
        return directory_birth_time.isoformat(), "directory_birth_time"

    parsed_basename = _parse_basename_timestamp(session_dir.name)
    if parsed_basename is not None:
        return parsed_basename.isoformat(), "basename"

    return None, None


def _get_birth_time(path: Path) -> Optional[datetime]:
    stat_result = path.stat()
    birth_time = getattr(stat_result, "st_birthtime", None)
    if birth_time is None:
        return None
    return datetime.fromtimestamp(birth_time).astimezone()


def _parse_basename_timestamp(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.strptime(value, "%m%d%y_%H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
