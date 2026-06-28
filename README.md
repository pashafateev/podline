# podline Automation

Local automation project for the podline production workflow.

Environment note
- This scaffold currently targets Python 3.9+ so it runs on the machine as-is.

Current scope
- The working manual-assisted path is implemented end to end through Podbean draft creation.
- Core inspection commands are available: `pod init`, `pod health`, `pod list`, `pod status`.
- Ingest commands are available: `pod scan`, `pod refresh`, `pod ct-list`, `pod ct-ingest`.
- Auphonic commands are available: `pod auphonic-prepare`, `pod auphonic-start`, `pod auphonic-inspect`, `pod auphonic-sync`.
- Post-edit commands are available: `pod finalize`, `pod episode-meta`, `pod podbean-draft`.
- `recorded_at` is best-effort metadata. The recorder date is not always reliable, so this value may reflect filesystem timing rather than a true recording timestamp.
- Silent extra spoken tracks are ignored during scan, so sessions with empty `Tr3`/`Tr4` files can still classify correctly as 2-speaker sessions.
- Add `--debug` to CLI commands when you want verbose request-level logging in `logs/automation.log`.
- `auphonic.request_timeout_seconds` controls how long large uploads can run before the client gives up.
- If direct local uploads keep failing, the fallback plan is to upload WAVs to an external service first and point Auphonic `multi_input_files` at those remote files instead of streaming them from this machine.

Project layout
- `src/podline/`: application code
- `tests/`: unit tests
- `state/`: SQLite DB and health file
- `logs/`: local logs
- `staging/`: temporary working area for future pipeline phases

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
1. Scan and pick the session

For Riverside (local files):
```bash
cd ~/podline
source .venv/bin/activate
pod refresh
pod list
```

For Cozytrack (remote stems via S3):
```bash
pod ct-list                    # show ready Cozytrack sessions, excludes already-ingested
pod ct-ingest <cozytrack-id>   # download stems to 02_recordings/cozytrack/<YYYY-MM-DD>_<slug>/
pod list
```

Notes on Cozytrack ingest:
- Tracks land as `Tr1.webm`, `Tr2.webm`, … in the session folder, in `createdAt` order.
- The session is registered in podline's DB tagged with `cozytrack_session_id` and is then handled by the existing pipeline (`pod auphonic-prepare` → `pod podbean-draft`).
- Re-running `pod ct-ingest <id>` is a no-op once the session is already ingested — it just prints the existing folder path.
- WebM/Opus stems are passed through to Auphonic as-is. Auphonic's docs list Opus/WebM as supported `multi_input_files`. If a real run rejects them in practice, the planned follow-up is an inline `ffmpeg` transcode step during ingest (see issue #19).

2. Upload to Auphonic without starting yet
```bash
pod auphonic-prepare @1 --debug
pod auphonic-inspect --session @1
```

3. Start Auphonic and let it finish
```bash
pod auphonic-start @1 --debug
pod status @1
```

4. Edit in Cubase, export the final master, then attach it
```bash
pod finalize @1 --master ep16
```

Notes:
- `--master ep16` resolves to `04_deliverables/masters/sexdrunks_ep16_master.*` in the expected masters directory.
- You can also pass a full path or a filename.

5. Generate episode metadata using the local OpenCode command
```bash
pod episode-meta @1
pod status @1
```

6. Create the Podbean draft
```bash
pod podbean-draft @1
pod status @1
```

Cleanup status
- There is currently no automatic cleanup after Podbean draft creation.
- The app keeps the local DB state, logs, Auphonic staging files under `~/podline/staging/`, and deliverable copies in `04_deliverables/`.
- Cleanup/retention policy is still a follow-up item rather than an implemented step.
