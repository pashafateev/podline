# Sex Drunks Automation

Local automation project for the Sex Drunks production workflow.

Environment note
- This scaffold currently targets Python 3.9+ so it runs on the machine as-is.

Current scope
- Phase 0 scaffold is implemented.
- The CLI can initialize runtime folders and database state.
- Basic inspection commands are available: `sd init`, `sd health`, `sd list`, `sd status`.
- Phase 1 scanning and ingest are implemented via `sd scan`.
- `sd refresh` is a shortcut for reset-db + scan + list.
- `recorded_at` is best-effort metadata. The recorder date is not always reliable, so this value may reflect filesystem timing rather than a true recording timestamp.
- Silent extra spoken tracks are ignored during scan, so sessions with empty `Tr3`/`Tr4` files can still classify correctly as 2-speaker sessions.
- Phase 3 Auphonic processing is available via `sd auphonic <session_uid>`.
- Safer Auphonic dry runs are available via `sd auphonic-prepare <session_uid>` followed by `sd auphonic-start <session_uid>`.
- Add `--debug` to CLI commands when you want verbose request-level logging in `logs/automation.log`.
- `auphonic.request_timeout_seconds` controls how long large uploads can run before the client gives up.
- If direct local uploads keep failing, the fallback plan is to upload WAVs to an external service first and point Auphonic `multi_input_files` at those remote files instead of streaming them from this machine.

Project layout
- `src/sexdrunks_automation/`: application code
- `tests/`: unit tests
- `state/`: SQLite DB and health file
- `logs/`: local logs
- `staging/`: temporary working area for future pipeline phases

Getting started
```bash
cd ~/sexdrunks-automation
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
sd init
sd health
```

Local config
- Keep committed defaults in `config.yaml`.
- Put machine-specific paths and secrets in `config.local.yaml`, which is gitignored.
- Start from `config.local.yaml.example` and copy it to `config.local.yaml`.
- The recording root is `02_recordings/`, with session folders directly under it.

Planned next steps
- manual end-to-end processing flow
- finalize and Podbean draft creation
- live Auphonic validation with real credentials
- external-service Auphonic inputs as a backup path if local multipart uploads stay flaky
