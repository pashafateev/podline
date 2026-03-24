from pathlib import Path
from typing import Optional

import typer

from sexdrunks_automation.config import load_config
from sexdrunks_automation.db import initialize_database
from sexdrunks_automation.health import read_health_snapshot, write_health_snapshot
from sexdrunks_automation.ingest import scan_and_ingest
from sexdrunks_automation.logging_setup import configure_logging
from sexdrunks_automation.paths import build_runtime_paths, ensure_runtime_dirs
from sexdrunks_automation.repository import get_session, list_sessions

app = typer.Typer(help="Sex Drunks automation CLI.")


def _resolve_config_path(config: Optional[Path]) -> Path:
    if config is not None:
        return config.expanduser().resolve()
    return (Path.home() / "sexdrunks-automation" / "config.yaml").resolve()


def _bootstrap(config_path: Path):
    loaded_config = load_config(config_path)
    runtime_paths = build_runtime_paths(loaded_config)
    ensure_runtime_dirs(runtime_paths)
    initialize_database(runtime_paths.db_file)
    logger = configure_logging(runtime_paths.logs_dir)
    return loaded_config, runtime_paths, logger


@app.command()
def init(
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Initialize runtime directories, logging, health file, and SQLite schema."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path)
    snapshot = write_health_snapshot(
        runtime_paths, loaded_config, worker_status="stopped"
    )
    logger.info("runtime initialized")

    typer.echo(f"Initialized runtime home: {runtime_paths.home}")
    typer.echo(f"Database: {runtime_paths.db_file}")
    typer.echo(f"Health file: {runtime_paths.health_file}")
    typer.echo(
        f"Deliverables mounted: {'yes' if snapshot.external_drive_mounted else 'no'}"
    )


@app.command()
def health(
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Show current local health snapshot."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, _logger = _bootstrap(config_path)
    snapshot = read_health_snapshot(runtime_paths, loaded_config)

    typer.echo(f"worker_status: {snapshot.worker_status}")
    typer.echo(f"last_heartbeat: {snapshot.last_heartbeat or 'n/a'}")
    typer.echo(
        f"external_drive_mounted: {'yes' if snapshot.external_drive_mounted else 'no'}"
    )
    typer.echo(f"queue_depth: {snapshot.queue_depth}")
    typer.echo(f"session_count: {snapshot.session_count}")
    typer.echo(f"last_error: {snapshot.last_error or 'none'}")


@app.command("list")
def list_command(
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """List known sessions."""
    config_path = _resolve_config_path(config)
    _loaded_config, runtime_paths, _logger = _bootstrap(config_path)
    sessions = list_sessions(runtime_paths.db_file)

    if not sessions:
        typer.echo("No sessions tracked yet.")
        return

    for session in sessions:
        typer.echo(
            " | ".join(
                [
                    session.session_uid,
                    session.status,
                    session.recorded_at or "unknown-recorded-at",
                    session.source_basename or "unknown-source",
                    session.source_path,
                    session.updated_at,
                ]
            )
        )


@app.command()
def status(
    session_uid: str,
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Show one tracked session."""
    config_path = _resolve_config_path(config)
    _loaded_config, runtime_paths, _logger = _bootstrap(config_path)
    session = get_session(runtime_paths.db_file, session_uid)

    if session is None:
        typer.echo(f"Session not found: {session_uid}")
        raise typer.Exit(code=1)

    typer.echo(f"session_uid: {session.session_uid}")
    typer.echo(f"status: {session.status}")
    typer.echo(f"source_basename: {session.source_basename or 'none'}")
    typer.echo(f"source_date_folder: {session.source_date_folder or 'none'}")
    typer.echo(f"source_path: {session.source_path}")
    typer.echo(f"recorded_at: {session.recorded_at or 'none'}")
    typer.echo(f"recorded_at_source: {session.recorded_at_source or 'none'}")
    typer.echo(f"fingerprint: {session.fingerprint}")
    typer.echo(f"created_at: {session.created_at}")
    typer.echo(f"updated_at: {session.updated_at}")
    typer.echo(f"failure_reason: {session.failure_reason or 'none'}")
    typer.echo(f"failed_at_stage: {session.failed_at_stage or 'none'}")


@app.command()
def scan(
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Scan raw recordings and ingest new sessions."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path)
    outcome = scan_and_ingest(loaded_config.paths.raw_root, runtime_paths.db_file)
    snapshot = write_health_snapshot(
        runtime_paths, loaded_config, worker_status="stopped"
    )

    logger.info(
        "scan finished discovered=%s created=%s duplicates=%s unsupported=%s",
        outcome.discovered,
        outcome.created,
        outcome.duplicates,
        len(outcome.unsupported),
    )

    typer.echo(f"discovered: {outcome.discovered}")
    typer.echo(f"created: {outcome.created}")
    typer.echo(f"duplicates: {outcome.duplicates}")
    typer.echo(f"unsupported: {len(outcome.unsupported)}")
    typer.echo(f"queue_depth: {snapshot.queue_depth}")

    for unsupported in outcome.unsupported:
        typer.echo(
            " | ".join(
                [
                    "unsupported_session",
                    f"path={unsupported.session_path}",
                    f"reason={unsupported.reason}",
                    f"spoken_tracks={unsupported.spoken_track_count}",
                ]
            )
        )
