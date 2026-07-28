from dataclasses import dataclass
from enum import Enum
from typing import Optional

from podline.models import EpisodeRecord, SessionRecord, SessionStatus


class GuidanceKind(str, Enum):
    NEXT = "Next"
    WAITING = "Waiting on you"
    RECOVERY = "Recovery"
    COMPLETE = "Complete"


@dataclass(frozen=True)
class NextAction:
    kind: GuidanceKind
    instruction: str
    command: Optional[str] = None


def resolve_next_action(
    session: SessionRecord,
    *,
    episode: Optional[EpisodeRecord] = None,
    final_master_sha256: Optional[str] = None,
    offset_plan_reviewed: bool = False,
) -> NextAction:
    """Resolve the next safe workflow action from persisted session state."""
    session_uid = session.session_uid
    status = session.status
    production_id = getattr(session, "auphonic_production_id", None)
    offsets_verified_for = getattr(
        session, "auphonic_offsets_verified_production_id", None
    )
    transcript_refresh_id = getattr(
        session, "transcript_refresh_production_id", None
    )
    transcript_refresh_source = getattr(
        session, "transcript_refresh_source_sha256", None
    )
    transcript_refreshed_from = getattr(
        session, "transcript_refreshed_from_sha256", None
    )

    if status == SessionStatus.FAILED.value:
        if (
            production_id
            and getattr(session, "failed_at_stage", None) == "auphonic_prepare"
        ):
            return NextAction(
                GuidanceKind.RECOVERY,
                "Abandon the incomplete Auphonic production and rebuild it.",
                f"pod auphonic-reset {session_uid}",
            )
        if production_id and getattr(session, "failed_at_stage", None) in {
            "auphonic",
            "auphonic_start",
        }:
            return NextAction(
                GuidanceKind.RECOVERY,
                "Reconcile the stored Auphonic production before retrying anything.",
                f"pod auphonic-sync --session {session_uid}",
            )
        return NextAction(
            GuidanceKind.RECOVERY,
            "Inspect the saved failure details and repair the failed stage manually; "
            "there is no safe automatic retry command for this state.",
        )

    if status == SessionStatus.QUEUED.value:
        return NextAction(
            GuidanceKind.NEXT,
            "Create the Auphonic production and attach its source tracks.",
            f"pod auphonic-prepare {session_uid}",
        )

    if status == SessionStatus.AUPHONIC_PREPARED.value:
        if not production_id:
            return NextAction(
                GuidanceKind.RECOVERY,
                "The prepared session has no saved Auphonic production ID.",
            )
        if offsets_verified_for == production_id:
            return NextAction(
                GuidanceKind.NEXT,
                "Open the verified production and review it before starting.",
                f"pod auphonic-preview {session_uid}",
            )
        if offset_plan_reviewed:
            return NextAction(
                GuidanceKind.WAITING,
                "Review the proposed track offsets, then apply and re-verify them.",
                f"pod auphonic-offset {session_uid} --apply",
            )
        return NextAction(
            GuidanceKind.NEXT,
            "Compute the offset plan without changing the Auphonic production.",
            f"pod auphonic-offset {session_uid}",
        )

    if status == SessionStatus.AUPHONIC_PREVIEWED.value:
        if not production_id:
            return NextAction(
                GuidanceKind.RECOVERY,
                "The previewed session has no saved Auphonic production ID.",
            )
        if offsets_verified_for != production_id:
            return NextAction(
                GuidanceKind.NEXT,
                "Offsets are not verified for this production; recompute the plan.",
                f"pod auphonic-offset {session_uid}",
            )
        return NextAction(
            GuidanceKind.WAITING,
            "Confirm the previewed tracks and offsets in Auphonic, then start processing.",
            f"pod auphonic-start {session_uid}",
        )

    if status == SessionStatus.AUPHONIC_STARTED.value:
        if not production_id:
            return NextAction(
                GuidanceKind.RECOVERY,
                "The running session has no saved Auphonic production ID to reconcile.",
            )
        return NextAction(
            GuidanceKind.RECOVERY,
            "Auphonic processing was interrupted locally; reconcile its remote state.",
            f"pod auphonic-sync --session {session_uid}",
        )

    if status == SessionStatus.WAITING_FOR_FINALIZE.value:
        return NextAction(
            GuidanceKind.WAITING,
            "Edit the workspace audio in Cubase, export the final master, then attach it.",
            f"pod finalize {session_uid}",
        )

    if status == SessionStatus.READY_FOR_DRAFT.value:
        if not final_master_sha256:
            return NextAction(
                GuidanceKind.RECOVERY,
                "The finalized session has no current final-master artifact.",
            )
        if transcript_refresh_id:
            if transcript_refresh_source != final_master_sha256:
                return NextAction(
                    GuidanceKind.RECOVERY,
                    "The final master changed after this transcript refresh started; "
                    "discard the stale production and retry.",
                    f"pod transcript-refresh {session_uid}",
                )
            return NextAction(
                GuidanceKind.RECOVERY,
                "Resume the saved Auphonic transcript-refresh production.",
                f"pod transcript-refresh {session_uid}",
            )
        if transcript_refreshed_from != final_master_sha256:
            return NextAction(
                GuidanceKind.NEXT,
                "Refresh the transcript from the current final master.",
                f"pod transcript-refresh {session_uid}",
            )
        if episode is None or not episode.title or not episode.description:
            return NextAction(
                GuidanceKind.NEXT,
                "Generate episode metadata from the refreshed transcript.",
                f"pod episode-meta {session_uid}",
            )
        return NextAction(
            GuidanceKind.WAITING,
            "Review the saved title and description, then create the Podbean draft.",
            f"pod podbean-draft {session_uid}",
        )

    if status == SessionStatus.DRAFT_CREATED.value:
        return NextAction(
            GuidanceKind.COMPLETE,
            "Verify the Podbean draft audio, transcript, metadata, and release "
            "settings before publishing.",
        )

    return NextAction(
        GuidanceKind.RECOVERY,
        f"No safe next action is defined for session status {status!r}.",
    )
