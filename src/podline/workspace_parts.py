"""Part-aware editing materialization; no processing or media assembly happens here."""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from podline.artifacts import (
    ArtifactError,
    _copy_workspace_artifact,
    _materialize_workspace_stems,
    _read_json_object,
    _remove_obsolete_stem_workspace_outputs,
    _stem_workspace_matches_registered,
    _workspace_target,
    materialize_artifact_record,
    workspace_path_for,
)
from podline.config import AppConfig
from podline.models import ArtifactKind, RecordingPartRecord, RuntimePaths
from podline.repository import get_media_artifact, list_media_artifacts
from podline.storage import MediaStorage


_OUTPUT_KINDS = (ArtifactKind.PRE_EDIT_MASTER.value, ArtifactKind.TRANSCRIPT.value)


def part_manifest_entry(
    part: RecordingPartRecord,
    *,
    artifacts: Dict,
    stems_dir: str,
    repair_manifest: str,
    pending: Optional[List[str]] = None,
) -> Dict:
    return {
        "part_id": part.part_id,
        "part_number": part.part_number,
        "take_id": part.take_id,
        "status": part.status,
        "ingest_complete": part.ingest_complete,
        "auphonic_production_id": part.auphonic_production_id,
        "operation": part.operation,
        "failure_reason": part.failure_reason,
        "output_basename": part.output_basename,
        "started_at": part.started_at,
        "stopped_at": part.stopped_at,
        "take_status": part.take_status,
        "source_snapshot": part.source_snapshot,
        "timing_warnings": part.timing_warnings,
        "artifacts": artifacts,
        "sources": [],
        "stems_dir": stems_dir,
        "repair_manifest": repair_manifest,
        "pending": pending if pending is not None else (
            [] if part.ingest_complete else ["ingest"]
        ),
    }


