from dataclasses import replace
from pathlib import Path

from sexdrunks_automation.fingerprint import build_fingerprint, build_session_uid
from sexdrunks_automation.scanner import scan_raw_sessions


def test_fingerprint_is_stable_for_fixture_session(fixture_raw_root) -> None:
    scan_result = scan_raw_sessions(fixture_raw_root)
    candidate = scan_result.candidates[0]

    fingerprint_a = build_fingerprint(candidate)
    fingerprint_b = build_fingerprint(candidate)

    assert fingerprint_a == fingerprint_b
    assert build_session_uid(fingerprint_a).startswith("sd-")


def test_fingerprint_does_not_depend_on_session_folder_name(
    fixture_raw_root, tmp_path: Path
) -> None:
    candidate = scan_raw_sessions(fixture_raw_root).candidates[0]

    renamed_dir = tmp_path / "totally-different-name"
    renamed_dir.mkdir()
    renamed_tracks = []
    for track in candidate.spoken_tracks:
        target = renamed_dir / track.path.name
        target.write_bytes(track.path.read_bytes())
        renamed_tracks.append(replace(track, path=target))

    renamed_mix_track = None
    if candidate.mix_track is not None:
        target = renamed_dir / candidate.mix_track.path.name
        target.write_bytes(candidate.mix_track.path.read_bytes())
        renamed_mix_track = replace(candidate.mix_track, path=target)

    renamed_candidate = replace(
        candidate,
        session_path=renamed_dir,
        session_folder_name=renamed_dir.name,
        spoken_tracks=renamed_tracks,
        mix_track=renamed_mix_track,
    )

    assert build_fingerprint(candidate) == build_fingerprint(renamed_candidate)


def test_fingerprint_changes_when_audio_content_changes(
    fixture_raw_root, tmp_path: Path
) -> None:
    candidate = scan_raw_sessions(fixture_raw_root).candidates[0]

    mutated_dir = tmp_path / candidate.session_folder_name
    mutated_dir.mkdir()
    mutated_tracks = []
    for index, track in enumerate(candidate.spoken_tracks):
        target = mutated_dir / track.path.name
        content = track.path.read_bytes()
        if index == 0:
            content += b"-mutated"
        target.write_bytes(content)
        mutated_tracks.append(
            replace(track, path=target, file_size=target.stat().st_size)
        )

    mutated_mix_track = None
    if candidate.mix_track is not None:
        target = mutated_dir / candidate.mix_track.path.name
        target.write_bytes(candidate.mix_track.path.read_bytes())
        mutated_mix_track = replace(
            candidate.mix_track,
            path=target,
            file_size=target.stat().st_size,
        )

    mutated_candidate = replace(
        candidate,
        session_path=mutated_dir,
        spoken_tracks=mutated_tracks,
        mix_track=mutated_mix_track,
    )

    assert build_fingerprint(candidate) != build_fingerprint(mutated_candidate)
