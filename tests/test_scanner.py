from sexdrunks_automation.scanner import scan_raw_sessions


def test_scanner_detects_supported_and_unsupported_sessions(fixture_raw_root) -> None:
    scan_result = scan_raw_sessions(fixture_raw_root)

    assert len(scan_result.candidates) == 2
    assert len(scan_result.unsupported_sessions) == 2

    first = scan_result.candidates[0]
    second = scan_result.candidates[1]

    assert first.session_folder_name == "010124_013143"
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
