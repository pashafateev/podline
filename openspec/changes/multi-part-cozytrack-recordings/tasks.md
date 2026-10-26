# Tasks

## 1. Add the minimal CozyTrack ingest contract

- [x] 1.1 Have the user attach CozyTrack before companion edits, read its applicable repository instructions, and verify the current take/segment schema and ingest-detail helper still match the inspected contract.
- [x] 1.2 Add failing `tests/ingest-session-detail.test.ts` contract tests for ordered take lifecycle metadata, existing track fields including `takeId`/`partial`, segment and incomplete-segment counts, a reconnect with `partial=false`, and unchanged browser-detail behavior; verify these tests fail before implementation.
- [x] 1.3 Implement the additive authenticated ingest-detail query/serialization without recorder, schema, browser UI, or authorization changes; verify the new contract tests and existing session/segment tests pass.
- [x] 1.4 Document the ingest fields and unknown/legacy behavior in CozyTrack's project documentation, run the companion focused tests, `npm test`, and `npm run typecheck`, and record exact outcomes for its separate ready PR.

## 2. Introduce part ownership and compatible migration

- [x] 2.1 Add failing database/repository tests for two parts sharing the same kind/role, independent episode-scope final artifacts, invalid cross-episode ownership, and scoped lookup/upsert/replacement/deletion/role/duration/source-ID updates; verify they expose the existing session-only uniqueness and query assumptions.
- [x] 2.2 Add failing migration fixtures for queued, prepared, previewed, failed, waiting-for-finalize, final-refresh-in-flight, ready-for-draft, draft-created, and drive-only legacy sessions; verify expected identity, production, transcript, filename, source-provenance, and no-network invariants are asserted.
- [x] 2.3 Implement the recording-part model/table, persisted episode participant map, nullable artifact ownership, and explicit partial unique indexes; implement transactional, repeatable default-part backfill and verify the migration tests pass on two consecutive initializations.
- [x] 2.4 Make repository and artifact storage/materialization operations explicitly scope-safe, preserving S3 keys and checksum-cache behavior; verify the ownership tests and existing `tests/test_db.py` and `tests/test_artifacts.py` cases pass without cross-part mutation.
- [x] 2.5 Document the schema ownership rule, legacy compatibility projection, backup requirement, and rollback limitation in project documentation; verify it distinguishes part pre-edit transcripts from the episode's refreshed final transcript.

## 3. Ingest takes with stable identity and resumable sources

- [x] 3.1 Add failing parser/ingest fixtures for 3 + 3 + 2 takes with shuffled track order, stable participant IDs, absent/reappearing guests, short/quiet retained sources, reconnect segments, and legacy metadata; verify failures occur before implementing take grouping.
- [x] 3.2 Parse the take/partial/segment/materialization metadata with missing values preserved as unknown, validate grouping/readiness, and derive the durable episode-wide role map; verify one API `recording` session with stopped complete takes imports while active/incomplete, mixed-ID, and unresolved-take fixtures fail without remote mutation.
- [x] 3.3 Implement stable multipart registration identity, per-part source snapshots and ingest checkpoints, scoped raw uploads, and source reuse on retry; verify interrupted ingest resumes without duplicate episode/part/artifact rows and never presents a partial source snapshot as ready.
- [x] 3.4 Add and satisfy tests for appending a later complete stopped take before finalization, unchanged existing part numbers/roles/identity, rejection of prepared-source changes and post-finalization input changes, and detection of source changes during ingest.
- [x] 3.5 Preserve and run single-part/local content-deduplication and interrupted source-ID/role-reconciliation regressions, including ambiguous content matches; verify multipart registration never deduplicates the whole episode onto one matching part.
- [x] 3.6 Extend `ct-ingest --list-tracks`, ingest summaries, and exclusion selectors to identify part/take/role/source ID and timing evidence; add CLI tests for exact IDs, part-qualified roles, and ambiguous role rejection, and document the commands in README.

## 4. Prepare, review, start, and resume part productions

- [x] 4.1 Extend the fake Auphonic client to track independent productions and add failing tests for 3/3/2 preset selection, isolated input bindings, supported-count preflight, repeated prepare, and unchanged single-part preset wiring.
- [x] 4.2 Implement part-scoped preparation with a durable creation claim and production-ID checkpoint before attachments; verify attachment failure resumes the same production and concurrent prepare issues at most one create request.
- [x] 4.3 Add failing lost-create-response and lost-start-response tests, then suppress blind retries for these non-idempotent part operations and add explicit verified production adoption; verify retries never create an extra production, restart running/completed work, or bind another part's inputs.
- [x] 4.4 Implement episode-level preview/start/sync orchestration over part checkpoints and add CLI coverage for ordered edit pages, `--no-open`, `--part`, all-selected preview preflight, and a failed/in-progress middle part; verify inspect/sync never start processing and completed siblings are reused.
- [x] 4.5 Scope output staging/persistence, current production validation, optional stem-set invalidation, and aggregate episode status to parts; verify failure after mix persistence or workspace creation resumes the stored production and readiness waits for every required mix/transcript.
- [x] 4.6 Extend status/inspect/reset presentation and targeting, preserving unqualified single-part reset but requiring a part for multipart reset; verify CLI tests reject ambiguous reset/adoption and README explains retry versus explicit replacement.

