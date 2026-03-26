from pathlib import Path

from sexdrunks_automation import scanner
from sexdrunks_automation.scanner import scan_raw_sessions


def test_scanner_detects_supported_and_unsupported_sessions(fixture_raw_root) -> None:
    scan_result = scan_raw_sessions(fixture_raw_root)

    assert len(scan_result.candidates) == 2
    assert len(scan_result.unsupported_sessions) == 2

    first = scan_result.candidates[0]
    second = scan_result.candidates[1]

    assert first.session_folder_name == "010124_013143"
    assert first.source_basename == "010124_013143"
    assert first.recorded_at is not None
    assert first.recorded_at_source in {
        "file_birth_time",
        "directory_birth_time",
        "basename",
    }
    assert [track.role for track in first.spoken_tracks] == ["host1", "host2"]
    assert first.mix_track is not None
    assert first.mix_track.track_name == "TrLR"

    assert second.session_folder_name == "010724_013643"
    assert [track.role for track in second.spoken_tracks] == [
        "host1",
        "host2",
        "guest1",
        "guest2",
    ]

    reasons = {
        item.session_path.name: item.reason for item in scan_result.unsupported_sessions
    }
    assert "fewer than 2" in reasons["011424_020000"]
    assert "more than 4" in reasons["012124_021500"]


