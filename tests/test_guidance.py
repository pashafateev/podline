from dataclasses import replace

import pytest

from podline.guidance import GuidanceKind, resolve_next_action
from podline.models import EpisodeRecord, SessionRecord


SESSION_UID = "session-exact-123"
PRODUCTION_ID = "prod-current"
FINAL_MASTER_SHA256 = "final-master-sha256"


def _session(**changes) -> SessionRecord:
    session = SessionRecord(
        session_uid=SESSION_UID,
        source_basename="recording",
        source_date_folder=None,
        source_path="/recordings/session-exact-123",
        recorded_at=None,
        recorded_at_source=None,
        fingerprint="fingerprint",
        status="queued",
        failure_reason=None,
        failed_at_stage=None,
        auphonic_production_id=None,
        auphonic_offsets_verified_production_id=None,
        transcript_refresh_production_id=None,
        transcript_refreshed_from_sha256=None,
        pre_edit_master_path=None,
        transcript_path=None,
        final_master_path=None,
        cozytrack_session_id=None,
        created_at="now",
        updated_at="now",
    )
    return replace(session, **changes)


EPISODE_METADATA = EpisodeRecord(
    session_uid=SESSION_UID,
    episode_number=None,
    podbean_post_id=None,
    draft_url=None,
    title="Saved title",
    description="Saved description",
)


@pytest.mark.parametrize(
    ("session", "episode", "final_sha256", "kind", "command"),
    [
        (
            _session(),
            None,
            None,
            GuidanceKind.NEXT,
            f"pod auphonic-prepare {SESSION_UID}",
        ),
        (
            _session(
                status="auphonic_prepared",
                auphonic_production_id=PRODUCTION_ID,
            ),
            None,
            None,
            GuidanceKind.NEXT,
            f"pod auphonic-offset {SESSION_UID}",
        ),
        (
            _session(
                status="auphonic_prepared",
                auphonic_production_id=PRODUCTION_ID,
                auphonic_offsets_verified_production_id=PRODUCTION_ID,
            ),
            None,
            None,
            GuidanceKind.NEXT,
            f"pod auphonic-preview {SESSION_UID}",
        ),
        (
            _session(
                status="auphonic_previewed",
                auphonic_production_id=PRODUCTION_ID,
                auphonic_offsets_verified_production_id=PRODUCTION_ID,
            ),
            None,
            None,
            GuidanceKind.WAITING,
            f"pod auphonic-start {SESSION_UID}",
        ),
        (
            _session(
                status="auphonic_started",
                auphonic_production_id=PRODUCTION_ID,
                auphonic_offsets_verified_production_id=PRODUCTION_ID,
            ),
            None,
            None,
            GuidanceKind.RECOVERY,
            f"pod auphonic-sync --session {SESSION_UID}",
        ),
        (
            _session(status="auphonic_started"),
            None,
            None,
            GuidanceKind.RECOVERY,
            None,
        ),
        (
            _session(
                status="waiting_for_finalize",
                auphonic_production_id=PRODUCTION_ID,
                auphonic_offsets_verified_production_id=PRODUCTION_ID,
            ),
            None,
            None,
            GuidanceKind.WAITING,
            f"pod finalize {SESSION_UID}",
        ),
        (
            _session(status="ready_for_draft"),
            None,
            FINAL_MASTER_SHA256,
            GuidanceKind.NEXT,
            f"pod transcript-refresh {SESSION_UID}",
        ),
        (
            _session(
                status="ready_for_draft",
                transcript_refreshed_from_sha256=FINAL_MASTER_SHA256,
            ),
            None,
            FINAL_MASTER_SHA256,
            GuidanceKind.NEXT,
            f"pod episode-meta {SESSION_UID}",
        ),
        (
            _session(
                status="ready_for_draft",
                transcript_refreshed_from_sha256=FINAL_MASTER_SHA256,
            ),
            EPISODE_METADATA,
            FINAL_MASTER_SHA256,
            GuidanceKind.WAITING,
            f"pod podbean-draft {SESSION_UID}",
        ),
        (
            _session(status="draft_created"),
            EPISODE_METADATA,
            FINAL_MASTER_SHA256,
            GuidanceKind.COMPLETE,
            None,
        ),
        (
            _session(
                status="failed",
                failed_at_stage="auphonic_prepare",
                failure_reason="second track attachment failed",
                auphonic_production_id=PRODUCTION_ID,
            ),
            None,
            None,
            GuidanceKind.RECOVERY,
            f"pod auphonic-reset {SESSION_UID}",
        ),
        (
            _session(
                status="failed",
                failed_at_stage="auphonic",
                failure_reason="poll interrupted",
                auphonic_production_id=PRODUCTION_ID,
            ),
            None,
            None,
            GuidanceKind.RECOVERY,
            f"pod auphonic-sync --session {SESSION_UID}",
        ),
        (
            _session(
                status="failed",
                failed_at_stage="auphonic",
                failure_reason="prepare failed before creation",
            ),
            None,
            None,
            GuidanceKind.RECOVERY,
            None,
        ),
    ],
)
def test_resolve_next_action_covers_the_complete_workflow(
    session: SessionRecord,
    episode: EpisodeRecord | None,
    final_sha256: str | None,
    kind: GuidanceKind,
    command: str | None,
) -> None:
    action = resolve_next_action(
        session,
        episode=episode,
        final_master_sha256=final_sha256,
    )

    assert action.kind is kind
    assert action.command == command


def test_offset_dry_run_guides_review_then_apply() -> None:
    action = resolve_next_action(
        _session(
            status="auphonic_prepared",
            auphonic_production_id=PRODUCTION_ID,
        ),
        offset_plan_reviewed=True,
    )

    assert action.kind is GuidanceKind.WAITING
    assert action.command == f"pod auphonic-offset {SESSION_UID} --apply"


def test_interrupted_transcript_refresh_guides_safe_resume() -> None:
    action = resolve_next_action(
        _session(
            status="ready_for_draft",
            transcript_refresh_production_id="prod-transcript-refresh",
        ),
        final_master_sha256=FINAL_MASTER_SHA256,
    )

    assert action.kind is GuidanceKind.RECOVERY
    assert action.command == f"pod transcript-refresh {SESSION_UID}"
