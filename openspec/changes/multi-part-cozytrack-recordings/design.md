# Design

## Context

See `proposal.md` for motivation and the three delta specs for behavior.

Observed Podline constraints:

- `cozytrack/client.py` drops take IDs and partial flags. `cozytrack/ingest.py` derives roles from a session-wide track list, so repeated host channels conflict and repeated guest tracks receive new guest numbers.
- Ingest shares a content-hash/S3 registration path with local imports. Existing content deduplication and interrupted source-ID reconciliation must survive this change.
- `sessions` stores one pre-edit production ID. `media_artifacts` is unique by session/kind/role; its lookup, update, role-reconciliation, replacement, duration-backfill, and deletion helpers all assume that scope.
- `auphonic.py` persists one session mix/transcript and handles optional stems as a complete role-keyed set. Its generic HTTP retry helper also retries production-creation POST requests, which can duplicate a production after an ambiguous response.
- Duration offsets currently assume a shared stop. Creation-time validation is limited to unequal-duration recordings with more than two tracks. The two-track path can therefore shift an early-ending track incorrectly.
- `artifacts.py` expects one master and transcript and may align exported stems using duration/offset comparisons. `finalize.py` requires one episode-scoped pre-edit pair. These are part of the change, not just CLI presentation.
- The worker and state-machine modules are placeholders. The MVP stays in the synchronous command workflow rather than introducing orchestration infrastructure.

CozyTrack source was inspected at commit `0a904a34a872e0bb31186f81841ed2f8cde6c2a0`:

- Its existing schema has recording takes with authoritative `recording`/`stopped` state, stable participant IDs, `Track.takeId`, a monotonic `partial` flag, and recorder segments.
- Session detail includes track scalar fields but not the take collection or segment summaries. The same detail helper serves browser and ingest routes.
- Reconnect segments can be concatenated into a logical track without setting `partial=true`; partialness alone is not a reconnect detector.
- The ingest download route serves only complete, unpurged tracks. A session's top-level `recording` status is not evidence that all its takes are active.

The example session's reported 3 + 3 + 2 layout is a test scenario, not a new live verification. No audio was downloaded or verified during planning.

## Goals / Non-Goals

**Goals:**

- Give pre-edit work one coherent ownership rule: episode -> part -> source inputs, production, and outputs.
- Preserve the clean single-part user experience, storage objects, and post-edit episode lifecycle.
- Make remote side effects recoverable at part granularity, with fail-closed handling of uncertain creation rather than an unsupported exactly-once claim.
- Retain source evidence and offer manual repair without treating shorter duration as proof of a late start.

**Non-Goals:**

