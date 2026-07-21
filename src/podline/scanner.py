import logging
import re
import subprocess
import shutil
import wave
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Union

from podline.models import (
    ScanCandidate,
    ScanResult,
    TrackFile,
    UnsupportedSession,
)

TRACK_PATTERN = re.compile(
    r"(?:(?P<basename>.+)_)?Tr(?P<track>LR|\d+)\.(?P<ext>wav|WAV|webm|WEBM|opus|OPUS)$"
)
ROLE_MAP = {
    1: "host1",
    2: "host2",
    3: "guest1",
    4: "guest2",
}
_GUEST_ROLE_PATTERN = re.compile(r"guest(\d+)")


def recorder_track_number(role: str) -> Optional[int]:
    """Inverse of ROLE_MAP: the recorder track number a role was derived from.

    Recorder order is the pipeline's canonical track order (Tr1 -> host1,
    Tr2 -> host2, Tr3 -> guest1, ...); role names must never be ordered
    alphabetically, which would put guests before hosts.
    """
    normalized = role.strip().lower()
    for number, name in ROLE_MAP.items():
        if name == normalized:
            return number
    match = _GUEST_ROLE_PATTERN.fullmatch(normalized)
    if match:
        return int(match.group(1)) + 2
    return None
WAV_ANALYSIS_FRAMES = 16384
SILENCE_RMS_THRESHOLD = 100
SESSION_SILENCE_FLOOR_DB = -90.0
SESSION_MIN_LOUDEST_RMS_DB = -88.0
SESSION_RELATIVE_ACTIVITY_WINDOW_DB = 18.0


def scan_raw_sessions(
    raw_root: Path, logger: Optional[logging.Logger] = None
) -> ScanResult:
    candidates: List[ScanCandidate] = []
    unsupported_sessions: List[UnsupportedSession] = []

    if not raw_root.exists():
        return ScanResult(
            candidates=candidates, unsupported_sessions=unsupported_sessions
        )

    for session_dir in sorted(path for path in raw_root.iterdir() if path.is_dir()):
        if not _looks_like_session_dir(session_dir):
            continue
        _debug_log(logger, "Scanning session directory %s", session_dir)
        parsed = _parse_session_dir(session_dir, logger=logger)
        if isinstance(parsed, UnsupportedSession):
            unsupported_sessions.append(parsed)
        elif parsed is not None:
            candidates.append(parsed)

    return ScanResult(candidates=candidates, unsupported_sessions=unsupported_sessions)


def _looks_like_session_dir(path: Path) -> bool:
    for entry in path.iterdir():
        if entry.is_file() and TRACK_PATTERN.match(entry.name):
            return True
    return False


def _parse_session_dir(session_dir: Path, logger: Optional[logging.Logger] = None):
    track_files: List[TrackFile] = []
    basenames = set()
    for entry in sorted(path for path in session_dir.iterdir() if path.is_file()):
        parsed_track = _parse_track_file(entry, logger=logger)
        if isinstance(parsed_track, UnsupportedSession):
            return UnsupportedSession(
                session_path=session_dir,
                reason=parsed_track.reason,
                spoken_track_count=parsed_track.spoken_track_count,
            )
        if parsed_track is not None:
            track_files.append(parsed_track)
            match = TRACK_PATTERN.match(entry.name)
            if match is not None:
                basenames.add(match.group("basename") or "")

    if not track_files:
        return None

    if len(basenames) != 1:
        return UnsupportedSession(
            session_path=session_dir,
            reason="multiple recorder basenames found in one session folder",
            spoken_track_count=len(
                [track for track in track_files if track.track_number is not None]
            ),
        )

    source_basename = next(iter(basenames)) or session_dir.name
    all_spoken_tracks = sorted(
        (track for track in track_files if track.track_number is not None),
        key=lambda item: item.track_number,
    )
    spoken_tracks = _filter_active_spoken_tracks(all_spoken_tracks, logger=logger)
    mix_track = next(
        (track for track in track_files if track.track_name == "TrLR"), None
    )
    spoken_track_count = len(spoken_tracks)

    if spoken_track_count < 2:
        return UnsupportedSession(
            session_path=session_dir,
            reason="expected 2 to 4 non-silent spoken tracks, found fewer than 2",
            spoken_track_count=spoken_track_count,
        )

    if spoken_track_count > 4:
        return UnsupportedSession(
            session_path=session_dir,
            reason="expected 2 to 4 non-silent spoken tracks, found more than 4",
            spoken_track_count=spoken_track_count,
        )

    invalid_track_numbers = [
        track.track_number
        for track in spoken_tracks
        if track.track_number is not None
        and (track.track_number < 1 or track.track_number > 4)
    ]
    if invalid_track_numbers:
        invalid = invalid_track_numbers[0]
        return UnsupportedSession(
            session_path=session_dir,
            reason=f"unsupported spoken track number: Tr{invalid}",
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
        date_folder_name="",
        recorded_at=recorded_at,
        recorded_at_source=recorded_at_source,
        spoken_tracks=spoken_tracks,
        mix_track=mix_track,
    )