## 5. Guard known unsafe duration alignment

- [x] 5.1 Add failing offset tests for `partial=true`, multiple segments with `partial=false`, incomplete segments, early-ending two-track media, equal-duration flagged tracks, and a flagged middle part among healthy parts; verify no unsafe remote offset writes occur.
- [x] 5.2 Implement persistent part warning reasons before duration-count shortcuts and use available recorder-time evidence with the existing 0.5-second tolerance for take-aware two-track and larger parts; verify flagged/contradictory cases refuse automatic plans while healthy and legacy/local alignment regressions pass.
- [x] 5.3 Preflight a whole `--apply` selection before writes, expose safe-part targeting, and keep warnings visible through preview/start without a new required start override; verify bulk unsafe selection writes nothing and an explicitly selected healthy part can still apply its validated offsets.
- [x] 5.4 Document the difference between a timing warning, blocked automatic offsets, and manual Auphonic/Cubase repair; verify CLI tests include the part, affected track, reason, and source-export instructions without claiming comprehensive disconnection detection.

## 6. Materialize the editing and repair workspace

- [x] 6.1 Add failing workspace tests for ordered independent part mixes/transcripts, pending parts, same speaker stem names across parts, source-only export before processing, and preservation of Cubase files and assembled exports on refresh.
- [x] 6.2 Implement multipart mix/transcript filenames and the part-aware root manifest, preserving existing single-part locations and names; verify adding a later part does not move/rename previously materialized Part 1 files or repair assets.
- [x] 6.3 Implement `workspace --sources` and optional `--part` using checksum-verified source materialization; verify exported bytes match raw artifacts and no production creation/start or alignment transformation occurs.
- [x] 6.4 Scope optional stem archive extraction, role/production bindings, manifests, and stale-file cleanup to each part while retaining single-part flat layout; verify existing unsafe-archive and expansion-limit tests plus cross-part isolation tests pass.
- [x] 6.5 Add and satisfy flagged-stem tests proving Podline does not infer a shift/pad/trim from durations, records configured Auphonic offsets without falsely asserting they were rendered, and preserves the original raw tracks and healthy parts' behavior.
- [x] 6.6 Update CLI workspace summaries and README examples for multipart outputs, `--sources`, optional per-part `--stems`, pending work, and the final loudness warning; verify the documented command forms through CLI tests.

## 7. Preserve the episode-level final-master workflow

- [x] 7.1 Add failing finalize/consumer tests for incomplete-part rejection, one assembled final master, required multipart final-transcript refresh, and protection against selecting the last part's transcript; verify they fail before changing the single-pre-edit-pair assumption.
- [x] 7.2 Implement all-parts readiness validation and episode-scoped final-master/transcript resolution for finalize, transcript refresh, metadata, and draft consumers; verify there is one final master, one final refresh production, and one episode draft association.
- [x] 7.3 Preserve existing transcript-refresh retry and metadata-invalidation behavior; add and satisfy tests showing later part sync cannot replace a refreshed final transcript, overwrite a final master, demote post-edit status, or change release metadata/draft state.
- [x] 7.4 Extend the fake-client/moto workflow test with the complete multipart path through an editor-supplied assembled export, then rerun the original local-import/single-part workflow unchanged; verify both pass without live Auphonic, OpenAI, or Podbean calls.
- [x] 7.5 Document the unchanged finalize -> transcript refresh -> metadata -> draft sequence and the multipart transcript prerequisite; verify README does not imply automatic audio assembly or VTT concatenation.

## 8. Verify integration and deliver the task branches

- [x] 8.1 Run the exact focused and full Podline commands in `design.md`, the companion focused/full/typecheck commands, `openspec validate multi-part-cozytrack-recordings --strict`, and `git diff --check`; record commands, interpreter/environment, pass/fail counts, and any checks not run.
- [x] 8.2 Exercise the documented episode-oriented CLI workflow against test configuration and fake services, including interruption/resume and source repair access; verify no live-session writes, paid processing, or publication occur and record the observable workspace/progress results.
- [x] 8.3 Review the final diff for unscoped artifact mutations, hidden retries/paid starts, accidental media/secret additions, and single-part regressions; verify every required spec scenario has a test or explicit validation result and timestamp-marker work remains out of scope.
- [x] 8.4 Create focused commits on task branches, keeping pre-existing staged OpenSpec setup separate from implementation changes; push to the verified source remotes and open ready PRs with exact validation outcomes and the CozyTrack-before-Podline rollout dependency. Verify the PR URLs and non-draft status; do not merge or deploy without separate authorization.
