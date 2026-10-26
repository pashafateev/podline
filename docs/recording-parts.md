# Recording-part persistence and compatibility

## Ownership

An episode keeps its existing `sessions.session_uid` identity and release
metadata in `episodes`. Ordered pre-edit work belongs to `recording_parts`.
Each part has an opaque stable `part_id`, an immutable parent and positive
`part_number`, and an optional CozyTrack `take_id`. Part numbers and non-null
take IDs are unique within an episode; neither a take ID nor an Auphonic
production ID is used as the part's primary key.

Part records persist ingest completeness, the source snapshot, take lifecycle
metadata, processing status, the pre-edit Auphonic production ID, an operation
claim, failure detail, timing warnings, and the managed output basename.
Snapshots are JSON objects supplied by ingest; timing reasons are a separate
JSON list, not an inferred property of the processing status. The episode's
participant-ID-to-role map is stored independently in `sessions.participant_roles`
so invalidating generated release metadata does not erase speaker identities.
Ingest owns role assignment and snapshot contents.

Artifact ownership is explicit:

| Artifact | Owner |
| --- | --- |
| `raw_track`, `pre_edit_master`, `pre_edit_stem` | Recording part |
| Pre-edit `transcript` | Recording part |
| Current episode `transcript` | Episode (`part_id IS NULL`) |
| `final_master` | Episode (`part_id IS NULL`) only |

The database uses two partial unique indexes, not nullable-column uniqueness:

- `idx_media_artifacts_session_kind_role`: `(session_uid, kind, role)` **where
  `part_id IS NULL`**.
- `idx_media_artifacts_part_kind_role`: `(session_uid, part_id, kind, role)`
  **where `part_id IS NOT NULL`**.

Thus two parts can each own a `raw_track/host1`, stem, mix, or transcript without
replacing one another. Role values remain speaker identities; part numbers are
not encoded into role names.

## Repository interface

`MediaArtifactRecord` adds a trailing `part_id: Optional[str] = None`, preserving
old positional and keyword construction. `RecordingPartRecord` exposes the
persisted fields as Python values (`dict`, `list`, and `bool` for JSON/ingest
fields), plus `created_at` and `updated_at`.

The part helpers in `podline.repository` are:

```python
create_recording_part(
    db_file, *, session_uid, part_number, take_id=None,
    source_snapshot=None, started_at=None, stopped_at=None, take_status=None,
    status="queued", ingest_complete=True, output_basename=None,
)
get_recording_part(db_file, part_id)          # record or None
list_recording_parts(db_file, session_uid)   # ordered by part_number
ensure_default_part(db_file, session_uid)    # sole part, or create Part 1
update_recording_part(db_file, part_id, **fields)
get_participant_roles(db_file, session_uid)
set_participant_roles(db_file, session_uid, roles)
claim_recording_part_operation(db_file, part_id, operation)  # bool
```

Creation/update helpers return the persisted record; duplicate part numbers or
take associations fail rather than renumbering an existing part.
`ensure_default_part` rejects multipart episodes instead of selecting Part 1.
The update helper accepts `take_id`, `status`, `auphonic_production_id`,
`ingest_complete`, `source_snapshot`, `timing_warnings`, `started_at`,
`stopped_at`, `take_status`, `output_basename`, `operation`, and `failure_reason`.
Explicit `None` clears optional fields; identity, parent, and order cannot be
changed through this helper.

The operation claim is one compare-and-set update from `NULL` to the supplied
operation. Only one concurrent claimant wins. Claims do not expire or clear
themselves; deciding whether a remote side effect succeeded belongs to the
processing caller. No database transaction is held across a remote request.

All artifact registration, lookup, listing, role-set replacement, deletion,
duration updates, role reconciliation, source-ID backfill, and the source-ID
portion of CozyTrack association accept `part_id=None`:

- A concrete part ID must belong to the supplied episode, including for empty
  reconciliation/no-op requests. Every query and mutation stays in that scope.
- An omitted part for raw tracks, pre-edit mixes, or stems resolves to the sole
  part, creating the default part lazily for direct-SQL legacy callers.
  Omission on a multipart episode fails, even if only one part currently has
  matching media.
- Omitted `transcript` and `final_master` selectors always mean episode scope.
  Looking up an episode transcript never falls back to a part transcript.
  Explicit part transcripts remain independent of final refresh.
- `list_media_artifacts(db_file, session_uid)` with no kind or part selector is
  intentionally an **aggregate** for status/inspection. Supplying a concrete
  part filters even without a kind; supplying a kind uses the normal ownership
  rule above. Do not use the aggregate to pick a processing input.
- Storage-only legacy callers with no session row retain unscoped artifacts.
  They cannot use a concrete part without a real parent session.

Set replacement is transactional within the selected scope. Failure rolls back
both stale-role removal and upserts. Reconciliation changes legacy `assets`
roles only for a single-part episode, because those old rows have no part
ownership. Existing source IDs are provenance and are not overwritten by a
missing-ID backfill.

`store_artifact_file`, `store_artifact_files`, and `materialize_artifact` forward
the same optional selector. Storage validates scope **before** accessing S3,
and registration validates again inside its transaction. Keys remain the
existing content-addressed session/kind/hash/filename keys; no objects move.
Materialization continues to verify the checksum cache and download only when
necessary. Part filenames/staging and workspace assembly are caller concerns;
these helpers do not concatenate, shift, or otherwise transform media.

## Legacy projection and migration

