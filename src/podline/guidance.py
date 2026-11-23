"""Determine the next production step from the session's persisted facts."""
from dataclasses import dataclass
from typing import Tuple

from podline.artifacts import workspace_path_for
from podline.models import RuntimePaths, SessionRecord
from podline.podbean import get_pending_draft
from podline.repository import get_episode, get_media_artifact, list_media_artifacts, offsets_applied, refresh_was_completed


@dataclass(frozen=True)
class NextStep:
    action: str
    reason: str
    manual: Tuple[str, ...] = ()


def next_step(paths: RuntimePaths, session: SessionRecord) -> NextStep:
    uid = session.session_uid
    db = paths.db_file
    state = session.status
    episode = get_episode(db, uid)
    if state == "ready_for_draft" and episode and episode.podbean_post_id:
        return NextStep("draft", "Record the existing Podbean draft as complete locally.",
                        ("podbean-draft", uid))
    if state == "draft_created" or (episode and episode.podbean_post_id):
        link = episode.draft_url if episode else None
        destination = link or "your Podbean dashboard"
        return NextStep("done", f"Review the unpublished draft in Podbean: {destination}")

    pending = get_pending_draft(db, uid)
    if pending:
        return NextStep("verify", f"Check Podbean for episode {pending.episode_number}, "
                        f"{pending.title!r}. Draft submission has an unconfirmed outcome.")
    if state == "queued" and session.auphonic_production_id:
        return NextStep("blocked", "Preparation was interrupted. Inspect the attached tracks before syncing.",
                        ("auphonic-inspect", "--session", uid))
    if state == "queued":
        return NextStep("preedit", "Prepare the Auphonic production.", ("auphonic-prepare", uid))
    if state in {"auphonic_prepared", "auphonic_previewed"} and session.failed_at_stage == "alignment":
        return NextStep("preedit", f"Inspect track alignment before rendering. {session.failure_reason}",
                        ("auphonic-inspect", "--session", uid))
    if state in {"auphonic_prepared", "auphonic_previewed"}:
        tracks = list_media_artifacts(db, uid, kind="raw_track")
        spoken_count = sum(track.role != "mix" for track in tracks)
        if spoken_count != 1 and not offsets_applied(db, uid, session.auphonic_production_id):
            return NextStep("preedit", "Validate and apply the track offsets.",
                            ("auphonic-offset", uid, "--apply"))
        if state == "auphonic_previewed":
            return NextStep("preedit", "Start the reviewed Auphonic production.", ("auphonic-start", uid))
        return NextStep("preedit", "Preview the tracks in Auphonic.", ("auphonic-preview", uid))
    if state == "auphonic_started":
        return NextStep("preedit", "Check the stored production and collect its outputs.",
                        ("auphonic-sync", "--session", uid))
    if state == "waiting_for_finalize":
        workspace = workspace_path_for(paths, uid)
        return NextStep("master", f"Edit the audio in {workspace} and export the finished mix. "
                        "Replace EPISODE_OR_PATH with its episode shortcut or file path.",
                        ("finalize", uid, "--master", "EPISODE_OR_PATH"))
    if state == "ready_for_draft":
        transcript = get_media_artifact(db, uid, kind="transcript")
        refreshed = (
            transcript and transcript.source_provider == "auphonic" and transcript.source_id
            and refresh_was_completed(db, uid, transcript.source_id)
        )
        if session.transcript_refresh_production_id or not refreshed:
            return NextStep("refresh", "Refresh the transcript from the final master.",
                            ("transcript-refresh", uid))
        if not episode or not episode.title or not episode.description:
            return NextStep("metadata", "Generate the episode title and description.",
                            ("episode-meta", uid))
        return NextStep("draft", "Create the unpublished Podbean draft.", ("podbean-draft", uid))
    if state == "failed" and session.failed_at_stage == "auphonic":
        if session.auphonic_production_id:
            return NextStep("blocked", "Inspect the failed Auphonic production, then sync its state.",
                            ("auphonic-inspect", "--session", uid))
        return NextStep("preedit", "Resolve the reported Auphonic error, then retry preparation.",
                        ("continue", uid))
    return NextStep("blocked", f"Check the session history before resuming from {state!r}. "
                    "Run pod status for the stored stage and failure details.", ("status", uid))
