from pathlib import Path
import re
from typing import List, Optional
import webbrowser

import typer

from podline.artifacts import (
    ArtifactError,
    apply_workspace_cleanup,
    cache_path_for,
    cache_usage_bytes,
    clear_cache,
    create_workspace,
    plan_workspace_cleanup,
    workspace_path_for,
)
from podline.auphonic import (
    AuphonicError,
    inspect_production,
    offset_session_auphonic_production,
    prepare_session_with_auphonic,
    preview_prepared_session_with_auphonic,
    process_session_with_auphonic,
    reconcile_production_to_session,
    refresh_transcript_with_auphonic,
    reset_session_auphonic_production,
    resolve_stem_workspace_metadata,
    start_prepared_session_with_auphonic,
)
from podline.config import load_config
from podline.storage import redact_url, sha256_of_file, storage_health
from podline.cozytrack.client import CozytrackClient, CozytrackError
from podline.cozytrack.ingest import (
    CozytrackIngestError,
    format_session_track_table,
    ingest_session as cozytrack_ingest_session,
    list_ready_sessions_for_ingest,
)
from podline.db import initialize_database
from podline.episode_meta import (
    EpisodeMetaError,
    generate_episode_metadata,
)
from podline.finalize import (
    FinalizeError,
    finalize_session,
    get_episode_metadata,
)
from podline.health import read_health_snapshot, write_health_snapshot
from podline.ingest import IngestError, import_local_folder
from podline.logging_setup import configure_logging
from podline.paths import build_runtime_paths, ensure_runtime_dirs
from podline.podbean import PodbeanError, create_podbean_draft
from podline.repository import get_session, list_media_artifacts, list_sessions

app = typer.Typer(help="podline automation CLI.")