def test_scanner_rejects_mixed_basenames_within_one_session(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    session_dir = raw_root / "010124_013143"
    session_dir.mkdir(parents=True)

    (session_dir / "010124_013143_Tr1.WAV").write_bytes(b"one-a")
    (session_dir / "010124_013143_Tr2.WAV").write_bytes(b"one-b")
    (session_dir / "DIFFERENT_Tr3.WAV").write_bytes(b"two-c")
    (session_dir / "DIFFERENT_Tr4.WAV").write_bytes(b"two-d")

    scan_result = scan_raw_sessions(raw_root)

    assert len(scan_result.candidates) == 0
    assert len(scan_result.unsupported_sessions) == 1
    assert "multiple recorder basenames" in scan_result.unsupported_sessions[0].reason


def test_scanner_rejects_invalid_track_numbers(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    session_dir = raw_root / "010124_013143"
    session_dir.mkdir(parents=True)

    (session_dir / "010124_013143_Tr0.WAV").write_bytes(b"bad-zero")
    (session_dir / "010124_013143_Tr1.WAV").write_bytes(b"good-one")

    scan_result = scan_raw_sessions(raw_root)

    assert len(scan_result.candidates) == 0
    assert len(scan_result.unsupported_sessions) == 1
    assert (
        "unsupported spoken track number" in scan_result.unsupported_sessions[0].reason
    )


def test_scanner_ignores_silent_extra_tracks(tmp_path: Path, wav_writer) -> None:
    raw_root = tmp_path / "raw"
    session_dir = raw_root / "010124_013143"

    wav_writer(session_dir / "010124_013143_Tr1.WAV", [0, 1200, -1200, 600] * 500)
    wav_writer(session_dir / "010124_013143_Tr2.WAV", [0, -900, 900, -450] * 500)
    wav_writer(session_dir / "010124_013143_Tr3.WAV", [0] * 4000)
    wav_writer(session_dir / "010124_013143_Tr4.WAV", [0] * 4000)

    scan_result = scan_raw_sessions(raw_root)

    assert len(scan_result.candidates) == 1
    candidate = scan_result.candidates[0]
    assert [track.track_name for track in candidate.spoken_tracks] == ["Tr1", "Tr2"]
    assert [track.role for track in candidate.spoken_tracks] == ["host1", "host2"]
    assert len(scan_result.unsupported_sessions) == 0


def test_scanner_rejects_when_only_one_track_has_audio(
    tmp_path: Path, wav_writer
) -> None:
    raw_root = tmp_path / "raw"
    session_dir = raw_root / "010124_013143"

    wav_writer(session_dir / "010124_013143_Tr1.WAV", [0, 1200, -1200, 600] * 500)
    wav_writer(session_dir / "010124_013143_Tr2.WAV", [0] * 4000)
    wav_writer(session_dir / "010124_013143_Tr3.WAV", [0] * 4000)
    wav_writer(session_dir / "010124_013143_Tr4.WAV", [0] * 4000)

    scan_result = scan_raw_sessions(raw_root)

    assert len(scan_result.candidates) == 0
    assert len(scan_result.unsupported_sessions) == 1
    assert "non-silent spoken tracks" in scan_result.unsupported_sessions[0].reason


def test_scanner_treats_low_level_noise_as_silent(tmp_path: Path, wav_writer) -> None:
    raw_root = tmp_path / "raw"
    session_dir = raw_root / "010124_012430"

    wav_writer(session_dir / "010124_012430_Tr1.WAV", [0, 1200, -1200, 600] * 500)
    wav_writer(session_dir / "010124_012430_Tr2.WAV", [0, -900, 900, -450] * 500)
    wav_writer(session_dir / "010124_012430_Tr3.WAV", [6, -6, 8, -8] * 1000)
    wav_writer(session_dir / "010124_012430_Tr4.WAV", [4, -4, 5, -5] * 1000)

    scan_result = scan_raw_sessions(raw_root)

    assert len(scan_result.candidates) == 1
    candidate = scan_result.candidates[0]
    assert [track.track_name for track in candidate.spoken_tracks] == ["Tr1", "Tr2"]


def test_scanner_supports_flat_recording_root_layout(
    tmp_path: Path, wav_writer
) -> None:
    raw_root = tmp_path / "02_recordings"
    session_dir = raw_root / "010124_012430"

    wav_writer(session_dir / "010124_012430_Tr1.WAV", [0, 1000, -1000, 500] * 500)
    wav_writer(session_dir / "010124_012430_Tr2.WAV", [0, -800, 800, -400] * 500)
    wav_writer(session_dir / "010124_012430_Tr3.WAV", [0] * 4000)
    wav_writer(session_dir / "010124_012430_Tr4.WAV", [0] * 4000)
    wav_writer(session_dir / "010124_012430_TrLR.WAV", [0, 600, -600, 300] * 500)

    scan_result = scan_raw_sessions(raw_root)

    assert len(scan_result.candidates) == 1
    candidate = scan_result.candidates[0]
    assert candidate.session_folder_name == "010124_012430"
    assert candidate.source_basename == "010124_012430"
    assert candidate.date_folder_name == ""
    assert [track.track_name for track in candidate.spoken_tracks] == ["Tr1", "Tr2"]
    assert candidate.mix_track is not None


def test_scanner_uses_relative_ffmpeg_levels_to_drop_quiet_tracks(
    tmp_path: Path, monkeypatch
) -> None:
    raw_root = tmp_path / "raw"
    session_dir = raw_root / "010124_012430"
    session_dir.mkdir(parents=True)

    for name in [
        "010124_012430_Tr1.WAV",
        "010124_012430_Tr2.WAV",
        "010124_012430_Tr3.WAV",
        "010124_012430_Tr4.WAV",
    ]:
        (session_dir / name).write_bytes(b"non-empty")

    rms_by_name = {
        "010124_012430_Tr1.WAV": -80.0,
        "010124_012430_Tr2.WAV": -76.0,
        "010124_012430_Tr3.WAV": -102.0,
        "010124_012430_Tr4.WAV": -101.0,
    }
    monkeypatch.setattr(
        scanner,
        "_ffmpeg_rms_db",
        lambda path: rms_by_name.get(path.name),
    )

    scan_result = scan_raw_sessions(raw_root)

    assert len(scan_result.candidates) == 1
    candidate = scan_result.candidates[0]
    assert [track.track_name for track in candidate.spoken_tracks] == ["Tr1", "Tr2"]
