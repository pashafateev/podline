from pathlib import Path
from typing import List

from typer.testing import CliRunner

from sexdrunks_automation import cli as cli_module
from sexdrunks_automation.cli import app
from sexdrunks_automation.cozytrack.client import CozytrackSession, CozytrackTrack
from sexdrunks_automation.db import initialize_database
from sexdrunks_automation.repository import get_session_by_cozytrack_id


runner = CliRunner()


def _write_config(config_file: Path, runtime_home: Path, raw_root: Path) -> None:
    config_file.write_text(
        "\n".join(
            [
                f"runtime_home: {runtime_home}",
                "paths:",
                f"  raw_root: {raw_root}",
                f"  deliverables_root: {runtime_home / 'deliverables'}",
                "polling:",
                "  interval_seconds: 30",
                "notifications:",
                "  enabled: false",
                "auphonic:",
                "  enabled: false",
                '  api_key: ""',
                '  base_url: "https://auphonic.example"',
                "  request_timeout_seconds: 600",
                "  poll_interval_seconds: 15",
                "  max_poll_attempts: 20",
                "  preset_ids:",
                '    speakers_2: ""',
                '    speakers_3: ""',
                '    speakers_4: ""',
                "podbean:",
                "  enabled: false",
                '  client_id: ""',
                '  client_secret: ""',
                '  base_url: "https://api.podbean.example/v1"',
                '  podcast_id: ""',
                "cozytrack:",
                '  base_url: "https://cozytrack.example"',
                '  api_key: "test-key"',
                '  download_subdir: "cozytrack"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _make_session(
    session_id: str, name: str, track_count: int = 2
) -> CozytrackSession:
    return CozytrackSession(
        id=session_id,
        name=name,
        status="ready",
        finalized_at="2026-04-24T12:00:00Z",
        tracks=[
            CozytrackTrack(
                id=f"{session_id}-tr-{i + 1}",
                participant_name=f"speaker-{i + 1}",
                status="complete",
                created_at=f"2026-04-24T12:00:0{i}Z",
            )
            for i in range(track_count)
        ],
    )


class _FakeClient:
    def __init__(self, sessions: List[CozytrackSession]) -> None:
        self._sessions = {s.id: s for s in sessions}
        self.requested_track_ids: List[str] = []

    def list_ready_sessions(self) -> List[CozytrackSession]:
        return list(self._sessions.values())

    def get_session(self, session_id: str) -> CozytrackSession:
        return self._sessions[session_id]

    def get_track_download_url(self, track_id: str) -> str:
        self.requested_track_ids.append(track_id)
        return f"https://s3.example/{track_id}"

    def download_to_file(self, url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"webm-bytes-data" * 64)


def test_ct_list_filters_already_ingested_ids(tmp_path: Path, monkeypatch) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    fake = _FakeClient(
        [
            _make_session("sess-fresh", "Fresh Session"),
            _make_session("sess-already", "Already Ingested"),
        ]
    )
    monkeypatch.setattr(
        cli_module,
        "list_ready_sessions_for_ingest",
        lambda *, config, db_file: [
            session
            for session in fake.list_ready_sessions()
            if session.id != "sess-already"
        ],
    )

    db_file = runtime_home / "state" / "state.db"
    initialize_database(str(db_file))

    result = runner.invoke(app, ["ct-list", "--config", str(config_file)])

    assert result.exit_code == 0, result.stdout
    assert "sess-fresh" in result.stdout
    assert "Fresh Session" in result.stdout
    assert "sess-already" not in result.stdout


def test_ct_ingest_creates_session_row(tmp_path: Path, monkeypatch) -> None:
    runtime_home = tmp_path / "runtime"
    raw_root = tmp_path / "raw"
    config_file = tmp_path / "config.yaml"
    _write_config(config_file, runtime_home, raw_root)

    fake = _FakeClient([_make_session("sess-1", "Demo Episode")])

    def fake_ingest(*, config, db_file, cozytrack_session_id, name_override, logger):
        from sexdrunks_automation.cozytrack.ingest import ingest_session

        return ingest_session(
            config=config,
            db_file=db_file,
            cozytrack_session_id=cozytrack_session_id,
            name_override=name_override,
            client=fake,
            logger=logger,
        )

    monkeypatch.setattr(cli_module, "cozytrack_ingest_session", fake_ingest)

    result = runner.invoke(
        app, ["ct-ingest", "sess-1", "--config", str(config_file)]
    )

    assert result.exit_code == 0, result.stdout
    db_file = runtime_home / "state" / "state.db"
    record = get_session_by_cozytrack_id(str(db_file), "sess-1")
    assert record is not None
    assert Path(record.source_path).exists()
    assert (Path(record.source_path) / "Tr1.webm").exists()
    assert (Path(record.source_path) / "Tr2.webm").exists()
