from importlib import resources
import os
from typing import Any, Optional

from openai import APITimeoutError, OpenAI, OpenAIError
from pydantic import BaseModel, Field, ValidationError, field_validator

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

EPISODE_METADATA_PROMPT = (
    resources.files("podline")
    .joinpath("prompts/episode_metadata.md")
    .read_text(encoding="utf-8")
)


class EpisodeMetaError(RuntimeError):
    pass


class EpisodeMetadataDraft(BaseModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)

    @field_validator("title", "description")
    @classmethod
    def reject_empty_fields(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value


def generate_episode_metadata(
    *,
    config: AppConfig,
    runtime_paths: RuntimePaths,
    db_file: str,
    session: SessionRecord,
    client: Optional[Any] = None,
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

    transcript_text = transcript_path.read_text(encoding="utf-8-sig")
    openai_client = client or _build_openai_client(config)
    draft = _request_episode_metadata(
        client=openai_client,
        config=config,
        transcript_text=transcript_text,
    )
    return upsert_episode_metadata(
        db_file,
        session_uid=session.session_uid,
        title=draft.title,
        description=draft.description,
    )


def _build_openai_client(config: AppConfig) -> OpenAI:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise EpisodeMetaError(
            "OPENAI_API_KEY is required for episode metadata generation"
        )
    return OpenAI(
        api_key=api_key,
        timeout=config.episode_metadata.request_timeout_seconds,
    )


def _request_episode_metadata(
    *,
    client: Any,
    config: AppConfig,
    transcript_text: str,
) -> EpisodeMetadataDraft:
    try:
        response = client.responses.parse(
            model=config.episode_metadata.model,
            input=[
                {
                    "role": "developer",
                    "content": EPISODE_METADATA_PROMPT,
                },
                {
                    "role": "user",
                    "content": transcript_text,
                },
            ],
            text_format=EpisodeMetadataDraft,
            reasoning={"effort": config.episode_metadata.reasoning_effort},
            store=False,
            tools=[],
        )
    except APITimeoutError as exc:
        raise EpisodeMetaError(
            "OpenAI episode metadata request timed out after "
            f"{config.episode_metadata.request_timeout_seconds} seconds"
        ) from exc
    except OpenAIError as exc:
        raise EpisodeMetaError(f"OpenAI API request failed: {exc}") from exc
    except ValidationError as exc:
        raise _invalid_structured_output_error() from exc

    status = getattr(response, "status", None)
    if status != "completed":
        detail = _response_failure_detail(response)
        raise EpisodeMetaError(
            f"OpenAI episode metadata response was {status or 'not completed'}{detail}"
        )

    for output in getattr(response, "output", []):
        if getattr(output, "type", None) != "message":
            continue
        for item in getattr(output, "content", []):
            if getattr(item, "type", None) == "refusal":
                refusal = getattr(item, "refusal", "No refusal reason provided")
                raise EpisodeMetaError(
                    f"OpenAI refused episode metadata generation: {refusal}"
                )
            parsed = getattr(item, "parsed", None)
            if parsed is not None:
                try:
                    return EpisodeMetadataDraft.model_validate(parsed)
                except ValidationError as exc:
                    raise _invalid_structured_output_error() from exc

    raise EpisodeMetaError(
        "OpenAI episode metadata response was missing parsed structured output"
    )


def _invalid_structured_output_error() -> EpisodeMetaError:
    return EpisodeMetaError(
        "OpenAI episode metadata response contained invalid structured output: "
        "title and description must not be empty or whitespace-only"
    )


def _response_failure_detail(response: Any) -> str:
    incomplete_details = getattr(response, "incomplete_details", None)
    reason = getattr(incomplete_details, "reason", None)
    if reason:
        return f": {reason}"
    error = getattr(response, "error", None)
    message = getattr(error, "message", None)
    if message:
        return f": {message}"
    return ""
