# Sex Drunks Automation

Local automation project for the Sex Drunks production workflow.

Environment note
- This scaffold currently targets Python 3.9+ so it runs on the machine as-is.

Current scope
- Phase 0 scaffold is implemented.
- The CLI can initialize runtime folders and database state.
- Basic inspection commands are available: `sd init`, `sd health`, `sd list`, `sd status`.
- Phase 1 scanning and ingest are implemented via `sd scan`.
- `recorded_at` is best-effort metadata. The recorder date is not always reliable, so this value may reflect filesystem timing rather than a true recording timestamp.
- Silent extra spoken tracks are ignored during scan, so sessions with empty `Tr3`/`Tr4` files can still classify correctly as 2-speaker sessions.
- Phase 3 Auphonic processing is available via `sd auphonic <session_uid>`.
- Safer Auphonic dry runs are available via `sd auphonic-prepare <session_uid>` followed by `sd auphonic-start <session_uid>`.

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
- The default recording root is now `02_recordings/`; both flat session folders and older nested date/session layouts are supported.

Planned next steps
- manual end-to-end processing flow
- finalize and Podbean draft creation
- live Auphonic validation with real credentials
