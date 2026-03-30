from pathlib import Path
from typing import Optional

import typer

from sexdrunks_automation.auphonic import (
    AuphonicError,
    inspect_production,
    prepare_session_with_auphonic,
    process_session_with_auphonic,
    reconcile_production_to_session,
    start_prepared_session_with_auphonic,
)
from sexdrunks_automation.config import load_config
from sexdrunks_automation.db import initialize_database
from sexdrunks_automation.finalize import (
    FinalizeError,
    finalize_session,
    get_episode_metadata,
)
from sexdrunks_automation.health import read_health_snapshot, write_health_snapshot
from sexdrunks_automation.ingest import scan_and_ingest
from sexdrunks_automation.logging_setup import configure_logging
from sexdrunks_automation.paths import build_runtime_paths, ensure_runtime_dirs
from sexdrunks_automation.repository import get_session, list_sessions

app = typer.Typer(help="Sex Drunks automation CLI.")

STATUS_STYLES = {
    "queued": ("Queued", typer.colors.YELLOW, "○"),
    "auphonic_prepared": ("Prepared", typer.colors.BLUE, "↑"),
    "auphonic_started": ("Running", typer.colors.BLUE, "◌"),
    "waiting_for_finalize": ("Ready To Finalize", typer.colors.GREEN, "✓"),
    "ready_for_draft": ("Ready For Draft", typer.colors.GREEN, "✓"),
    "draft_created": ("Draft Created", typer.colors.GREEN, "★"),
    "failed": ("Failed", typer.colors.RED, "✖"),
}


def _styled(text: str, *, fg=None, bold: bool = False) -> str:
    return typer.style(text, fg=fg, bold=bold)


def _format_status(status: str) -> str:
    label, color, icon = STATUS_STYLES.get(
        status, (status.replace("_", " ").title(), typer.colors.WHITE, "•")
    )
    return _styled(f"{icon} {label}", fg=color, bold=True)


def _should_show_episode_draft(session) -> bool:
    return session.status in {
        "ready_for_draft",
        "draft_created",
    }


def _print_header(title: str) -> None:
    typer.secho(f"✦ {title}", fg=typer.colors.CYAN, bold=True)
    typer.secho("─" * (len(title) + 2), fg=typer.colors.CYAN)


def _print_key_values(items) -> None:
    width = max(len(label) for label, _value in items)
    for label, value in items:
        typer.echo(
            f"{_styled(label.ljust(width), fg=typer.colors.BRIGHT_BLUE, bold=True)}  {value}"
        )


def _print_section(title: str, items) -> None:
    typer.echo()
    typer.secho(title, fg=typer.colors.MAGENTA, bold=True)
    _print_key_values(items)


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return value[: width - 3] + "..."


def _print_session_summary(session) -> None:
    typer.echo(
        "  ".join(
            [
                _styled(_truncate(session.session_uid, 16).ljust(16), bold=True),
                _format_status(session.status).ljust(28),
                _truncate(session.recorded_at or "unknown", 25).ljust(25),
                _styled(
                    _truncate(session.source_basename or "unknown", 18).ljust(18),
                    fg=typer.colors.MAGENTA,
                ),
                _truncate(session.source_path, 60),
            ]
        )
    )