- New background jobs, a generalized workflow engine, a new database/service, or migration of S3 media keys.
- Audio stitching, gap reconstruction, sample-accurate synchronization, or timestamp/DAW markers. Marker feasibility is [issue #54](https://github.com/pashafateev/podline/issues/54).
- New participant-count presets or a new CozyTrack recorder/UI lifecycle.

## Decisions

### 1. Add episode-owned parts rather than child episodes

Keep `sessions` as the processing identity of an episode and `episodes` as its existing release metadata. Add `recording_parts` with:

- A stable primary key, parent `session_uid`, immutable positive part number, and nullable CozyTrack take ID.
- Take start/stop/lifecycle metadata and a compact source snapshot containing track IDs, participant IDs, resolved roles, materialization metadata, and timing evidence.
- Ingest progress, pre-edit processing state, current production ID, a small operation checkpoint/claim, failure detail, and timing-warning reasons.
- Unique parent/part-number and parent/non-null-take-ID associations.

Persist the episode's participant-to-role map once, as a small episode-owned metadata value. Host channel identities retain their existing `hostN` meaning; guests are assigned once by first appearance in ordered takes, with creation time and track ID as tie-breakers. A later subset uses its existing roles, not a freshly numbered guest list.

Every new local import or unsegmented legacy session receives one default part. There is no user-facing requirement to select it.

**Alternative:** child session rows would reuse existing functions but duplicate episode identity, finalization state, and draft eligibility. Encoding parts in role names would break role identity and stem mapping. Neither solves ownership cleanly.

### 2. Scope artifacts explicitly and keep final outputs episode-owned

Add nullable `part_id` to media artifacts. Raw inputs, pre-edit mixes, per-part transcripts, and pre-edit stems use a concrete part; the final master and refreshed final transcript use episode scope.

Replace the old unique index with two explicit uniqueness rules:

- Part scope: `(session_uid, part_id, kind, role)` when `part_id` is not null.
- Episode scope: `(session_uid, kind, role)` when `part_id` is null.

Do not rely on ordinary SQLite uniqueness over nullable columns. Validate that a supplied part belongs to the supplied episode. All repository operations, including optional stem-set replacement/deletion and source-ID/role/duration reconciliation, must carry the same scope. An omitted selector must never mean "first matching part."

Keep role values as speaker identities. Do not encode `part-02` into `host1`, use a transcript role to hide part ownership, or overload Auphonic IDs as part IDs.

Retain the current content-addressed S3 layout and checksum cache. Distinct DB ownership and deterministic part filenames prevent collisions; existing S3 objects do not need to move. Multipart staging uses a part subdirectory so downloads cannot overwrite another part or write through a cache hardlink.

For single-part compatibility, the episode's current transcript can reference the same S3 object as the part's pre-edit transcript before finalization. This is a metadata alias, not another audio upload. A successful final-master refresh replaces only the episode-level current transcript. Later part sync must never replace a refreshed final transcript.

**Alternative:** duplicating final outputs onto every part would confuse draft generation. Changing S3 keys is unnecessary because content hashes already isolate objects.

### 3. Add only the ingest metadata needed from CozyTrack

Extend the authenticated ingest detail response, retaining existing scalar fields:

```text
recordingTakes[]: id, startedAt, stoppedAt, status
tracks[]: existing fields including takeId and partial
          + segmentCount
          + incompleteSegmentCount
```

Counts describe all known segment rows; an incomplete segment has a status other than `complete`. These summaries flag known restart/reconnect and recovery risks without exposing per-segment audio URLs or promising a trustworthy media timestamp.

Keep the browser API unchanged by giving the shared query an explicit ingest-detail mode or a small ingest-specific wrapper. No schema migration, new recorder telemetry, auth relaxation, participant-status history, or segment timestamp export is required.

Podline parses missing optional fields as unknown, not `false` or an invented count. Multipart input requires its take collection so order and terminal state are authoritative. Old responses with no take IDs, or a single take ID without lifecycle metadata, retain the legacy single-part path with unavailable evidence reported. Mixed IDs or multiple IDs without take metadata are errors, not time-clustering heuristics.

**Alternative:** inferring reconnects from short duration misses materialized multi-segment tracks and misclassifies early departures. Requiring every historical session to have modern metadata would unnecessarily break compatibility.

### 4. Register a source snapshot and resume ingestion per part

Read the whole session before uploading. Validate associations and readiness: all exposed takes must be stopped, and selected tracks must be complete and unpurged. The top-level session may still say `recording`; do not call finalize or any write endpoint to change that.

Order nonempty takes by `(startedAt, takeId)` and report empty takes without inventing empty Auphonic work. All-null legacy take IDs form one default part. Resolve and persist the episode role map before registering part sources. Reject duplicate/ambiguous participant associations with exact source IDs rather than guessing.

Use the unique CozyTrack session association as the multipart ingest identity. For a new multipart episode, derive the stable session UID and a namespaced registration fingerprint from that provider/session ID, not a changing aggregate of downloaded audio. Keep the existing content-first registration/deduplication path for single-part CozyTrack and local imports; it can finish the single-part content check before creating its planned records. Existing session UIDs and registration fingerprints stay immutable when another take is appended. Never treat a match to only one part as proof that an entire multipart episode content-matches a local import.

Register the episode, planned parts, and source metadata transactionally. Then hash/probe/upload and register sources at part granularity, reusing verified durable registrations after interruption. The episode is not eligible for preparation until its selected ingest snapshot is complete. Re-read detail before declaring ingest complete to detect a recording or materialization that changed during ingest.

For CozyTrack, preserve every included source; do not use activity or short duration to silently drop a track. Keep the existing local recorder classifier unchanged. An explicitly excluded track remains recorded as an operator selection, without deleting its remote recording. Existing single-part role selectors work; ambiguous multipart roles require an exact track ID or `part-02:guest1`.

Repeated ingest keeps existing part numbers and assignments. Before finalization, new complete stopped takes ordered after the existing parts can append; source membership/materialization changes to a prepared part require explicit reconciliation/reset and must not overwrite consumed inputs. Do not insert an earlier take by renumbering existing files. After finalization, new takes or changed sources produce an actionable error rather than modifying the final episode.

For legacy single-part local-import deduplication, preserve the surviving session ID and verified content-hash source-ID backfill. The new multipart path must not attempt to register each part as an independent content-deduplicated episode.

**Alternative:** one transaction after every download is simple but loses useful ingest checkpoints. Blindly treating an existing CozyTrack ID as a no-op would hide unfinished or newly appended parts.

### 5. Use part-local processing checkpoints with thin episode orchestration

Reuse existing Auphonic primitives, with an explicit part context rather than constructing fake child sessions. Each part has ingest-pending/ready and preparing/prepared/previewed/starting/started/complete/failed progress as needed for its current operation. A durable production ID survives local errors.

The command layer selects all applicable parts by default or one `--part N`. Preflight supported counts (2-4), configured presets, scoped inputs, and start eligibility before remote side effects. Map preset slots to the selected part's persisted roles in the existing recorder order; retain the displayed binding even when a role's numeric suffix is greater than the number of speakers present.

Operation rules:

1. **Prepare:** atomically claim creation for an unprepared part; issue at most one create request for that attempt. Persist the returned ID before attaching inputs. On retry, inspect that ID and finish only missing matching attachments. Fail on foreign/mismatched attached inputs rather than overwriting them.
2. **Ambiguous create:** disable blind retries of this production-creation POST in the part-processing path. A claim with no durable ID after an ambiguous response/crash is blocked for explicit reconciliation, not automatically reset to allow another create. Include a stable episode/part marker in the production title for operator identification. `auphonic-sync <production-id> --session <episode> --part N` can explicitly bind a verified candidate, checking the marker, preset/slots, and any present input keys.
3. **Preview:** inspect and display all selected edit pages in order. Mark each eligible part previewed without starting it. The default browser behavior can open the selected prepared edit pages; `--no-open` remains available.
4. **Start:** preflight that all selected not-yet-started parts are previewed. Inspect stored productions before starting; resume running ones and persist completed outputs without restarting them. An uncertain start response leaves the ID/checkpoint intact for inspection. Do not automatically replay an ambiguous start request.
5. **Sync:** inspect and reconcile without starting processing. `--session` without a production ID synchronizes known part productions; an explicit production ID must resolve to exactly one selected/owned part.
6. **Reset:** retain the existing single-part prepared-production reset. Multipart reset requires `--part N`; it cannot abandon all completed sibling work accidentally. Replacement is an explicit action, never a retry side effect. Remote deletion is not added.

Use SQLite compare-and-set claims to stop concurrent creation for the same part. Do not hold database transactions across network calls or automatically expire an uncertain creation claim into permission to create again.

Aggregate the pre-edit episode status from part progress: all complete -> `waiting_for_finalize`; otherwise a failed part is reported with its part number, running work remains started, all previewed-or-complete is previewed, all prepared-or-later is prepared, and unfinished ingest/preparation remains queued. Retries consult part checkpoints, not only the aggregate status. Existing post-edit statuses are never demoted by part synchronization.

For a single part, retain legacy production-ID/status presentation as a compatibility projection. Do not make the session-level production ID the multipart source of truth.

**Alternative:** reusing the current whole-session retry path can recreate productions or overwrite outputs. A new worker or distributed lock system would exceed the MVP; durable part claims and remote inspection are sufficient for this synchronous tool.

### 6. Separate timing evidence from processing permission

Persist structured warning reasons and affected track IDs. Flag known `partial=true`, `segmentCount > 1`, incomplete segments, and contradictory timing. Missing fields remain unknown; no metadata combination proves the absence of every sleep/disconnection issue.

Evaluate known-risk evidence before the two-track/equal-duration shortcuts. For take-aware parts with available creation-time evidence, validate duration differences against recorder creation-time differences using the existing 0.5-second tolerance, including two-track parts. Do not treat take start/stop wall-clock timestamps as sample positions or use them to reconstruct gaps.

For a flagged part:

- Offset dry-run reports the part, track, reason, and manual repair instructions instead of a confident duration-offset plan.
- `--apply` preflights the entire selection and makes no offset writes if any selected part is unsafe. Safe parts remain selectable with `--part`.
- Prepare, preview, source export, and explicit start remain usable. No new mandatory start override or assertion of successful repair is introduced.
- Healthy parts continue their normal alignment. Legacy single-part/local behavior remains available when evidence is absent and no known risk exists; report the evidence limitation.

Preview/start do not clear a timing warning. A part with an early-ending track stays a single part; do not right-align it simply because it is shorter.

**Alternative:** warning-only auto-apply lets the same known-bad assumption damage alignment. Blocking every processing command would prevent the manual Auphonic/Cubase repair path the MVP is intended to support.

### 7. Materialize one workspace and isolate every managed output

Multipart output layout:

```text
<workspaces>/<episode_uid>/
  part-01.mp3
  part-01.vtt
  part-02.mp3
  part-02.vtt
  part-03.mp3
  part-03.vtt
  manifest.json
  sources/                    # explicit --sources
    part-02/
      <role>-<source-id>.webm
  stems/                      # explicit --stems
    part-01/
      <processed-speaker>.flac
  repair-manifest-part-01.json
  <user Cubase project and exports>
```

Single-part sessions keep the current mix/transcript filenames, flat `stems/`, and `repair-manifest.json`. Part filenames and repair paths are stable once registered; never rename or move existing single-part files automatically if another take is appended. Record those paths as the managed Part 1 outputs and use ordered names and part directories for newly added parts; the manifest supplies explicit order in that compatibility case.

The root manifest lists all parts, outputs, provenance, checksums, timing warnings, and pending work. Default workspace refresh can materialize completed parts while reporting others as pending. `--sources`, optionally with `--part`, works immediately after source ingest even when no paid output exists. Export exact source bytes through the existing checksum materialization path.

Scope stem archive validation, exact track/role mapping, complete-set replacement, stale-output cleanup, and repair metadata to the owning part. Preserve all existing archive traversal, duplicate, type, symlink, completeness, and expansion-limit protections.

For healthy parts retain the existing processed-stem offset handling. For flagged parts, copy returned FLACs without any Podline duration-derived shift/pad/trim; record Auphonic-configured offsets and mark their rendered status unknown unless independently established. The editor can inspect/repair sources or stems; this workflow does not claim to restore lost wall-clock gaps. Keep the loudness warning.

Refresh only files recorded as Podline-managed. Preserve Cubase projects, assembled exports, and other user files. Optional-stem failure/invalidation is limited to that part and never removes raw sources or a valid mix/transcript.

**Alternative:** separate workspaces/episodes increase manual bookkeeping; flattening all stem sets loses part provenance and causes filename collisions.

### 8. Rejoin the existing final-master workflow once

Finalization validates a mix/transcript pair for every required part rather than a single session-level pre-edit pair. It still accepts exactly one user-assembled master and stores it at episode scope. No concatenated audio or transcript is generated automatically.

The multipart episode has no factual whole-episode transcript until final-master transcript refresh succeeds. Metadata/draft consumers must require that refreshed episode transcript, not select a part transcript. Retain the existing episode-level transcript-refresh production checkpoint and metadata invalidation behavior.

Part sync after finalization may refresh its own pre-edit media but cannot alter the final master, episode current transcript, release metadata, draft association, or post-edit status. New input takes after finalization are rejected rather than silently invalidating an assembled export.

**Alternative:** using the last part's transcript or joining its VTT files without the Cubase edits would misrepresent the final episode.

## Risks / Trade-offs

- **Reconnect metadata is incomplete evidence** -> State limitations and flag known events only; do not claim universal detection or exact media timestamps.
- **Creation can succeed remotely without a received ID** -> Persist the intent, suppress blind create retries, and require explicit verified adoption. This sacrifices automatic recovery in an ambiguous window to prevent duplicate productions.
- **Two-track alignment becomes more conservative when evidence exists** -> Preserve unknown-metadata legacy behavior while blocking a demonstrated contradiction or known partial/reconnect.
- **Single-part fields and artifact aliases can become competing state** -> Make parts authoritative for pre-edit operations; isolate the legacy projection and episode transcript alias, and test that post-edit outputs cannot be overwritten.
- **Appending a take after Part 1 was materialized could rename user inputs** -> Preserve existing managed filenames and immutable part numbers; use manifest order rather than moving files.
- **A companion repository is not attached** -> Planning can specify the contract, but companion implementation must wait for attachment. Keep a separate ready PR with explicit dependency/rollout order.
- **Old clients cannot understand part-owned rows** -> Back up SQLite before migration and treat rollback as restoring the pre-migration database, not running an old binary on new multipart state. S3 sources remain unchanged.

## Migration Plan

1. Attach CozyTrack before companion edits; recheck its repository instructions/current source. Add contract tests first and implement only the additive ingest-detail metadata. The required rollout order is CozyTrack metadata deployed before multipart reliance in Podline; document this dependency in both PRs rather than merging or deploying automatically. Existing clients ignore the new fields.
2. Add an idempotent transactional Podline migration. Create one default part for legacy sessions, transfer existing raw/pre-edit/stem ownership, and carry the stored pre-edit production/progress into that part. Keep media URIs, hashes, filenames, roles, IDs, and existing post-edit state.
3. Preserve the existing episode current transcript. Add a part-scoped reference only when provenance identifies the pre-edit production; never mislabel a refreshed final transcript as an original part transcript or invent a lost historical transcript. Do not demote already finalized sessions if historical pre-edit artifacts are unavailable.
4. Keep drive-only legacy sessions with no registered S3 artifacts on their existing actionable error path; do not invent ready source records. Preserve hash-based local-import deduplication and interrupted CozyTrack association repair.
5. Run migration twice in regression tests across queued, prepared, previewed, failed, waiting-for-finalize, final-refresh-in-flight, ready-for-draft, and draft-created states. Verify no network/storage writes and no production recreation.
6. Implement the shared part-aware pipeline and CLI, then run fake-client/moto full-workflow tests and documentation checks. Record exact commands/results in focused commits and the ready task PR. Do not merge to a shared branch without separate landing authorization.
7. Validate deployment with read-only metadata inspection only if needed. Do not finalize the example session, start paid processing, alter remote recordings, or publish a draft for validation.

Rollback before new multipart use restores the pre-migration database backup and prior application version. After new multipart records exist, do not down-convert or silently flatten them; retain the source media and use a forward fix.

## Validation Strategy

Write failing regression/contract tests before each important behavior change, then make those tests pass. Extend the existing factories, fake Auphonic/CozyTrack clients, moto S3 fixtures, CLI runner, stem archive tests, and full S3 workflow instead of adding live integration dependencies.

Podline focused command (from the Podline repository, after test implementation):

```sh
uv run --locked --extra dev python -m pytest tests/test_db.py tests/test_cozytrack.py tests/test_cli_cozytrack.py tests/test_ingest.py tests/test_auphonic.py tests/test_artifacts.py tests/test_finalize.py tests/test_cli.py tests/test_workflow_s3.py
```

Full suite:

```sh
uv run --locked --extra dev python -m pytest
```

Companion commands (from the attached CozyTrack repository, once its new contract test exists):

```sh
npm test -- tests/ingest-session-detail.test.ts tests/sessions-route.test.ts tests/logical-track-segments.test.ts
npm test
npm run typecheck
```

Planning validation:

```sh
openspec validate multi-part-cozytrack-recordings --strict
git diff --check
```

These are intended implementation checks, not claims that they ran during proposal creation. Tests must cover 3 + 3 + 2 input, shuffled identities, source preservation, scoped output/stem deletion, creation/start ambiguity, concurrent create claims, interrupted middle-part recovery, known-risk guards including two-track/equal-duration cases, original single-part filenames, legacy migration, and the final assembled-master workflow.