STATUS_STYLES = {
    "queued": ("Queued", typer.colors.YELLOW, "○"),
    "auphonic_prepared": ("Prepared", typer.colors.BLUE, "↑"),
    "auphonic_previewed": ("Previewed", typer.colors.BLUE, "◉"),
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
    if not items:
        typer.echo("  none")
        return
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


def _format_seconds(value: float) -> str:
    return f"{value:.3f}"


def _format_track_offset(value) -> str:
    if value is None or value == "":
        return "none"
    try:
        return _format_seconds(float(value))
    except (TypeError, ValueError):
        return str(value)


def _print_session_summary(session) -> None:
    _print_session_summary_row(session, index=None, full=False)


def _print_session_summary_row(session, *, index: Optional[int], full: bool) -> None:
    session_value = session.session_uid if full else _truncate(session.session_uid, 16)
    path_value = session.source_path if full else _truncate(session.source_path, 60)
    index_value = f"@{index}" if index is not None else ""
    typer.echo(
        "  ".join(
            [
                _styled(index_value.ljust(4), fg=typer.colors.YELLOW, bold=True),
                _styled(session_value.ljust(36 if full else 16), bold=True),
                _format_status(session.status).ljust(28),
                _truncate(session.recorded_at or "unknown", 25).ljust(25),
                _styled(
                    _truncate(session.source_basename or "unknown", 18).ljust(18),
                    fg=typer.colors.MAGENTA,
                ),
                path_value,
            ]
        )
    )


def _print_session_table(sessions, *, full: bool = False) -> None:
    typer.echo(
        "  ".join(
            [
                _styled("pick".ljust(4), fg=typer.colors.BRIGHT_BLUE, bold=True),
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
                "-" * 4,
                "-" * 16,
                "-" * 28,
                "-" * 25,
                "-" * 18,
                "-" * 40,
            ]
        )
    )
    for index, session in enumerate(sessions, start=1):
        _print_session_summary_row(session, index=index, full=full)


def _resolve_config_path(config: Optional[Path]) -> Path:
    if config is not None:
        return config.expanduser().resolve()
    return (Path.home() / "podline" / "config.yaml").resolve()


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


def _print_sessions(runtime_paths, *, full: bool = False) -> None:
    sessions = list_sessions(runtime_paths.db_file)

    if not sessions:
        typer.secho("☷ No sessions tracked yet.", fg=typer.colors.YELLOW, bold=True)
        return

    _print_session_table(sessions, full=full)


def _resolve_session_arg(runtime_paths, session_arg: str):
    if session_arg == "latest":
        sessions = list_sessions(runtime_paths.db_file)
        if not sessions:
            return None
        return sessions[0]

    if session_arg.startswith("@"):
        try:
            index = int(session_arg[1:])
        except ValueError:
            return None
        sessions = list_sessions(runtime_paths.db_file)
        if 1 <= index <= len(sessions):
            return sessions[index - 1]
        return None

    return get_session(runtime_paths.db_file, session_arg)


def _resolve_finalize_master(
    loaded_config, master: Optional[Path], *, workspace_dir: Optional[Path] = None
) -> Path:
    masters_dir = loaded_config.paths.deliverables_root / "masters"
    allowed_suffixes = {".mp3", ".wav", ".m4a", ".aif", ".aiff"}
    search_dirs = [
        directory
        for directory in (workspace_dir, masters_dir)
        if directory is not None and directory.exists()
    ]

    if master is not None:
        if not master.is_absolute() and master.parent == Path("."):
            cwd_candidate = master.resolve()
            if cwd_candidate.exists():
                return cwd_candidate

            episode_match = re.fullmatch(r"ep(\d+)", master.name.lower())
            if episode_match:
                episode_number = int(episode_match.group(1))
                candidates = sorted(
                    path
                    for directory in search_dirs
                    for path in directory.iterdir()
                    if path.is_file()
                    and path.suffix.lower() in allowed_suffixes
                    and re.fullmatch(
                        rf"sexdrunks_ep0*{episode_number}_master{re.escape(path.suffix)}",
                        path.name,
                        re.IGNORECASE,
                    )
                )
                if len(candidates) == 1:
                    return candidates[0]
                if len(candidates) > 1:
                    raise FinalizeError(
                        f"Multiple masters matched shortcut {master.name} in {', '.join(str(d) for d in search_dirs)}. Provide --master <filename> or --master <path>."
                    )

            for directory in search_dirs:
                candidate = directory / master.name
                if candidate.exists():
                    return candidate
        return master

    if not search_dirs:
        raise FinalizeError(
            f"No --master provided and neither the session workspace nor {masters_dir} exists. Provide --master <path>."
        )

    # The workspace also holds the downloaded pre-edit master, which must not
    # be picked up as the exported final master.
    candidates = sorted(
        path
        for directory in search_dirs
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix.lower() in allowed_suffixes
        and not (directory == workspace_dir and "pre_edit" in path.name.lower())
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FinalizeError(
            "No --master provided and no exported masters were found in "
            f"{', '.join(str(d) for d in search_dirs)}. Provide --master <path>."
        )
    raise FinalizeError(
        "No --master provided and multiple masters were found in "
        f"{', '.join(str(d) for d in search_dirs)}. Provide --master <filename> or --master <path>."
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
    """Retired: sessions now live in S3-backed storage."""
    typer.secho(
        "✖ pod refresh is retired: it rebuilt the database from drive scans, which "
        "would discard S3-native sessions.",
        fg=typer.colors.RED,
        bold=True,
    )
    typer.echo("Use 'pod ct-ingest <id>' for Cozytrack or 'pod import <folder>' for local recordings.")
    raise typer.Exit(code=1)


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
    storage_status = storage_health(loaded_config.storage)

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
            (
                "storage",
                _styled(storage_status, fg=typer.colors.GREEN, bold=True)
                if storage_status.startswith("ok")
                else _styled(storage_status, fg=typer.colors.RED, bold=True)
                if storage_status.startswith("error")
                else storage_status,
            ),
            ("cache_usage", _format_bytes(cache_usage_bytes(runtime_paths))),
            ("queue_depth", str(snapshot.queue_depth)),
            ("session_count", str(snapshot.session_count)),
            ("last_error", snapshot.last_error or "none"),
        ]
    )


@app.command("cache-clear")
def cache_clear(
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Empty the local artifact cache. Cached files re-download from S3 on demand."""
    config_path = _resolve_config_path(config)
    _loaded_config, runtime_paths, _logger = _bootstrap(config_path)
    freed = clear_cache(runtime_paths)

    _print_header("Cache Cleared")
    _print_key_values(
        [
            ("cache_dir", runtime_paths.cache_dir),
            ("freed", _format_bytes(freed)),
        ]
    )


@app.command("workspace-clean")
def workspace_clean(
    older_than_days: float = typer.Option(
        14,
        "--older-than-days",
        min=0,
        help="Only remove workspaces untouched for at least this many days.",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually delete eligible workspaces (dry run without it).",
    ),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Remove editing workspaces for released episodes after a retention window.

    Only sessions that reached draft_created are eligible, and only once
    nothing in the workspace has been modified for the retention window.
    Media re-materializes from S3 (pod workspace <session>); a Cubase project
    saved in the workspace is deleted with it.
    """
    config_path = _resolve_config_path(config)
    _loaded_config, runtime_paths, _logger = _bootstrap(config_path)
    try:
        candidates = plan_workspace_cleanup(
            runtime_paths,
            runtime_paths.db_file,
            older_than_days=older_than_days,
        )
    except ArtifactError as exc:
        typer.secho(f"✖ {exc}", fg=typer.colors.RED, bold=True)
        raise typer.Exit(code=1)

    _print_header("Workspace Cleanup" if apply else "Workspace Cleanup (dry run)")
    if not candidates:
        typer.echo("No workspaces found.")
        return

    for candidate in candidates:
        marker = (
            _styled("✖ removing" if apply else "✖ would remove", fg=typer.colors.RED, bold=True)
            if candidate.eligible
            else _styled("✓ keeping", fg=typer.colors.GREEN, bold=True)
        )
        typer.echo(
            f"{marker}  {candidate.session_uid}  "
            f"{_format_bytes(candidate.size_bytes)}  {candidate.reason}"
        )

    eligible = [candidate for candidate in candidates if candidate.eligible]
    if not eligible:
        typer.echo("Nothing eligible to remove.")
        return
    if not apply:
        typer.echo(
            f"{len(eligible)} workspace(s) eligible; re-run with --apply to delete."
        )
        return

    freed = apply_workspace_cleanup(candidates)
    _print_key_values(
        [
            ("removed", str(len(eligible))),
            ("freed", _format_bytes(freed)),
        ]
    )


def _artifact_uri(runtime_paths, session_uid: str, kind: str) -> str:
    artifacts = [
        artifact
        for artifact in list_media_artifacts(runtime_paths.db_file, session_uid)
        if artifact.kind == kind
    ]
    if not artifacts:
        return "none"
    return artifacts[0].s3_uri


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


@app.command("list")
def list_command(
    full: bool = typer.Option(
        False, "--full", help="Show full session ids and full paths."
    ),
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """List known sessions."""
    config_path = _resolve_config_path(config)
    _loaded_config, runtime_paths, _logger = _bootstrap(config_path, debug=debug)
    _print_sessions(runtime_paths, full=full)


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
    session = _resolve_session_arg(runtime_paths, session_uid)

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
            (
                "transcript_refresh_production_id",
                session.transcript_refresh_production_id or "none",
            ),
        ],
    )
    artifacts = list_media_artifacts(runtime_paths.db_file, session.session_uid)
    if artifacts:
        workspace_dir = workspace_path_for(runtime_paths, session.session_uid)
        rows = []
        for artifact in artifacts:
            label = (
                f"{artifact.kind}:{artifact.role}" if artifact.role else artifact.kind
            )
            cached = cache_path_for(
                runtime_paths, artifact.sha256, artifact.filename
            ).exists()
            # +workspace means "the current version is in the workspace",
            # not "a file by this name exists" — a transcript refresh leaves
            # a stale same-named copy behind, so verify content.
            workspace_copy = workspace_dir / artifact.filename
            in_workspace = (
                workspace_copy.exists()
                and sha256_of_file(workspace_copy) == artifact.sha256
            )
            location = "cached" if cached else "s3-only"
            if in_workspace:
                location += "+workspace"
            rows.append((label, f"{artifact.s3_uri}  [{location}]"))
        _print_section("Artifacts", rows)
    else:
        _print_section(
            "Legacy Paths",
            [
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
                        "Run pod episode-meta @n before Podbean draft",
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
    """Retired: use pod import <folder> to register local recordings."""
    typer.secho(
        "✖ pod scan is retired: drive-scan registration would create sessions the "
        "S3-backed pipeline cannot process.",
        fg=typer.colors.RED,
        bold=True,
    )
    typer.echo("Use 'pod import <folder>' to register a local recording folder.")
    raise typer.Exit(code=1)


@app.command("import")
def import_command(
    folder: Path = typer.Argument(..., help="Recording folder containing TrN track files."),
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Register a local recording folder (e.g. Riverside) into S3 storage."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)

    try:
        record, created = import_local_folder(
            config=loaded_config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            folder=folder,
            logger=logger,
        )
    except IngestError as exc:
        logger.error("Import failed for %s: %s", folder, exc)
        typer.echo(f"Import error: {exc}")
        raise typer.Exit(code=1)

    _print_header("Import Complete" if created else "Already Registered")
    _print_key_values(
        [
            ("folder", str(folder)),
            ("session_uid", record.session_uid),
            ("status", _format_status(record.status)),
            ("created", "yes" if created else "no (same content already registered)"),
        ]
    )


@app.command()
def workspace(
    session_uid: str,
    stems: bool = typer.Option(
        False,
        "--stems",
        help="Include Auphonic-processed individual FLAC tracks for repair editing.",
    ),
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Materialize a persistent editing folder, optionally with repair stems."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)
    session = _resolve_session_arg(runtime_paths, session_uid)
    if session is None:
        typer.secho(
            f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
        )
        raise typer.Exit(code=1)

    try:
        stem_metadata = (
            resolve_stem_workspace_metadata(
                config=loaded_config,
                db_file=runtime_paths.db_file,
                session=session,
            )
            if stems
            else None
        )
        workspace_dir = create_workspace(
            config=loaded_config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session_uid=session.session_uid,
            include_stems=stems,
            stem_metadata=stem_metadata,
        )
    except (ArtifactError, AuphonicError) as exc:
        logger.error("Workspace creation failed for %s: %s", session.session_uid, exc)
        typer.echo(f"Workspace error: {exc}")
        raise typer.Exit(code=1)

    contents = sorted(
        entry.name for entry in workspace_dir.iterdir() if entry.is_file()
    )
    _print_header("Workspace Ready")
    _print_key_values(
        [
            ("session_uid", session.session_uid),
            ("workspace", str(workspace_dir)),
            ("contents", ", ".join(contents) or "empty"),
            (
                "stems",
                str(len(list((workspace_dir / "stems").glob("*.flac"))))
                if stems
                else "not requested",
            ),
        ]
    )
    if stems:
        typer.secho(
            "⚠ Auphonic individual tracks are not loudness-normalized. "
            "Run a final loudness check on the Cubase master.",
            fg=typer.colors.YELLOW,
            bold=True,
        )


@app.command("finalize")
def finalize_command(
    session_uid: str,
    master: Optional[Path] = typer.Option(
        None,
        "--master",
        help="Path to the edited final master file, or just the filename if it is already in deliverables/masters.",
    ),
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Attach the final edited master and generate draft metadata."""
    config_path = _resolve_config_path(config)
    _loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)
    session = _resolve_session_arg(runtime_paths, session_uid)
    if session is None:
        typer.secho(
            f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
        )
        raise typer.Exit(code=1)

    try:
        resolved_master = _resolve_finalize_master(
            _loaded_config, master, workspace_dir=workspace_path_for(runtime_paths, session.session_uid)
        )
        updated = finalize_session(
            config=_loaded_config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            master_path=resolved_master,
        )
    except FinalizeError as exc:
        logger.error("Finalize failed for %s: %s", session_uid, exc)
        typer.echo(f"Finalize error: {exc}")
        raise typer.Exit(code=1)

    episode = get_episode_metadata(runtime_paths.db_file, updated.session_uid)
    final_master_uri = _artifact_uri(runtime_paths, updated.session_uid, "final_master")
    _print_header("Finalize Complete")
    _print_section(
        "Overview",
        [
            ("session_uid", updated.session_uid),
            ("status", _format_status(updated.status)),
            ("final_master", final_master_uri),
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
                        "Run pod episode-meta @n before Podbean draft",
                        fg=typer.colors.WHITE,
                    ),
                ),
            ],
        )


@app.command("episode-meta")
def episode_meta_command(
    session_uid: str,
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Generate episode title and description with the OpenAI API."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)
    session = _resolve_session_arg(runtime_paths, session_uid)
    if session is None:
        typer.secho(
            f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
        )
        raise typer.Exit(code=1)

    try:
        episode = generate_episode_metadata(
            config=loaded_config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
        )
    except EpisodeMetaError as exc:
        logger.error(
            "Episode metadata generation failed for %s: %s", session.session_uid, exc
        )
        typer.echo(f"Episode metadata error: {exc}")
        raise typer.Exit(code=1)

    _print_header("Episode Metadata")
    _print_section(
        "Draft",
        [
            ("session_uid", episode.session_uid),
            ("title", episode.title or "none"),
            ("description", episode.description or "none"),
        ],
    )


@app.command("transcript-refresh")
def transcript_refresh_command(
    session_uid: str,
    preset_id: Optional[str] = typer.Option(
        None,
        "--preset",
        help="Auphonic preset id or name to use for final-master transcript refresh.",
    ),
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Regenerate the transcript from the finalized master."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)
    session = _resolve_session_arg(runtime_paths, session_uid)
    if session is None:
        typer.secho(
            f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
        )
        raise typer.Exit(code=1)

    try:
        updated = refresh_transcript_with_auphonic(
            config=loaded_config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            preset_id=preset_id,
            logger=logger,
        )
    except (AuphonicError, ArtifactError) as exc:
        logger.error("Transcript refresh failed for %s: %s", session.session_uid, exc)
        typer.echo(f"Auphonic error: {exc}")
        raise typer.Exit(code=1)

    _print_header("Transcript Refreshed")
    _print_section(
        "Overview",
        [
            ("session_uid", updated.session_uid),
            ("status", _format_status(updated.status)),
            ("transcript", _artifact_uri(runtime_paths, updated.session_uid, "transcript")),
            ("final_master", _artifact_uri(runtime_paths, updated.session_uid, "final_master")),
        ],
    )


@app.command("podbean-draft")
def podbean_draft_command(
    session_uid: str,
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Upload the finalized master and create a Podbean draft."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)
    session = _resolve_session_arg(runtime_paths, session_uid)
    if session is None:
        typer.secho(
            f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
        )
        raise typer.Exit(code=1)

    try:
        episode = create_podbean_draft(
            config=loaded_config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
        )
    except PodbeanError as exc:
        logger.error(
            "Podbean draft creation failed for %s: %s", session.session_uid, exc
        )
        typer.echo(f"Podbean error: {exc}")
        raise typer.Exit(code=1)

    updated = get_session(runtime_paths.db_file, session.session_uid)
    _print_header("Podbean Draft")
    _print_section(
        "Overview",
        [
            ("session_uid", session.session_uid),
            ("status", _format_status(updated.status if updated else session.status)),
            ("episode_number", str(episode.episode_number or "none")),
            ("podbean_post_id", episode.podbean_post_id or "none"),
            ("draft_url", episode.draft_url or "none"),
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
    session = _resolve_session_arg(runtime_paths, session_uid)
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
    except (AuphonicError, ArtifactError) as exc:
        logger.error("Auphonic processing failed for %s: %s", session_uid, exc)
        typer.echo(f"Auphonic error: {exc}")
        raise typer.Exit(code=1)

    _print_header("Auphonic Complete")
    _print_key_values(
        [
            ("session_uid", updated.session_uid),
            ("status", _format_status(updated.status)),
            ("auphonic_production_id", updated.auphonic_production_id or "none"),
            ("pre_edit_master", _artifact_uri(runtime_paths, updated.session_uid, "pre_edit_master")),
            ("transcript", _artifact_uri(runtime_paths, updated.session_uid, "transcript")),
            ("workspace", str(workspace_path_for(runtime_paths, updated.session_uid))),
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
    session = _resolve_session_arg(runtime_paths, session_uid)
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
    except (AuphonicError, ArtifactError) as exc:
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
            ("next", f"pod auphonic-preview {session_uid}"),
        ]
    )
    # Show which file landed on which named preset track so a wrong pairing
    # is visible before anything starts. The session is already prepared at
    # this point, so a failure here is informational, never an exit code.
    try:
        details = inspect_production(loaded_config, updated.auphonic_production_id)
    except AuphonicError as exc:
        typer.echo(f"(attached-track display unavailable: {exc})")
        return
    attached = details.get("multi_input_files") or []
    if attached:
        _print_section(
            "Attached Tracks",
            [
                (
                    str(track.get("id") or "?"),
                    redact_url(str(track.get("input_file") or "none")),
                )
                for track in attached
            ],
        )


@app.command("auphonic-preview")
def auphonic_preview_command(
    session_uid: str,
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="Open the Auphonic edit page in the default browser.",
    ),
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Open the prepared Auphonic production for preview before starting."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)
    session = _resolve_session_arg(runtime_paths, session_uid)
    if session is None:
        typer.secho(
            f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
        )
        raise typer.Exit(code=1)

    try:
        preview = preview_prepared_session_with_auphonic(
            config=loaded_config,
            db_file=runtime_paths.db_file,
            session=session,
            logger=logger,
        )
    except AuphonicError as exc:
        logger.error("Auphonic preview failed for %s: %s", session_uid, exc)
        typer.echo(f"Auphonic error: {exc}")
        raise typer.Exit(code=1)

    details = preview.details
    edit_page = details["edit_page"] or ""
    browser_opened = False
    if open_browser and edit_page:
        browser_opened = webbrowser.open(edit_page)

    _print_header("Auphonic Preview")
    _print_key_values(
        [
            ("session_uid", preview.session.session_uid),
            ("status", _format_status(preview.session.status)),
            (
                "auphonic_production_id",
                preview.session.auphonic_production_id or "none",
            ),
            ("edit_page", edit_page or "none"),
            ("status_page", details["status_page"] or "none"),
            ("browser_opened", "yes" if browser_opened else "no"),
            ("next", f"pod auphonic-start {session_uid}"),
        ]
    )
    _print_auphonic_tracks(details["multi_input_files"])


@app.command("auphonic-reset")
def auphonic_reset_command(
    session_uid: str,
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Abandon the prepared Auphonic production and requeue the session.

    Use after track roles or durations changed (e.g. an ingest role
    reconciliation): the abandoned production keeps its old wiring, so
    requeue and run auphonic-prepare/offset/preview again.
    """
    config_path = _resolve_config_path(config)
    _loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)
    session = _resolve_session_arg(runtime_paths, session_uid)
    if session is None:
        typer.secho(
            f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
        )
        raise typer.Exit(code=1)

    abandoned_production_id = session.auphonic_production_id or "none"
    try:
        refreshed = reset_session_auphonic_production(
            db_file=runtime_paths.db_file,
            session=session,
            logger=logger,
        )
    except AuphonicError as exc:
        logger.error("Auphonic reset failed for %s: %s", session_uid, exc)
        typer.echo(f"Auphonic error: {exc}")
        raise typer.Exit(code=1)

    _print_header("Auphonic Reset")
    _print_key_values(
        [
            ("session_uid", refreshed.session_uid),
            ("status", _format_status(refreshed.status)),
            ("abandoned_production", abandoned_production_id),
            ("next", f"pod auphonic-prepare {refreshed.session_uid}"),
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
    session = _resolve_session_arg(runtime_paths, session_uid)
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
    except (AuphonicError, ArtifactError) as exc:
        logger.error("Auphonic start failed for %s: %s", session_uid, exc)
        typer.echo(f"Auphonic error: {exc}")
        raise typer.Exit(code=1)

    _print_header("Auphonic Started")
    _print_key_values(
        [
            ("session_uid", updated.session_uid),
            ("status", _format_status(updated.status)),
            ("auphonic_production_id", updated.auphonic_production_id or "none"),
            ("pre_edit_master", _artifact_uri(runtime_paths, updated.session_uid, "pre_edit_master")),
            ("transcript", _artifact_uri(runtime_paths, updated.session_uid, "transcript")),
            ("workspace", str(workspace_path_for(runtime_paths, updated.session_uid))),
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
        session = _resolve_session_arg(runtime_paths, session_uid)
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
        _print_auphonic_tracks(details["multi_input_files"])


def _print_auphonic_tracks(tracks) -> None:
    if not tracks:
        return
    typer.echo()
    _print_header("Tracks")
    for track in tracks:
        typer.echo(
            "  ".join(
                [
                    f"id={track.get('id', '')}",
                    f"type={track.get('type', '')}",
                    f"input_file={track.get('input_file', '') or 'none'}",
                    f"offset={_format_track_offset(track.get('offset'))}",
                ]
            )
        )


@app.command("auphonic-offset")
def auphonic_offset_command(
    session_uid: str,
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Apply the computed offset to the prepared Auphonic production.",
    ),
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Compute and optionally apply a duration-based Auphonic track offset."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)
    session = _resolve_session_arg(runtime_paths, session_uid)
    if session is None:
        typer.secho(
            f"✖ Session not found: {session_uid}", fg=typer.colors.RED, bold=True
        )
        raise typer.Exit(code=1)

    try:
        result = offset_session_auphonic_production(
            config=loaded_config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            apply=apply,
        )
    except AuphonicError as exc:
        logger.error("Auphonic offset failed for %s: %s", session_uid, exc)
        typer.echo(f"Auphonic error: {exc}")
        raise typer.Exit(code=1)

    plan = result.plan
    _print_header("Auphonic Offset Applied" if result.applied else "Auphonic Offset Plan")
    _print_section(
        "Durations",
        [
            (
                item.asset.filename,
                f"{_format_seconds(item.duration_seconds)}s  role={item.asset.role}",
            )
            for item in plan.asset_durations
        ],
    )
    _print_section(
        "Reference",
        [
            ("session_uid", session.session_uid),
            ("production_id", plan.production_id),
            (
                "local_assets",
                ", ".join(asset.filename for asset in plan.local_assets)
                or "not validated",
            ),
            ("reference_asset", plan.reference_asset.filename),
            ("applied", "yes" if result.applied else "no"),
        ],
    )
    _print_section(
        "Targets",
        [
            (
                target.asset.filename,
                "  ".join(
                    [
                        f"track_id={target.track_id}",
                        f"input_file={target.input_file or 'none'}",
                        f"offset_seconds={_format_seconds(target.offset_seconds)}",
                    ]
                ),
            )
            for target in plan.targets
        ],
    )
    if plan.aligned_assets:
        _print_section(
            "Already Aligned",
            [
                (asset.filename, "offset_seconds=0  update=skipped")
                for asset in plan.aligned_assets
            ],
        )
    if result.track_states:
        _print_section(
            "Updated Tracks",
            [
                (
                    str(track_state.get("id", "none")),
                    "  ".join(
                        [
                            f"type={track_state.get('type', 'none')}",
                            f"input_file={track_state.get('input_file') or 'none'}",
                            f"offset={_format_track_offset(track_state.get('offset'))}",
                        ]
                    ),
                )
                for track_state in result.track_states
            ],
        )


@app.command("ct-list")
def ct_list_command(
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """List Cozytrack sessions ready to ingest (excludes already-ingested)."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)

    try:
        sessions = list_ready_sessions_for_ingest(
            config=loaded_config,
            db_file=runtime_paths.db_file,
        )
    except CozytrackError as exc:
        logger.error("Cozytrack list failed: %s", exc)
        typer.echo(f"Cozytrack error: {exc}")
        raise typer.Exit(code=1)

    _print_header("Cozytrack Ready Sessions")
    if not sessions:
        typer.secho(
            "☷ No new Cozytrack sessions ready for ingest.",
            fg=typer.colors.YELLOW,
            bold=True,
        )
        return

    typer.echo(
        "  ".join(
            [
                _styled("cozytrack_id".ljust(36), fg=typer.colors.BRIGHT_BLUE, bold=True),
                _styled("name".ljust(28), fg=typer.colors.BRIGHT_BLUE, bold=True),
                _styled("tracks".ljust(6), fg=typer.colors.BRIGHT_BLUE, bold=True),
                _styled("finalized_at", fg=typer.colors.BRIGHT_BLUE, bold=True),
            ]
        )
    )
    typer.echo("  ".join(["-" * 36, "-" * 28, "-" * 6, "-" * 25]))
    for session in sessions:
        typer.echo(
            "  ".join(
                [
                    _truncate(session.id, 36).ljust(36),
                    _truncate(session.name or "(unnamed)", 28).ljust(28),
                    str(len(session.tracks)).ljust(6),
                    session.finalized_at or "unknown",
                ]
            )
        )


@app.command("ct-ingest")
def ct_ingest_command(
    cozytrack_id: str,
    name: Optional[str] = typer.Option(
        None, "--name", help="Override the session name when building the folder slug."
    ),
    exclude: Optional[List[str]] = typer.Option(
        None,
        "--exclude",
        help="Exclude a track by displayed role or exact Cozytrack track id. Repeat as needed.",
    ),
    list_tracks: bool = typer.Option(
        False,
        "--list-tracks",
        help="Show track roles, participants, durations, start times, and ids without downloading.",
    ),
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
) -> None:
    """Download a Cozytrack session's tracks and register it in podline."""
    config_path = _resolve_config_path(config)
    loaded_config, runtime_paths, logger = _bootstrap(config_path, debug=debug)

    if list_tracks:
        try:
            session = CozytrackClient(loaded_config).get_session(cozytrack_id)
        except CozytrackError as exc:
            logger.error("Cozytrack track list failed for %s: %s", cozytrack_id, exc)
            typer.echo(f"Cozytrack error: {exc}")
            raise typer.Exit(code=1)
        _print_header("Cozytrack Tracks")
        typer.echo(format_session_track_table(session))
        typer.echo()
        typer.echo(
            "Exclude an irrelevant track with "
            "'--exclude <role-or-track-id>'; repeat the option as needed."
        )
        return

    try:
        record = cozytrack_ingest_session(
            config=loaded_config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            cozytrack_session_id=cozytrack_id,
            name_override=name,
            excluded_track_selectors=exclude or [],
            logger=logger,
        )
    except (CozytrackError, CozytrackIngestError) as exc:
        logger.error("Cozytrack ingest failed for %s: %s", cozytrack_id, exc)
        typer.echo(f"Cozytrack error: {exc}")
        raise typer.Exit(code=1)

    artifacts = list_media_artifacts(runtime_paths.db_file, record.session_uid)
    _print_header("Cozytrack Ingest Complete")
    _print_key_values(
        [
            ("cozytrack_id", cozytrack_id),
            ("session_uid", record.session_uid),
            ("artifacts", str(len(artifacts))),
            ("status", _format_status(record.status)),
        ]
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

    session = _resolve_session_arg(runtime_paths, session_uid)
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
    except (AuphonicError, ArtifactError) as exc:
        logger.error("Auphonic sync failed for %s: %s", session_uid, exc)
        typer.echo(f"Auphonic error: {exc}")
        raise typer.Exit(code=1)

    _print_header("Auphonic Synced")
    _print_key_values(
        [
            ("session_uid", updated.session_uid),
            ("status", _format_status(updated.status)),
            ("auphonic_production_id", updated.auphonic_production_id or "none"),
            ("pre_edit_master", _artifact_uri(runtime_paths, updated.session_uid, "pre_edit_master")),
            ("transcript", _artifact_uri(runtime_paths, updated.session_uid, "transcript")),
            (
                "pre_edit_stems",
                str(
                    len(
                        [
                            artifact
                            for artifact in list_media_artifacts(
                                runtime_paths.db_file, updated.session_uid
                            )
                            if artifact.kind == "pre_edit_stem"
                        ]
                    )
                ),
            ),
        ]
    )
