# Proposal

## Why

A CozyTrack episode can contain several host-controlled recording takes, but Podline currently treats every session track as one simultaneous recording. Importing and processing ordered parts under one episode will support recordings such as 3 + 3 + 2 participants without mixing unrelated takes or making the editor manage separate episodes.

## What Changes

- Import all session tracks in one `pod ct-ingest <session-id>` operation, grouping them by CozyTrack take ID and ordering parts by take start time. Resolve speaker roles once per episode from stable participant IDs, independently of track order or later absences.
- Add explicit recording-part records and part-scoped source and pre-edit artifact ownership to the existing SQLite database. Preserve episode-scoped final-master, refreshed-transcript, metadata, and draft ownership.
- Extend prepare, offset, preview, start, inspect, reset, status, and sync to operate on an episode's parts. Use one existing supported Auphonic preset and production per part; keep preview and explicit start separate.
- Checkpoint each part independently, reuse finished work, and resume interrupted work without automatically creating replacement productions. Fail safely when a remote creation outcome is unknown.
- Create one editing workspace with ordered multipart mixes and transcripts (`part-01.mp3`, `part-01.vtt`, etc.), optional processed stems per part, and an explicit source-track export for manual repair. Do not stitch the parts together.
- Identify parts with known reconnects, partial recordings, or inconsistent timing. Report why duration-derived alignment is unsafe and refuse automatic offset application to those parts; preserve manual preview/start and source/stem repair paths.
- Preserve the usual single-part commands, filenames, workspace layout, existing completed productions, and local-import workflow. A known timing risk can trigger the safety guard even in a single-part episode.
- Add narrowly scoped CozyTrack ingest metadata for take lifecycle and reconnect-segment summaries, using existing database fields. No recording controls or recorder synchronization changes.

Reconnect timestamps and editor markers remain a later investigation in [Podline issue #54](https://github.com/pashafateev/podline/issues/54). Automatic gap reconstruction, automatic reconnect repair, additional simultaneous participant counts, and a broader worker/storage redesign are out of scope.

## Capabilities

### New Capabilities

- `recording-parts`: Episode-owned ordered recording parts, take-based ingest, stable speaker identity, durable source associations, and compatible migration of single-part sessions.
- `part-processing`: Part-scoped Auphonic preparation, timing safeguards, preview/start control, outputs, and interruption recovery.
- `episode-editing`: One editing workspace containing independent part outputs and repair assets, followed by the existing single-final-master workflow.

### Modified Capabilities

None. The repository has no existing main OpenSpec capabilities.

## Impact

- Podline: `cozytrack/client.py`, `cozytrack/ingest.py`, `db.py`, `models.py`, `repository.py`, `ingest.py`, `auphonic.py`, `artifacts.py`, `cli.py`, and the finalize/transcript consumer boundary; corresponding tests and README.
- Storage: additive SQLite migration and scoped artifact queries/updates; retain existing S3 objects and content-addressed storage/cache behavior.
- CozyTrack: a small companion ingest-detail API change and contract tests. Its repository must be attached before editing; deploy the additive API before relying on the new metadata in Podline.
- Validation: test-first regressions for 3 + 3 + 2 grouping, shuffled track order, stable roles, independent artifacts, interrupted processing, timing guards, legacy migration, and the complete single-part/local workflow. Use fake remote clients and test storage; no paid processing or live-session mutation.
