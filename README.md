# podline Automation

Local automation project for the podline production workflow.

Environment note
- This scaffold currently targets Python 3.9+ so it runs on the machine as-is.

Current scope
- The working manual-assisted path is implemented end to end through Podbean draft creation, with a Podline-owned S3 bucket as the authoritative store for all new session media.
- Core inspection commands: `pod init`, `pod health`, `pod list`, `pod status`, `pod cache-clear`.
- Ingest commands: `pod ct-list`, `pod ct-ingest` (Cozytrack) and `pod import <folder>` (Riverside/local). `pod scan` and `pod refresh` are retired — they registered drive-path sessions the S3 pipeline cannot process.
- Auphonic commands: `pod auphonic-prepare`, `pod auphonic-preview`, `pod auphonic-offset`, `pod auphonic-start`, `pod auphonic-inspect`, `pod auphonic-sync`. Productions read audio directly from S3 through the Auphonic external service — there are no local uploads.
- Editing and post-edit commands: `pod workspace`, `pod finalize`, `pod transcript-refresh`, `pod episode-meta`, `pod podbean-draft`.
- The external drive is no longer required for the pipeline; it remains readable history for pre-S3 sessions. File-touching commands on those old sessions fail with a clear "no artifacts registered" error.
- `recorded_at` is best-effort metadata. The recorder date is not always reliable, so this value may reflect filesystem timing rather than a true recording timestamp.
- Silent extra spoken tracks are ignored during classification, so sessions with empty `Tr3`/`Tr4` files can still classify correctly as 2-speaker sessions.
- Add `--debug` to CLI commands when you want verbose request-level logging in `logs/automation.log`.

Project layout
- `src/podline/`: application code
- `tests/`: unit tests
- `scripts/`: one-off operational scripts (e.g. the Auphonic remote-input pilot)
- `state/`: SQLite DB and health file
- `logs/`: local logs
- `staging/`: temporary working area for future pipeline phases
- `cache/`: checksum-keyed local copies of S3 artifacts (safe to clear with `pod cache-clear`)

S3 media storage
- A private Podline S3 bucket is the authoritative store for session media; `src/podline/storage.py` and `src/podline/artifacts.py` hold the storage layer (content-addressed keys `sessions/{session_uid}/{kind}/{sha256}/{filename}`, checksum-verified downloads, replace-in-place `media_artifacts` rows).
- Configure it under `storage.*` in `config.local.yaml` (`enabled`, `bucket`, `region`, optional `key_prefix`, optional `profile` for a dedicated AWS credentials profile). Setting `enabled: false` pauses new ingests without affecting anything else.
- Transient S3 upload failures retry automatically as complete transfers. Podline verifies content-addressed size and checksum after ambiguous failures, so final failures remain safe to rerun.
- Auphonic reads S3 audio through an Amazon S3 external service registered in the Auphonic account (read-only IAM user); set its UUID as `auphonic.s3_service_id`. `scripts/setup_iam_users.sh` provisions the IAM users.
- `pod health` reports storage access and cache usage; `pod cache-clear` empties the local cache (entries re-download on demand). Workspaces under `~/podline/workspaces/` are never touched by cache clearing; retire them with `pod workspace-clean` once episodes are out (dry run by default, `--apply` deletes workspaces of `draft_created` sessions untouched for 14+ days, `--older-than-days` to tune).

Getting started
```bash
cd ~/podline
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
pod init
pod health
```

Local config
- Keep committed defaults in `config.yaml`.
- Put machine-specific paths and secrets in `config.local.yaml`, which is gitignored.
- Start from `config.local.yaml.example` and copy it to `config.local.yaml`.
- The recording root is `02_recordings/`, with session folders directly under it.
- `auphonic.preset_ids.transcript_refresh` should point at a single-track preset that produces WebVTT subtitles from the edited final master. You can also pass this per run with `pod transcript-refresh --preset <id-or-name>`.
- Cozytrack ingest config lives under `cozytrack.*`:
  - `base_url` — Cozytrack API base URL (default `http://localhost:3000`).
  - `api_key` — value sent as `X-API-Key` to Cozytrack endpoints; required.
  - `download_subdir` — subfolder under `raw_root` where Cozytrack ingests land (default `cozytrack`).
  - `request_timeout_seconds` — HTTP request/download timeout (default `600`).

