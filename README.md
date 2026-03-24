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

Planned next steps
- Phase 1 raw-session scanning and fingerprinting
- ingest persistence and event tracking
- queue inspection via `sd scan`
