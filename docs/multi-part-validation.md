# Multi-part CozyTrack MVP validation

## Environment and limits

- Podline: macOS, CPython 3.13.5, pytest 9.1.1, dependencies from `uv.lock`.
- CozyTrack: Node v26.10.0, npm 11.19.1, dependencies from `package-lock.json`.
- Auphonic, CozyTrack downloads, OpenAI, and Podbean were faked in workflow tests;
  S3 used moto. Tests used temporary databases, caches, staging, and workspaces.
- No example-session mutation/finalization, live audio download/listening, paid
  processing, real publication, deployment, or shared-branch merge was performed.
- Python 3.9, real Cubase assembly, real Auphonic processing, CozyTrack browser/
  PostgreSQL integration smoke checks, and a CozyTrack production build were not
  run. No claim of universal disconnect detection or reconnect-gap repair is made.
- Reconnect timestamp/editor markers remain outside this change in
  [issue #54](https://github.com/pashafateev/podline/issues/54).

## Final commands and outcomes

From Podline:

```sh
uv run --locked --extra dev python -m pytest tests/test_db.py tests/test_cozytrack.py tests/test_cli_cozytrack.py tests/test_ingest.py tests/test_auphonic.py tests/test_artifacts.py tests/test_finalize.py tests/test_cli.py tests/test_workflow_s3.py
```

**191 passed.**

```sh
uv run --locked --extra dev python -m pytest
```

**457 passed.** This includes all new ownership, migration, source-ingest,
request-recovery, timing, CLI, editing, and final-master tests, plus the existing
single-part/local workflow and storage/security regressions.

```sh
openspec validate multi-part-cozytrack-recordings --strict
git diff --check
```

**Passed.** Scoped staged-diff checks also passed before source commits.

From CozyTrack:

```sh
npm test -- tests/ingest-session-detail.test.ts tests/sessions-route.test.ts tests/logical-track-segments.test.ts
npm test
npm run typecheck
git diff --check
```

**41 focused tests passed**, **353 full-suite tests passed across 40 files**,
**typecheck passed with zero errors**, and **diff check passed**. `npm ci` reported
existing dependency audit findings; no dependency/lockfile upgrade was made as
part of the additive metadata contract.

## Test-first and review evidence

| Change | Red evidence before its implementation/fix | Final evidence |
| --- | --- | --- |
| CozyTrack ingest-detail contract | 7 new failures, 34 passed | 41 focused / 353 full tests pass |
| Part ownership, migration, storage | Initial 22 failures + 40 missing-API setup errors; legacy projection then 9 failures / 2 passed | 95 focused ownership/migration/artifact tests pass |
| Take parsing / 3 + 3 + 2 ingest | Initial 6 failures / 4 passed, including repeated host-role collision and discarded take metadata | Included in full suite |
| Non-idempotent HTTP retries | 6 failures / 1 passed: create/start requests issued three times | All request-policy regressions pass |
| Workspace readiness and final provenance | After DB integration: 4 behavioral failures / 67 passed | 71 focused boundary tests pass |
| Stale concurrent prepare / foreign source start | 2 behavioral failures | Both regressions pass |
| Active source during ingest / final-master guard / modern legacy healing / subset dedup / single combined command | 5 behavioral failures | All regressions pass |
| Atomic multipart plan / single-part timing evidence | 2 behavioral failures | Both regressions pass |
| Independent review findings | 4 reproduced failures: missing input at start, foreign input at completed sync, cache hardlink corruption on retry, lost timing warning on reingest | All four regressions pass |
| Definite rejection and known-created-ID recovery | 4 failures, including an unrecoverable rejected-create claim and lost returned production ID | All regressions pass |
| Preset-less production-detail response | Explicit adoption rejected a candidate when the response omitted the template UUID | Read-only preset-definition verification regression passes |
| Dynamic slot attachment resume | Partial attachment could not resume because missing slots were treated as changed IDs | Regression passes without recreating the production or reattaching the completed source |
| Single-part reset projection | Reset retained an abandoned production ID in the legacy session projection | Regression passes |
| Known active take with legacy unassociated tracks | 1 failure: the legacy fallback ignored available active-take metadata | Ingest now rejects before downloading |

The production/preset recovery contract was checked against Auphonic's public
[query documentation](https://auphonic.com/help/api/query.html). Production detail
is not assumed to return a template UUID; explicit reconciliation can read the
configured preset definition to verify its declared slots. Public documentation
inspection did not call a live account or start processing.

## Acceptance coverage

| Capability / requirement group | Primary test evidence |
| --- | --- |
| Take-based ingest; host stop/start versus reconnect; changing participant counts | `tests/test_recording_parts.py` |
| Stable identity, shuffled source order, missing/duplicate identity, absent guests | `tests/test_recording_parts.py`, existing identity tests in `tests/test_cozytrack.py` |
| Additive metadata, legacy unknown values, active/incomplete/missing take errors | CozyTrack `tests/ingest-session-detail.test.ts`; Podline parser/ingest tests |
| Part ownership, interrupted ingest, append, prepared/final source changes, exclusions | `tests/test_part_repository.py`, `tests/test_part_artifacts.py`, `tests/test_recording_parts.py` |
| Single-part/local compatibility, migration, verified content/source-ID healing | `tests/test_recording_parts_migration.py`, `tests/test_ingest.py`, `tests/test_cozytrack.py`, `tests/test_recording_parts.py` |
| Episode commands, targeted inspect/reset/recovery, supported count/preset wiring | `tests/test_parts_cli.py`, `tests/test_part_processing.py`, `tests/test_auphonic.py` |
| Preview gate; no implicit paid starts; explicit flagged-part control | `tests/test_parts_cli.py`, `tests/test_part_processing.py` |
| Creation/start ambiguity, concurrency, attachment/poll/output/cache interruption | `tests/test_auphonic_retry_safety.py`, `tests/test_part_processing.py`, repository claim tests |
| Known timing guards, early ending, equal duration, safe selected part, legacy offsets | `tests/test_part_processing.py`, `tests/test_auphonic.py`, `tests/test_multipart_editing.py` |
| Scoped output/stem synchronization and archive protections | `tests/test_part_artifacts.py`, `tests/test_auphonic.py`, `tests/test_multipart_editing.py` |
| Ordered/pending workspace, stable legacy names, user-file protection, exact sources | `tests/test_multipart_editing.py`, `tests/test_parts_cli.py`, `tests/test_artifacts.py` |
| Per-part stems, unchanged flagged FLACs, manifest/offset/loudness provenance | `tests/test_multipart_editing.py`, `tests/test_artifacts.py` |
| Every-part readiness and current production provenance at finalize | `tests/test_multipart_editing.py`, `tests/test_finalize.py` |
| One assembled master, final transcript prerequisite/retry, metadata/draft protection | `tests/test_workflow_s3.py`, `tests/test_multipart_editing.py`, final-consumer suites |

The combined multipart S3 workflow uses all eight synthetic source tracks,
prepares three independent productions, materializes ordered part outputs, accepts
an editor-supplied assembled export, resumes its interrupted final transcript
refresh, generates metadata, and creates one fake draft. A later part sync leaves
the final master, refreshed episode transcript, publishing state, and draft
association intact. The original local-import single-part full workflow also
passes unchanged.

## Delivery and rollout

- Companion API PR: [CozyTrack #205](https://github.com/pashafateev/cozytrack/pull/205).
- Deploy the additive CozyTrack metadata endpoint before relying on multipart
  ingest in Podline. No CozyTrack schema migration is needed.
- Before the first command using the new Podline version, back up its SQLite
  database. Migration changes metadata ownership, not S3 objects or source audio.
- Ready PRs are for review only; merge and deployment require separate
  authorization. Archive the OpenSpec change separately when appropriate.
