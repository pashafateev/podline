from pathlib import Path
import re
import shutil
import subprocess

from podline.artifacts import ArtifactError, materialize_artifact
from podline.config import AppConfig
from podline.models import (
    ArtifactKind,
    EpisodeRecord,
    RuntimePaths,
    SessionRecord,
    SessionStatus,
)
from podline.repository import upsert_episode_metadata

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class EpisodeMetaError(RuntimeError):
    pass


def generate_episode_metadata_with_opencode(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session: SessionRecord,
) -> EpisodeRecord:
    if session.status not in {
        SessionStatus.READY_FOR_DRAFT.value,
        SessionStatus.DRAFT_CREATED.value,
    }:
        raise EpisodeMetaError(
            f"Session {session.session_uid} must be ready_for_draft before generating episode metadata"
        )

    try:
        transcript_path = materialize_artifact(
            config=config,
            runtime_paths=runtime_paths,
            db_file=db_file,
            session_uid=session.session_uid,
            kind=ArtifactKind.TRANSCRIPT.value,
        )
    except ArtifactError as exc:
        raise EpisodeMetaError(str(exc)) from exc

    opencode_binary = _resolve_opencode_binary()
    project_dir = PROJECT_ROOT
    transcript_text = transcript_path.read_text(encoding="utf-8-sig")
    output = _run_opencode_episode_meta(opencode_binary, project_dir, transcript_text)
    title, description = _parse_episode_meta_output(output)
    return upsert_episode_metadata(
        db_file,
        session_uid=session.session_uid,
        title=title,
        description=description,
    )


def _resolve_opencode_binary() -> str:
    resolved = shutil.which("opencode")
    if resolved is None:
        raise EpisodeMetaError("opencode is required for episode metadata generation")
    return resolved


def _run_opencode_episode_meta(
    opencode_binary: str, project_dir: Path, transcript_text: str
) -> str:
    command = [
        opencode_binary,
        "run",
        "--dir",
        str(project_dir),
        "--command",
        "episode-meta",
        transcript_text,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise EpisodeMetaError(
            f"opencode episode-meta failed with exit code {result.returncode}: {result.stderr[:300]}"
        )
    return result.stdout.strip()


def _parse_episode_meta_output(output: str) -> tuple[str, str]:
    title_match = re.search(r"\*\*Title:\*\*\s*(.+)", output)
    description_match = re.search(r"\*\*Description:\*\*\s*(.+)", output, re.DOTALL)
    if not title_match or not description_match:
        raise EpisodeMetaError(
            "Could not parse title/description from opencode episode-meta output"
        )
    title = title_match.group(1).strip()
    description = description_match.group(1).strip()
    if not title or not description:
        raise EpisodeMetaError(
            "opencode episode-meta returned empty title or description"
        )
    return title, description
