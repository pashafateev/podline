from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import httpx
from openai import APITimeoutError, OpenAIError
import pytest

from podline.episode_meta import (
    EPISODE_METADATA_PROMPT,
    EpisodeMetadataDraft,
    EpisodeMetaError,
    generate_episode_metadata,
)
from podline.repository import get_episode, upsert_episode_metadata


class FakeResponses:
    def __init__(self, response=None, error: Optional[Exception] = None) -> None:
        self.response = response
        self.error = error
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeOpenAIClient:
    def __init__(self, response=None, error: Optional[Exception] = None) -> None:
        self.responses = FakeResponses(response=response, error=error)


def _parsed_response(
    *,
    title: str = "Boba Boom",
    description: str = "Pasha and J. debate boba tea and trends.",
):
    return SimpleNamespace(
        status="completed",
        incomplete_details=None,
        output=[
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        type="output_text",
                        parsed=EpisodeMetadataDraft(
                            title=title,
                            description=description,
                        ),
                    )
                ],
            )
        ],
    )


def _response_with_content(content, *, status: str = "completed", reason=None):
    incomplete_details = (
        SimpleNamespace(reason=reason) if reason is not None else None
    )
    return SimpleNamespace(
        status=status,
        incomplete_details=incomplete_details,
        output=[SimpleNamespace(type="message", content=content)],
    )


def _ready_session_with_transcript(
    *,
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path: Path,
    session_uid: str,
):
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = session_factory(
        runtime_paths.db_file,
        session_uid=session_uid,
        status="ready_for_draft",
    )
    transcript = tmp_path / f"{session_uid}.vtt"
    transcript.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nhello from the current cut\n",
        encoding="utf-8",
    )
    artifact_registrar(
        runtime_paths.db_file,
        runtime_paths,
        session_uid=session.session_uid,
        kind="transcript",
        path=transcript,
    )
    return config, runtime_paths, session


def test_episode_meta_prompt_preserves_editorial_contract() -> None:
    assert "2–7 words" in EPISODE_METADATA_PROMPT
    assert "recurring transcript themes, phrases, or images" in EPISODE_METADATA_PROMPT
    assert "Slightly edgy and memorable, but newcomer-safe" in EPISODE_METADATA_PROMPT
    assert "Humor is secondary to the episode's meaning" in EPISODE_METADATA_PROMPT
    assert "2–4 conversational sentences" in EPISODE_METADATA_PROMPT
    assert "Use `J.`" in EPISODE_METADATA_PROMPT
    assert "Do not include the episode number" in EPISODE_METADATA_PROMPT
    assert "Jonathan" not in EPISODE_METADATA_PROMPT
    assert "feed.podbean.com" not in EPISODE_METADATA_PROMPT
    assert "pbcopy" not in EPISODE_METADATA_PROMPT
    assert "$ARGUMENTS" not in EPISODE_METADATA_PROMPT


def test_generate_episode_metadata_sends_current_transcript_and_persists_result_unchanged(
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path: Path,
) -> None:
    config, runtime_paths, session = _ready_session_with_transcript(
        app_config_factory=app_config_factory,
        runtime_factory=runtime_factory,
        session_factory=session_factory,
        artifact_registrar=artifact_registrar,
        tmp_path=tmp_path,
        session_uid="session-meta-1",
    )
    title = "  White-Knuckling Toward Surrender  "
    description = "Pasha and J. follow the episode's recurring images without sanding them down."
    client = FakeOpenAIClient(
        response=_parsed_response(title=title, description=description)
    )

    episode = generate_episode_metadata(
        config=config,
        runtime_paths=runtime_paths,
        db_file=runtime_paths.db_file,
        session=session,
        client=client,
    )

    assert episode.title == title
    assert episode.description == description
    saved = get_episode(runtime_paths.db_file, session.session_uid)
    assert saved is not None
    assert saved.title == title
    assert saved.description == description

    assert len(client.responses.calls) == 1
    request = client.responses.calls[0]
    assert request["model"] == "gpt-5.6"
    assert request["reasoning"] == {"effort": "low"}
    assert request["text_format"] is EpisodeMetadataDraft
    assert request["store"] is False
    assert request["tools"] == []
    assert request["input"][0] == {
        "role": "developer",
        "content": EPISODE_METADATA_PROMPT,
    }
    assert request["input"][1]["role"] == "user"
    assert request["input"][1]["content"].startswith("WEBVTT")
    assert "hello from the current cut" in request["input"][1]["content"]
    assert "background" not in request
    assert "conversation" not in request
    assert "previous_response_id" not in request