Back up SQLite **before starting the updated application**: initialization
automatically migrates the configured database. Stop concurrent old Podline
processes while taking the backup and upgrading. Use SQLite's backup facility
for a consistent copy, particularly if a database uses WAL mode, for example:

```sh
sqlite3 /path/to/state.db ".backup '/path/to/state.before-recording-parts.db'"
```

Initialization creates the schema, replaces the old unscoped uniqueness rule,
and backfills default parts in one `BEGIN IMMEDIATE` transaction. On failure,
schema, indexes, and ownership changes roll back together. Re-initialization
does not create duplicate defaults, reset part checkpoints/claims, or restore
deleted artifacts.

For each legacy session with no parts, migration:

1. Creates one default Part 1, preserving the session identity, fingerprint,
   CozyTrack association, timestamps, production ID, and failure detail. The
   default output basename is the original `session_uid`.
2. Moves ownership of existing raw tracks, pre-edit mixes, and stems in place.
   Existing artifact row IDs, filenames, roles, durations, content types,
   checksums, S3 URIs, source IDs, and timestamps remain unchanged.
3. Leaves the existing episode transcript and final master in place. If the
   transcript's Auphonic `source_id` equals the stored pre-edit production ID
   and is not the in-flight final refresh production ID, adds only a
   **metadata reference** to that same object at part scope.
4. Does **not** invent a historical pre-edit transcript when the episode's
   current transcript has already been refreshed or its provenance is unknown.
   An in-flight final refresh with the old pre-edit transcript still current
   preserves both the old reference and the episode refresh checkpoint.
5. Leaves session status, workspace paths, release metadata, draft associations,
   and episode-level transcript-refresh state untouched. No remote request,
   media copy, new production, or storage write occurs.

The pre-edit status compatibility projection is:

| Legacy session status | Default part status |
| --- | --- |
| `queued`, `failed` | unchanged |
| `auphonic_prepared` | `prepared` |
| `auphonic_previewed` | `previewed` |
| `auphonic_started` | `started` |
| `waiting_for_finalize`, `finalize_started`, `ready_for_draft`, `draft_created` | `complete` |

Old single-part callers updating session status or resetting a preparation also
mirror the stored pre-edit production/status/failure state to the sole part
**only when its source snapshot is empty**. This keeps migrated/local legacy
workflows coherent without making session-level state authoritative for
multipart or snapshot-owned parts. New session creation and local
`create_session_with_artifacts` create the default part in the same transaction.

Drive-only legacy sessions without registered artifacts get no invented source
records and are not marked ingest-complete by migration. Existing finalized
status is preserved even if historical pre-edit artifacts are missing; migration
is not permission to restart paid processing or demote published work.

Before finalization, single-part workflows may explicitly maintain an
episode-current transcript alias to the same object as the part transcript.
After final-master transcript refresh, the episode transcript is authoritative
for metadata/draft work. Updating a part's pre-edit transcript must not replace
that refreshed transcript. Persistence does not generate an assembled transcript
or infer one from the last part.

## Rollback limitations

An older binary cannot interpret part-owned media reliably. Before new multipart
use, rollback means restoring the **pre-migration database backup together with
the previous application version**, not running the old version against the
migrated database. Do not attempt to flatten part records into session-only
roles. Once new multipart work exists, retain the original S3 sources and use a
forward fix; restoring an old backup would discard the new database ownership
and checkpoints. S3 objects and local user editing files are not modified by
this migration.

## Focused test-first evidence

Checks use `uv run --locked --extra dev python -m pytest` on CPython 3.13.5,
pytest 9.1.1, macOS. Test databases/files are temporary; S3 tests use moto and
synthetic bytes. Migration tests forbid socket connections, storage construction,
and subprocess execution.

- Initial new suites (`tests/test_part_repository.py`,
  `tests/test_recording_parts_migration.py`, `tests/test_part_artifacts.py`,
  `--tb=short`): **22 failed, 40 setup errors** before implementation.
- After adding schema/model/part helpers but before artifact scoping, the same
  command: **51 failed, 11 passed**, exposing the old artifact APIs and missing
  default creation.
- Parent-requested legacy projection regressions (`tests/test_part_repository.py
  -k 'legacy_session_status or legacy_reset or session_projection or
  concurrent_default' --tb=short`): **9 failed, 2 passed, 36 deselected** before
  adding the projection.
- Focused database/artifact/ownership/migration command:

  ```sh
  uv run --locked --extra dev python -m pytest tests/test_db.py tests/test_artifacts.py tests/test_part_repository.py tests/test_recording_parts_migration.py tests/test_part_artifacts.py --tb=short
  ```

  **95 passed** after implementation (including all 73 new cases).
- Existing workflow regressions:

  ```sh
  uv run --locked --extra dev python -m pytest tests/test_cozytrack.py tests/test_cli_cozytrack.py tests/test_ingest.py tests/test_auphonic.py tests/test_finalize.py tests/test_cli.py tests/test_workflow_s3.py --tb=short
  ```

  **168 passed**. These check compatibility with the pre-existing workflow;
  they do not claim the separately implemented multipart ingest, Auphonic, CLI,
  or workspace integration is complete.
- Full repository suite:

  ```sh
  uv run --locked --extra dev python -m pytest
  ```

  **357 passed**. `openspec validate multi-part-cozytrack-recordings --strict`
  and `git diff --check` also passed. No live-service or real-media workflow was
  run; multipart integration and OpenSpec task tracking remain with the parent.