def _print_session_table(sessions) -> None:
    typer.echo(
        "  ".join(
            [
                _styled("session".ljust(16), fg=typer.colors.BRIGHT_BLUE, bold=True),
                _styled("status".ljust(28), fg=typer.colors.BRIGHT_BLUE, bold=True),
                _styled(
                    "recorded_at".ljust(25), fg=typer.colors.BRIGHT_BLUE, bold=True
                ),
                _styled("source".ljust(18), fg=typer.colors.BRIGHT_BLUE, bold=True),
                _styled("path", fg=typer.colors.BRIGHT_BLUE, bold=True),
            ]
        )
    )
    typer.echo(
        "  ".join(
            [
                "-" * 16,
                "-" * 28,
                "-" * 25,
                "-" * 18,
                "-" * 40,
            ]
        )
    )
    for session in sessions:
        _print_session_summary(session)


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
        typer.secho("☷ No sessions tracked yet.", fg=typer.colors.YELLOW, bold=True)
        return

    _print_session_table(sessions)


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

    _print_header("Runtime Initialized")
    _print_key_values(
        [
            ("home", runtime_paths.home),
            ("database", runtime_paths.db_file),
            ("health_file", runtime_paths.health_file),
            (
                "deliverables_mounted",
                _styled("✓ yes", fg=typer.colors.GREEN, bold=True)
                if snapshot.external_drive_mounted
                else _styled("✖ no", fg=typer.colors.RED, bold=True),
            ),
        ]
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

    _print_header("Database Reset")
    _print_key_values([("database", runtime_paths.db_file)])


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

    _print_header("Refresh Complete")
    _print_key_values(
        [
            ("database", runtime_paths.db_file),
            ("discovered", str(outcome.discovered)),
            ("created", str(outcome.created)),
            ("duplicates", str(outcome.duplicates)),
            ("unsupported", str(len(outcome.unsupported))),
            ("queue_depth", str(snapshot.queue_depth)),
        ]
    )

    for unsupported in outcome.unsupported:
        typer.echo()
        _print_header("Unsupported Session")
        typer.echo(
            " | ".join(
                [
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

    _print_header("Health")
    _print_key_values(
        [
            (
                "worker_status",
                _format_status(snapshot.worker_status)
                if snapshot.worker_status in STATUS_STYLES
                else snapshot.worker_status,
            ),
            ("last_heartbeat", snapshot.last_heartbeat or "n/a"),
            (
                "external_drive_mounted",
                _styled("✓ yes", fg=typer.colors.GREEN, bold=True)
                if snapshot.external_drive_mounted
                else _styled("✖ no", fg=typer.colors.RED, bold=True),
            ),
            ("queue_depth", str(snapshot.queue_depth)),
            ("session_count", str(snapshot.session_count)),
            ("last_error", snapshot.last_error or "none"),
        ]
    )


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
        typer.secho(
            f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
        )
        raise typer.Exit(code=1)

    _print_header("Session")
    _print_section(
        "Overview",
        [
            ("session_uid", session.session_uid),
            ("status", _format_status(session.status)),
            ("recorded_at", session.recorded_at or "none"),
            ("recorded_at_source", session.recorded_at_source or "none"),
        ],
    )
    _print_section(
        "Source",
        [
            ("source_basename", session.source_basename or "none"),
            ("source_date_folder", session.source_date_folder or "none"),
            ("source_path", session.source_path),
        ],
    )
    _print_section(
        "Auphonic",
        [
            ("auphonic_production_id", session.auphonic_production_id or "none"),
            ("pre_edit_master_path", session.pre_edit_master_path or "none"),
            ("transcript_path", session.transcript_path or "none"),
            ("final_master_path", session.final_master_path or "none"),
        ],
    )
    _print_section(
        "Metadata",
        [
            ("fingerprint", session.fingerprint),
            ("created_at", session.created_at),
            ("updated_at", session.updated_at),
            ("failure_reason", session.failure_reason or "none"),
            ("failed_at_stage", session.failed_at_stage or "none"),
        ],
    )
    episode = get_episode_metadata(runtime_paths.db_file, session.session_uid)
    if episode is not None:
        _print_section(
            "Episode Draft",
            [
                ("title", episode.title or "none"),
                ("description", episode.description or "none"),
                ("draft_url", episode.draft_url or "none"),
            ],
        )
    elif _should_show_episode_draft(session):
        _print_section(
            "Episode Draft",
            [
                (
                    "status",
                    _styled("AI metadata pending", fg=typer.colors.YELLOW, bold=True),
                ),
                (
                    "next_step",
                    _styled(
                        "Generate with /episode-meta before Podbean draft",
                        fg=typer.colors.WHITE,
                    ),
                ),
            ],
        )


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

    _print_header("Scan Complete")
    _print_key_values(
        [
            ("discovered", str(outcome.discovered)),
            ("created", str(outcome.created)),
            ("duplicates", str(outcome.duplicates)),
            ("unsupported", str(len(outcome.unsupported))),
            ("queue_depth", str(snapshot.queue_depth)),
        ]
    )

    for unsupported in outcome.unsupported:
        typer.echo()
        _print_header("Unsupported Session")
        typer.echo(
            " | ".join(
                [
                    f"path={unsupported.session_path}",
                    f"reason={unsupported.reason}",
                    f"spoken_tracks={unsupported.spoken_track_count}",
                ]
            )
        )


@app.command("finalize")
def finalize_command(
    session_uid: str,
    master: Path = typer.Option(
        ..., "--master", help="Path to the edited final master file."
    ),
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Attach the final edited master and generate draft metadata."""
    config_path = _resolve_config_path(config)
    _loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)
    session = get_session(runtime_paths.db_file, session_uid)
    if session is None:
        typer.secho(
            f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
        )
        raise typer.Exit(code=1)

    try:
        updated = finalize_session(
            db_file=runtime_paths.db_file,
            session=session,
            master_path=master,
        )
    except FinalizeError as exc:
        logger.error("Finalize failed for %s: %s", session_uid, exc)
        typer.echo(f"Finalize error: {exc}")
        raise typer.Exit(code=1)

    episode = get_episode_metadata(runtime_paths.db_file, updated.session_uid)
    _print_header("Finalize Complete")
    _print_section(
        "Overview",
        [
            ("session_uid", updated.session_uid),
            ("status", _format_status(updated.status)),
            ("final_master_path", updated.final_master_path or "none"),
        ],
    )
    if episode is not None:
        _print_section(
            "Episode Draft",
            [
                ("title", episode.title or "none"),
                ("description", episode.description or "none"),
            ],
        )
    else:
        _print_section(
            "Episode Draft",
            [
                (
                    "status",
                    _styled("AI metadata pending", fg=typer.colors.YELLOW, bold=True),
                ),
                (
                    "next_step",
                    _styled(
                        "Use /episode-meta on the transcript before Podbean draft",
                        fg=typer.colors.WHITE,
                    ),
                ),
            ],
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
        typer.secho(
            f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
        )
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

    _print_header("Auphonic Complete")
    _print_key_values(
        [
            ("session_uid", updated.session_uid),
            ("status", _format_status(updated.status)),
            ("auphonic_production_id", updated.auphonic_production_id or "none"),
            ("pre_edit_master_path", updated.pre_edit_master_path or "none"),
            ("transcript_path", updated.transcript_path or "none"),
        ]
    )


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
        typer.secho(
            f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
        )
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

    _print_header("Auphonic Prepared")
    _print_key_values(
        [
            ("session_uid", updated.session_uid),
            ("status", _format_status(updated.status)),
            ("auphonic_production_id", updated.auphonic_production_id or "none"),
            ("auphonic_started", "no"),
        ]
    )


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
        typer.secho(
            f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
        )
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

    _print_header("Auphonic Started")
    _print_key_values(
        [
            ("session_uid", updated.session_uid),
            ("status", _format_status(updated.status)),
            ("auphonic_production_id", updated.auphonic_production_id or "none"),
            ("pre_edit_master_path", updated.pre_edit_master_path or "none"),
            ("transcript_path", updated.transcript_path or "none"),
        ]
    )


@app.command("auphonic-inspect")
def auphonic_inspect_command(
    production_id: Optional[str] = typer.Argument(None),
    session_uid: Optional[str] = typer.Option(
        None, "--session", help="Resolve production id from a tracked session."
    ),
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Inspect an Auphonic production without starting it."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, _logger = _bootstrap(config_path, debug=debug)

    resolved_production_id = production_id
    if session_uid is not None:
        session = get_session(runtime_paths.db_file, session_uid)
        if session is None:
            typer.secho(
                f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
            )
            raise typer.Exit(code=1)
        if not session.auphonic_production_id:
            typer.echo(f"Session has no Auphonic production id: {session_uid}")
            raise typer.Exit(code=1)
        resolved_production_id = session.auphonic_production_id

    if not resolved_production_id:
        typer.echo("Provide a production id or use --session <session_uid>.")
        raise typer.Exit(code=1)

    try:
        details = inspect_production(loaded_config, resolved_production_id)
    except AuphonicError as exc:
        typer.echo(f"Auphonic error: {exc}")
        raise typer.Exit(code=1)

    _print_header("Auphonic Production")
    _print_section(
        "Overview",
        [
            ("production_id", details["production_id"]),
            (
                "status",
                _styled(str(details["status"]), fg=typer.colors.WHITE, bold=True),
            ),
            ("status_string", details["status_string"] or "none"),
            ("start_allowed", str(details["start_allowed"])),
            ("change_allowed", str(details["change_allowed"])),
        ],
    )
    _print_section(
        "Links",
        [
            ("edit_page", details["edit_page"] or "none"),
            ("status_page", details["status_page"] or "none"),
        ],
    )
    _print_section(
        "Messages",
        [
            ("error_message", details["error_message"] or "none"),
            ("warning_message", details["warning_message"] or "none"),
        ],
    )
    if details["multi_input_files"]:
        typer.echo()
        _print_header("Tracks")
    for track in details["multi_input_files"]:
        typer.echo(
            "  ".join(
                [
                    f"id={track.get('id', '')}",
                    f"type={track.get('type', '')}",
                    f"input_file={track.get('input_file', '') or 'none'}",
                ]
            )
        )


@app.command("auphonic-sync")
def auphonic_sync_command(
    production_id: Optional[str] = typer.Argument(None),
    session_uid: Optional[str] = typer.Option(
        None, "--session", help="Tracked session to reconcile."
    ),
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Reconcile remote Auphonic state back into the local session DB."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)

    if session_uid is None:
        typer.echo("Use --session <session_uid> to choose the local session.")
        raise typer.Exit(code=1)

    session = get_session(runtime_paths.db_file, session_uid)
    if session is None:
        typer.secho(
            f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
        )
        raise typer.Exit(code=1)

    resolved_production_id = production_id or session.auphonic_production_id
    if not resolved_production_id:
        typer.echo("Provide a production id or sync a session that already stores one.")
        raise typer.Exit(code=1)

    try:
        updated = reconcile_production_to_session(
            config=loaded_config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            production_id=resolved_production_id,
            logger=logger,
        )
    except AuphonicError as exc:
        logger.error("Auphonic sync failed for %s: %s", session_uid, exc)
        typer.echo(f"Auphonic error: {exc}")
        raise typer.Exit(code=1)

    _print_header("Auphonic Synced")
    _print_key_values(
        [
            ("session_uid", updated.session_uid),
            ("status", _format_status(updated.status)),
            ("auphonic_production_id", updated.auphonic_production_id or "none"),
            ("pre_edit_master_path", updated.pre_edit_master_path or "none"),
            ("transcript_path", updated.transcript_path or "none"),
        ]
    )