def _parse_track_file(
    path: Path, logger: Optional[logging.Logger] = None
) -> Optional[Union[TrackFile, UnsupportedSession]]:
    match = TRACK_PATTERN.match(path.name)
    if match is None:
        return None

    track = match.group("track")
    if track == "LR":
        has_audio, activity_rms_db = _track_has_audio(path, logger=logger)
        return TrackFile(
            track_name="TrLR",
            track_number=None,
            role=None,
            path=path,
            file_size=path.stat().st_size,
            has_audio=has_audio,
            activity_rms_db=activity_rms_db,
        )

    track_number = int(track)
    if track_number < 1:
        return UnsupportedSession(
            session_path=path.parent,
            reason=f"unsupported spoken track number: Tr{track_number}",
            spoken_track_count=0,
        )

    has_audio, activity_rms_db = _track_has_audio(path, logger=logger)
    return TrackFile(
        track_name=f"Tr{track_number}",
        track_number=track_number,
        role=ROLE_MAP.get(track_number, f"guest{track_number - 2}"),
        path=path,
        file_size=path.stat().st_size,
        has_audio=has_audio,
        activity_rms_db=activity_rms_db,
    )


def _track_has_audio(
    path: Path, logger: Optional[logging.Logger] = None
) -> Tuple[bool, Optional[float]]:
    ffmpeg_rms_db = _ffmpeg_rms_db(path, logger=logger)
    if ffmpeg_rms_db is not None:
        _debug_log(
            logger,
            "Track analysis path=%s rms_db=%s active=%s via=ffmpeg",
            path,
            ffmpeg_rms_db,
            ffmpeg_rms_db >= SESSION_SILENCE_FLOOR_DB,
        )
        return ffmpeg_rms_db >= SESSION_SILENCE_FLOOR_DB, ffmpeg_rms_db

    try:
        with wave.open(str(path), "rb") as wav_file:
            sample_width = wav_file.getsampwidth()
            while True:
                frames = wav_file.readframes(WAV_ANALYSIS_FRAMES)
                if not frames:
                    return False, None
                if _pcm_rms(frames, sample_width) >= SILENCE_RMS_THRESHOLD:
                    _debug_log(
                        logger, "Track analysis path=%s active=True via=pcm", path
                    )
                    return True, None
    except (wave.Error, EOFError, OSError):
        fallback_active = path.stat().st_size > 0
        _debug_log(
            logger,
            "Track analysis path=%s active=%s via=size-fallback",
            path,
            fallback_active,
        )
        return fallback_active, None
    _debug_log(logger, "Track analysis path=%s active=False via=pcm", path)
    return False, None


def _filter_active_spoken_tracks(
    spoken_tracks: List[TrackFile], logger: Optional[logging.Logger] = None
) -> List[TrackFile]:
    audio_tracks = [track for track in spoken_tracks if track.has_audio]
    rms_tracks = [track for track in audio_tracks if track.activity_rms_db is not None]
    if not rms_tracks:
        return audio_tracks

    loudest_rms_db = max(
        track.activity_rms_db
        for track in rms_tracks
        if track.activity_rms_db is not None
    )
    _debug_log(
        logger,
        "Track activity loudest_rms_db=%s candidate_tracks=%s",
        loudest_rms_db,
        [f"{track.track_name}:{track.activity_rms_db}" for track in rms_tracks],
    )
    if loudest_rms_db < SESSION_MIN_LOUDEST_RMS_DB:
        _debug_log(
            logger,
            "Rejecting session as all-noise loudest_rms_db=%s threshold=%s",
            loudest_rms_db,
            SESSION_MIN_LOUDEST_RMS_DB,
        )
        return []

    activity_threshold_db = max(
        loudest_rms_db - SESSION_RELATIVE_ACTIVITY_WINDOW_DB,
        SESSION_SILENCE_FLOOR_DB,
    )
    filtered = [
        track
        for track in audio_tracks
        if track.activity_rms_db is None
        or track.activity_rms_db >= activity_threshold_db
    ]
    _debug_log(
        logger,
        "Track activity threshold_db=%s kept_tracks=%s",
        activity_threshold_db,
        [f"{track.track_name}:{track.activity_rms_db}" for track in filtered],
    )
    return filtered


def _ffmpeg_rms_db(
    path: Path, logger: Optional[logging.Logger] = None
) -> Optional[float]:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        return None

    command = [
        ffmpeg_path,
        "-v",
        "info",
        "-i",
        str(path),
        "-af",
        "astats=metadata=0:reset=0",
        "-f",
        "null",
        "-",
    ]
    _debug_log(logger, "Running ffmpeg level analysis for %s", path)
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        _debug_log(
            logger,
            "ffmpeg level analysis failed for %s returncode=%s",
            path,
            result.returncode,
        )
        return None

    match = re.findall(r"RMS level dB:\s*([-0-9.inf]+)", result.stderr)
    if not match:
        return None

    value = match[-1].lower()
    if value in {"inf", "-inf", "nan"}:
        return None

    try:
        return float(value)
    except ValueError:
        return None


def _debug_log(logger: Optional[logging.Logger], message: str, *args) -> None:
    if logger is not None:
        logger.debug(message, *args)


def _pcm_rms(frames: bytes, sample_width: int) -> int:
    if sample_width <= 0:
        return 0
    if sample_width == 1:
        values = [byte - 128 for byte in frames]
    else:
        values = []
        for index in range(0, len(frames), sample_width):
            chunk = frames[index : index + sample_width]
            if len(chunk) != sample_width:
                continue
            values.append(int.from_bytes(chunk, byteorder="little", signed=True))
    if not values:
        return 0
    squared_mean = sum(value * value for value in values) / len(values)
    return int(squared_mean**0.5)


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
