from pathlib import Path
from typing import Optional

import typer

from sexdrunks_automation.auphonic import (
    AuphonicError,
    prepare_session_with_auphonic,
    process_session_with_auphonic,
    start_prepared_session_with_auphonic,
)
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


def _bootstrap(config_path: Path, debug: bool = False):
    loaded_config = load_config(config_path)
    runtime_paths = build_runtime_paths(loaded_config)
    ensure_runtime_dirs(runtime_paths)
    initialize_database(runtime_paths.db_file)
    logger = configure_logging(runtime_paths.logs_dir, debug=debug)
    return loaded_config, runtime_paths, logger


def _reset_database_for_runtime(config_path: Path) -> tuple:
    loaded_config = load_config(config_path)
    runtime_paths = build_runtime_paths(loaded_config)
    ensure_runtime_dirs(runtime_paths)

    db_path = Path(runtime_paths.db_file)
    if db_path.exists():
        db_path.unlink()

    initialize_database(runtime_paths.db_file)
    write_health_snapshot(runtime_paths, loaded_config, worker_status="stopped")
    return loaded_config, runtime_paths


def _print_sessions(runtime_paths) -> None:
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
def init(
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Initialize runtime directories, logging, health file, and SQLite schema."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)
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


@app.command("reset-db")
def reset_db(
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Delete the current SQLite database without prompting.",
    ),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Delete and recreate the SQLite database for the current runtime."""
    if not yes:
        typer.echo("Refusing to delete the database without --yes.")
        raise typer.Exit(code=1)

    config_path = _resolve_config_path(config)
    _loaded_config, runtime_paths = _reset_database_for_runtime(config_path)

    typer.echo(f"Reset database: {runtime_paths.db_file}")


@app.command("refresh")
def refresh(
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Reset DB, rescan recordings, and list sessions."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths = _reset_database_for_runtime(config_path)
    logger = configure_logging(runtime_paths.logs_dir, debug=debug)

    typer.echo(f"Reset database: {runtime_paths.db_file}")

    outcome = scan_and_ingest(
        loaded_config.paths.raw_root, runtime_paths.db_file, logger=logger
    )
    snapshot = write_health_snapshot(
        runtime_paths, loaded_config, worker_status="stopped"
    )
    logger.info(
        "refresh finished discovered=%s created=%s duplicates=%s unsupported=%s",
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

    _print_sessions(runtime_paths)


@app.command()
def health(
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Show current local health snapshot."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, _logger = _bootstrap(config_path, debug=debug)
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
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """List known sessions."""
    config_path = _resolve_config_path(config)
    _loaded_config, runtime_paths, _logger = _bootstrap(config_path, debug=debug)
    _print_sessions(runtime_paths)


@app.command()
def status(
    session_uid: str,
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Show one tracked session."""
    config_path = _resolve_config_path(config)
    _loaded_config, runtime_paths, _logger = _bootstrap(config_path, debug=debug)
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
    typer.echo(f"auphonic_production_id: {session.auphonic_production_id or 'none'}")
    typer.echo(f"pre_edit_master_path: {session.pre_edit_master_path or 'none'}")
    typer.echo(f"transcript_path: {session.transcript_path or 'none'}")
    typer.echo(f"created_at: {session.created_at}")
    typer.echo(f"updated_at: {session.updated_at}")
    typer.echo(f"failure_reason: {session.failure_reason or 'none'}")
    typer.echo(f"failed_at_stage: {session.failed_at_stage or 'none'}")


@app.command()
def scan(
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Scan raw recordings and ingest new sessions."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)
    outcome = scan_and_ingest(
        loaded_config.paths.raw_root, runtime_paths.db_file, logger=logger
    )
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


@app.command("auphonic")
def auphonic_command(
    session_uid: str,
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Upload one queued session to Auphonic and download outputs."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)
    session = get_session(runtime_paths.db_file, session_uid)
    if session is None:
        typer.echo(f"Session not found: {session_uid}")
        raise typer.Exit(code=1)

    try:
        updated = process_session_with_auphonic(
            config=loaded_config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            logger=logger,
        )
    except AuphonicError as exc:
        logger.error("Auphonic processing failed for %s: %s", session_uid, exc)
        typer.echo(f"Auphonic error: {exc}")
        raise typer.Exit(code=1)

    typer.echo(f"session_uid: {updated.session_uid}")
    typer.echo(f"status: {updated.status}")
    typer.echo(f"auphonic_production_id: {updated.auphonic_production_id or 'none'}")
    typer.echo(f"pre_edit_master_path: {updated.pre_edit_master_path or 'none'}")
    typer.echo(f"transcript_path: {updated.transcript_path or 'none'}")


@app.command("auphonic-prepare")
def auphonic_prepare_command(
    session_uid: str,
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Create an Auphonic production and upload files without starting it."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)
    session = get_session(runtime_paths.db_file, session_uid)
    if session is None:
        typer.echo(f"Session not found: {session_uid}")
        raise typer.Exit(code=1)

    try:
        updated = prepare_session_with_auphonic(
            config=loaded_config,
            db_file=runtime_paths.db_file,
            session=session,
            logger=logger,
        )
    except AuphonicError as exc:
        logger.error("Auphonic prepare failed for %s: %s", session_uid, exc)
        typer.echo(f"Auphonic error: {exc}")
        raise typer.Exit(code=1)

    typer.echo(f"session_uid: {updated.session_uid}")
    typer.echo(f"status: {updated.status}")
    typer.echo(f"auphonic_production_id: {updated.auphonic_production_id or 'none'}")
    typer.echo("auphonic_started: no")


@app.command("auphonic-start")
def auphonic_start_command(
    session_uid: str,
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Start an already-uploaded Auphonic production and fetch outputs."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)
    session = get_session(runtime_paths.db_file, session_uid)
    if session is None:
        typer.echo(f"Session not found: {session_uid}")
        raise typer.Exit(code=1)

    try:
        updated = start_prepared_session_with_auphonic(
            config=loaded_config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            logger=logger,
        )
    except AuphonicError as exc:
        logger.error("Auphonic start failed for %s: %s", session_uid, exc)
        typer.echo(f"Auphonic error: {exc}")
        raise typer.Exit(code=1)

    typer.echo(f"session_uid: {updated.session_uid}")
    typer.echo(f"status: {updated.status}")
    typer.echo(f"auphonic_production_id: {updated.auphonic_production_id or 'none'}")
    typer.echo(f"pre_edit_master_path: {updated.pre_edit_master_path or 'none'}")
    typer.echo(f"transcript_path: {updated.transcript_path or 'none'}")