Planned next steps
- combine the post-edit steps into a smoother single command once the pieces stay stable
- add cleanup/retention rules for staging and generated artifacts
- improve release verification after Podbean draft creation
- evaluate whether external-service inputs are worth formalizing into the workflow; see `docs/open-issues.md`

Current workflow
1. Ingest the session

For Cozytrack (remote stems):
```bash
pod ct-list                    # show ready Cozytrack sessions, excludes already-ingested
pod ct-ingest <cozytrack-id>   # download, classify, upload tracks to S3, register
pod list
```

For Riverside (local files):
```bash
pod import <folder>            # classify, upload tracks to S3, register
pod list
```

Notes on ingest:
- Tracks are hashed and uploaded to `s3://<bucket>/sessions/<uid>/raw_track/...`; packet-derived durations are stored for the offset step. Nothing is written to the external drive.
- Cozytrack downloads land in a disposable staging folder that is removed after registration; re-running `pod ct-ingest <id>` is a no-op, and re-importing an identical folder is detected by content fingerprint.
- WebM/Opus stems are passed through to Auphonic as-is via the S3 service reference.

2. Prepare the Auphonic production without starting it
```bash
pod auphonic-prepare @1 --debug   # creates the production and attaches S3 tracks
pod auphonic-offset @1            # dry run; uses durations stored at ingest
pod auphonic-offset @1 --apply    # offset remote track(s); keep local tracks at zero
pod auphonic-preview @1
pod auphonic-inspect --session @1
```

Run `pod auphonic-offset` before preview/start. Durations come from audio packet timing recorded at ingest. For mixed Cozytrack sessions, stable participant identity distinguishes host-local tracks from remote tracks: local tracks remain at zero, while every shorter remote track is offset against the longest local reference. Remote tracks already equal to the reference are reported as aligned and skipped. The command updates the prepared production and does not start processing.

3. Start Auphonic after previewing the edit page
```bash
pod auphonic-start @1 --debug
pod status @1
```

The pre-edit master and transcript are persisted to S3 and a workspace is created automatically at `~/podline/workspaces/<session_uid>/` with both files. `pod workspace @1` recreates or refreshes it at any time.

4. Edit in Cubase from the workspace, export the final master, then attach it
```bash
pod finalize @1 --master ep16
```

Notes:
- `--master ep16` resolves against the session workspace first, then `04_deliverables/masters/` when the drive is mounted. Full paths and bare filenames also work.
- Finalize uploads the edited master to S3 as the session's `final_master` artifact.

5. Refresh the transcript from the edited final master
```bash
pod transcript-refresh @1
```

Notes:
- This creates a single-track Auphonic production reading the final master from S3, downloads the new WebVTT file, and stores it as the session's current transcript artifact (the previous object stays in the bucket as history).
- If polling or downloading is interrupted after Auphonic starts, re-running the command resumes the stored refresh production instead of creating a duplicate.
- A successful refresh invalidates previously generated episode metadata, so run `pod episode-meta @1` again before creating the Podbean draft.

6. Generate episode metadata using the local OpenCode command
```bash
pod episode-meta @1
pod status @1
```

7. Create the Podbean draft
```bash
pod podbean-draft @1
pod status @1
```

Cleanup status
- Source media and generated artifacts are authoritative in S3; the local cache under `~/podline/cache/` is a replaceable copy (`pod cache-clear`).
- Workspaces under `~/podline/workspaces/` persist through editing and release, then get retired with `pod workspace-clean --apply` after the retention window (default 14 days untouched, `draft_created` sessions only). Everything except a Cubase project saved in the workspace re-materializes from S3.
- Auphonic staging files under `~/podline/staging/` are transient; a retention policy is still a follow-up item.