@pytest.mark.parametrize("status", ["queued", "waiting_for_finalize"])
def test_generate_episode_metadata_preserves_session_status_restrictions(
    app_config_factory,
    runtime_factory,
    session_factory,
    tmp_path: Path,
    status: str,
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = session_factory(
        runtime_paths.db_file,
        session_uid=f"session-meta-{status}",
        status=status,
    )
    client = FakeOpenAIClient(response=_parsed_response())

    with pytest.raises(EpisodeMetaError, match="must be ready_for_draft"):
        generate_episode_metadata(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            client=client,
        )

    assert client.responses.calls == []


def test_generate_episode_metadata_requires_transcript_artifact(
    app_config_factory, runtime_factory, session_factory, tmp_path: Path
) -> None:
    config = app_config_factory(tmp_path)
    runtime_paths = runtime_factory(config)
    session = session_factory(
        runtime_paths.db_file,
        session_uid="session-meta-2",
        status="ready_for_draft",
    )

    with pytest.raises(EpisodeMetaError, match="No transcript artifact"):
        generate_episode_metadata(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            client=FakeOpenAIClient(response=_parsed_response()),
        )


def test_generate_episode_metadata_requires_openai_api_key(
    monkeypatch,
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path: Path,
) -> None:
    config, runtime_paths, session = _ready_session_with_transcript(
        app_config_factory=app_config_factory,
        runtime_factory=runtime_factory,
        session_factory=session_factory,
        artifact_registrar=artifact_registrar,
        tmp_path=tmp_path,
        session_uid="session-meta-no-key",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(EpisodeMetaError, match="OPENAI_API_KEY"):
        generate_episode_metadata(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
        )


@pytest.mark.parametrize(
    ("client", "message"),
    [
        (
            FakeOpenAIClient(error=OpenAIError("service unavailable")),
            "OpenAI API request failed",
        ),
        (
            FakeOpenAIClient(
                error=APITimeoutError(
                    request=httpx.Request("POST", "https://api.openai.com/v1/responses")
                )
            ),
            "timed out after 120 seconds",
        ),
        (
            FakeOpenAIClient(
                response=_response_with_content(
                    [SimpleNamespace(type="refusal", refusal="Cannot help with that.")]
                )
            ),
            "refused",
        ),
        (
            FakeOpenAIClient(
                response=_response_with_content(
                    [SimpleNamespace(type="output_text", parsed=None)]
                )
            ),
            "missing parsed",
        ),
        (
            FakeOpenAIClient(
                response=_response_with_content(
                    [],
                    status="incomplete",
                    reason="max_output_tokens",
                )
            ),
            "incomplete",
        ),
    ],
)
def test_generation_failures_do_not_overwrite_existing_metadata(
    app_config_factory,
    runtime_factory,
    session_factory,
    artifact_registrar,
    tmp_path: Path,
    client: FakeOpenAIClient,
    message: str,
) -> None:
    config, runtime_paths, session = _ready_session_with_transcript(
        app_config_factory=app_config_factory,
        runtime_factory=runtime_factory,
        session_factory=session_factory,
        artifact_registrar=artifact_registrar,
        tmp_path=tmp_path,
        session_uid=f"session-meta-failure-{message.replace(' ', '-')}",
    )
    original = upsert_episode_metadata(
        runtime_paths.db_file,
        session_uid=session.session_uid,
        title="Existing title",
        description="Existing description.",
    )

    with pytest.raises(EpisodeMetaError, match=message):
        generate_episode_metadata(
            config=config,
            runtime_paths=runtime_paths,
            db_file=runtime_paths.db_file,
            session=session,
            client=client,
        )

    saved = get_episode(runtime_paths.db_file, session.session_uid)
    assert saved == original