def create_part_workspace(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session_uid: str,
    parts: List[RecordingPartRecord],
    storage: Optional[MediaStorage],
    include_stems: bool,
    stem_metadata: Dict,
    expected_auphonic_production_id: Optional[str],
    include_sources: bool,
    part_number: Optional[int],
) -> Path:
    parts = sorted(parts, key=lambda part: part.part_number)
    if not parts:
        raise ArtifactError(f"No recording parts registered for session {session_uid}")
    selected = [part for part in parts if part_number in (None, part.part_number)]
    if not selected:
        raise ArtifactError(f"Session {session_uid} has no Part {part_number}")
    if expected_auphonic_production_id is not None and len(selected) != 1:
        raise ArtifactError(
            "Select --part when validating one expected Auphonic production"
        )

    workspace = workspace_path_for(runtime_paths, session_uid)
    manifest_path = _workspace_target(workspace, "manifest.json")
    workspace.mkdir(parents=True, exist_ok=True)
    previous = _read_json_object(manifest_path)
    previous_parts = {
        item["part_id"]: item
        for item in previous.get("parts", [])
        if isinstance(item, dict) and item.get("part_id")
    }
    single_part = len(parts) == 1
    entries = []
    ready = True
    for part in parts:
        outputs = {
            kind: get_media_artifact(
                db_file, session_uid, kind=kind, part_id=part.part_id,
            )
            for kind in _OUTPUT_KINDS
        }
        # The normal single-part workflow exposes the episode's current VTT.
        # Multipart never consults that alias: it may be the final edited cut.
        if single_part:
            current_transcript = get_media_artifact(
                db_file, session_uid, kind=ArtifactKind.TRANSCRIPT.value, part_id=None,
            )
            if current_transcript is not None:
                outputs[ArtifactKind.TRANSCRIPT.value] = current_transcript
        pending = ([] if part.ingest_complete else ["ingest"]) + [
            kind for kind, artifact in outputs.items() if artifact is None
        ]
        if not single_part:
            pending.extend(
                f"current_{kind}" for kind, artifact in outputs.items()
                if artifact is not None and (
                    not part.auphonic_production_id
                    or artifact.source_id != part.auphonic_production_id
                )
            )
            if not pending and part.status != "complete":
                pending.append("processing")
        ready = ready and not pending
        old = previous_parts.get(part.part_id, {})
        # Adopt an old, flat manifest without renaming or moving Part 1's media.
        if not old and part.part_number == 1 and previous.get("artifacts"):
            old = {
                "artifacts": previous["artifacts"],
                "stems_dir": "stems",
                "repair_manifest": "repair-manifest.json",
            }
        if part not in selected and part.part_id in previous_parts:
            # These hashes describe the files actually materialized by a prior
            # refresh, not newer DB outputs that --part deliberately skipped.
            entries.append(previous_parts[part.part_id])
            continue
        flat_stems = single_part or old.get("stems_dir") == "stems" or (
            part.part_number == 1 and (
                part.output_basename in (None, session_uid)
                or (workspace / "repair-manifest.json").exists()
            )
        )
        stems_dir = "stems" if flat_stems else f"stems/part-{part.part_number:02d}"
        repair_manifest = (
            "repair-manifest.json" if flat_stems
            else f"repair-manifest-part-{part.part_number:02d}.json"
        )
        artifacts = {}
        for kind, artifact in outputs.items():
            if artifact is None:
                continue
            old_output = old.get("artifacts", {}).get(kind, {})
            filename = old_output.get("filename") or artifact.filename
            _workspace_target(workspace, filename)
            if part in selected:
                cached = materialize_artifact_record(
                    config=config, runtime_paths=runtime_paths,
                    artifact=artifact, storage=storage,
                )
                _copy_workspace_artifact(
                    workspace, filename, cached, artifact.sha256,
                    managed=bool(old_output),
                )
            artifacts[kind] = {
                "filename": filename,
                "sha256": artifact.sha256,
                "s3_uri": artifact.s3_uri,
                "source_provider": artifact.source_provider,
                "source_id": artifact.source_id,
                "materialized": part in selected,
            }
        entry = part_manifest_entry(
            part, artifacts=artifacts, stems_dir=stems_dir,
            repair_manifest=repair_manifest, pending=pending,
        )
        entry["sources"] = old.get("sources", [])
        if part in selected:
            if include_sources:
                entry["sources"] = _materialize_sources(
                    config=config, runtime_paths=runtime_paths,
                    db_file=db_file, session_uid=session_uid,
                    part=part, workspace=workspace,
                    previous=entry["sources"], storage=storage,
                )
            registered_stems = list_media_artifacts(
                db_file, session_uid, kind=ArtifactKind.PRE_EDIT_STEM.value,
                part_id=part.part_id,
            )
            production_id = (
                expected_auphonic_production_id or part.auphonic_production_id
            )
            if not registered_stems or (
                not include_stems and not _stem_workspace_matches_registered(
                    workspace, registered_stems,
                    current_production_id=production_id,
                    stems_subdir=stems_dir,
                    repair_manifest_name=repair_manifest,
                    timing_warnings=part.timing_warnings,
                )
            ):
                _remove_obsolete_stem_workspace_outputs(
                    workspace, stems_subdir=stems_dir,
                    repair_manifest_name=repair_manifest,
                )
            if include_stems and registered_stems:
                _materialize_workspace_stems(
                    config=config, runtime_paths=runtime_paths,
                    db_file=db_file, session_uid=session_uid,
                    workspace=workspace, storage=storage,
                    stem_metadata=(
                        stem_metadata if single_part
                        else stem_metadata.get(part.part_id, {})
                    ),
                    part_id=part.part_id, part_number=part.part_number,
                    stems_subdir=stems_dir, repair_manifest_name=repair_manifest,
                    timing_warnings=part.timing_warnings,
                    expected_auphonic_production_id=production_id,
                )
        entries.append(entry)

    manifest = {
        "session_uid": session_uid,
        "parts": entries,
        "ready_for_finalize": bool(ready),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if single_part:
        manifest["artifacts"] = entries[0]["artifacts"]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return workspace


def _source_token(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")[:80] or "source"
    if safe != value:
        safe += "-" + hashlib.sha256(value.encode()).hexdigest()[:12]
    return safe


def _materialize_sources(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session_uid: str,
    part: RecordingPartRecord,
    workspace: Path,
    previous: List[Dict],
    storage: Optional[MediaStorage],
) -> List[Dict]:
    sources = list_media_artifacts(
        db_file, session_uid, kind=ArtifactKind.RAW_TRACK.value, part_id=part.part_id,
    )
    previous_files = {
        item["filename"] for item in previous
        if isinstance(item, dict) and item.get("filename")
    }
    prefix = f"sources/part-{part.part_number:02d}/"
    entries = []
    for source in sorted(sources, key=lambda item: (item.role, item.source_id or "")):
        source_id = source.source_id or Path(source.filename).stem
        extension = Path(source.filename).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", extension):
            extension = ".bin"
        filename = (
            prefix + _source_token(source.role or "track")
            + "-" + _source_token(source_id) + extension
        )
        cached = materialize_artifact_record(
            config=config, runtime_paths=runtime_paths,
            artifact=source, storage=storage,
        )
        _copy_workspace_artifact(
            workspace, filename, cached, source.sha256,
            managed=filename in previous_files,
        )
        entries.append({
            "role": source.role,
            "source_provider": source.source_provider,
            "source_id": source.source_id,
            "filename": filename,
            "original_filename": source.filename,
            "sha256": source.sha256,
            "s3_uri": source.s3_uri,
        })
    current_files = {item["filename"] for item in entries}
    for stale in previous_files - current_files:
        # Only this part's manifest-owned copies may be removed. Never traverse
        # sibling parts or delete an editor's repairs saved beside the sources.
        if stale.startswith(prefix):
            path = _workspace_target(workspace, stale)
            if path.is_file():
                path.unlink()
    return entries
