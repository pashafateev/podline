# Editing multiple recording parts

A multipart episode has **one workspace and one final master**. Parts remain
independent pre-edit mixes and transcripts. Podline does not join audio, concatenate
VTT cues, reconstruct reconnect gaps, or create a separate episode/draft per take.
The editor assembles the intended final cut in Cubase (or another editor).

## Accessing completed parts and original sources

```sh
pod workspace @1
pod workspace @1 --sources
pod workspace @1 --sources --part 2
```

The default refresh makes completed part outputs available even when another
part is unfinished. A new multipart workspace uses:

```text
<workspaces>/<session_uid>/
  part-01.mp3
  part-01.vtt
  part-02.mp3
  part-02.vtt
  manifest.json
  sources/
    part-02/
      host1-<source-id>.webm
      guest2-<source-id>.webm
```

`--sources` works immediately after ingest, before Auphonic preparation or paid
processing. Each source copy is materialized through the checksum-verified
artifact cache and contains the original registered bytes: no shifting, padding,
trimming, stitching, or remote processing. Filenames retain the original extension
and combine safe speaker-role and source-ID tokens. Unsafe or long tokens gain a
short identity hash so sanitization cannot silently alias another source.

`--part N` refreshes only that part. It does not erase siblings' files or their
previously materialized manifest evidence. The root manifest always lists every
part in order, with take and production IDs, processing/ingest progress, source
snapshot, timing warnings, output checksums and storage provenance, and pending
work. `ready_for_finalize` describes required registered outputs for the episode,
not a claim that every optional repair asset has been exported.

Workspace refresh only replaces Podline-managed files. Cubase projects, manual
repairs, assembled exports, and unrelated files are kept, including user files
inside source/stem directories. A conflicting unmanaged file or symlink is an
error rather than permission to overwrite it. Cache clearing does not remove
workspaces. The existing explicit `workspace-clean --apply` retention policy is
unchanged and removes an entire eligible old workspace, including user projects;
keep those projects elsewhere if they must outlive that cleanup.

## Optional processed FLAC stems

Enable **Individual Tracks / FLAC** on the relevant existing Auphonic production,
then sync and request stems:

```sh
pod auphonic-sync --session @1 --part 2
pod workspace @1 --stems --part 2
```

Multipart stems live under `stems/part-02/`, with
`repair-manifest-part-02.json` at the workspace root. Repeated speaker/track names
in other parts do not collide. Each repair manifest binds the owning part,
production, speaker role, Auphonic track ID and input filename to its source and
workspace checksums and configured/applied offsets. Invalidating one part's
optional stems never removes another part's stems, raw sources, or valid mix/VTT.
Archive extraction and its traversal, symlink, entry-type, completeness,
duplicate, and expansion-limit checks remain in the Auphonic output stage; the
workspace consumes only its persisted, scoped FLAC records.

Healthy parts retain the existing offset handling. A part with a persisted timing
warning instead gets **unchanged FLAC bytes**: Podline does not infer a shift,
padding, trimming, or rendered offsets from durations. The manifest records the
configured Auphonic offsets, zero Podline-applied offsets, and
`offsets_rendered_by_auphonic: null` (unknown). This does not claim that Auphonic
ignored an editor's adjustments or that reconnect gaps were repaired.

Individual tracks are processed but **not loudness-normalized**. Check final
loudness and true peak on the assembled/repaired mix before finalize.

## Single-part and append compatibility

Single-part workspaces keep their existing mix/VTT names, flat `stems/` directory,
and `repair-manifest.json`. If a later take appends before finalization, existing
Part 1 files and repair paths stay in place: no automatic rename/move that breaks
Cubase references. New parts get separate ordered names and stem directories;
the manifest provides the order even when Part 1 uses historical filenames.
Persisted artifact filenames and already-managed workspace paths take precedence
over constructing a new Part 1 name.

## Final-master boundary

```sh
pod finalize @1 --master <one-assembled-export>
pod transcript-refresh @1
pod episode-meta @1
pod podbean-draft @1
```

Finalize requires every part's source ingest and completed processing, with its
mix/VTT pair bound to the current part production. A stale caller's aggregate
status cannot make old outputs eligible during reprocessing. Finalize names a
missing/unfinished part instead of accepting the last completed part.
It uploads exactly one editor-supplied, episode-scoped `final_master`. Existing
workspace/deliverables master-path resolution, including paths with spaces,
remains unchanged.

A multipart episode has no factual whole-episode transcript until
`transcript-refresh` succeeds against that assembled master. Metadata generation
and draft creation reject a missing transcript or a part/pre-edit alias with a
message to run `pod transcript-refresh`. They do not silently select the last
part or concatenate its subtitles. A successful refresh remains episode-owned
and uses a production distinct from all pre-edit part productions.

The existing refresh checkpoint/retry and metadata-invalidation flow still
applies: an interrupted download resumes the stored episode refresh production,
and successful refresh invalidates saved metadata. Neither workspace nor finalize
starts processing, generates metadata, or creates/publishes a draft. Part sync is
pre-edit only; the episode's final master, refreshed transcript, release metadata,
draft association, and post-edit state are separate.
