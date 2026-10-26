# Recording-part processing and recovery

An episode remains the unit of work. One CozyTrack take is one part, not one
participant or reconnect segment. A 3 + 3 + 2 recording becomes three productions
using the existing three-, three-, and two-speaker presets. It does not require
eight simultaneous speakers or three independent episode records.

## Normal commands

```sh
pod ct-ingest <cozytrack-session-id>
pod auphonic-prepare @1
pod auphonic-offset @1
pod auphonic-offset @1 --apply
pod auphonic-preview @1
pod auphonic-start @1
pod status @1
```

Preparation, alignment, preview, inspection, sync, and workspace materialization
never start paid processing. Multipart start preflights the selected parts'
preview state and verifies their complete input bindings before starting any of
them. Preset slots retain their recorded speaker associations; an absent guest
does not renumber another guest.

Default commands operate on applicable parts in order. `--part N` targets one
part for prepare, preview, offset, start, inspect, sync, reset, or workspace
materialization. For example:

```sh
pod auphonic-inspect --session @1 --part 2
pod auphonic-sync --session @1 --part 2
pod workspace @1 --sources --part 2
```

All normal single-part commands, workspace names, and local imports remain
available without a selector. The existing explicit combined `pod auphonic @1`
convenience command remains available for a single part; multipart episodes use
the separate review/start sequence above.

## Repeated commands and interruptions

- Prepare reuses an existing production and verifies/resumes its source
  attachments. It never replaces another part's production automatically.
- Repeat start after interrupted polling/download/persistence. Completed parts
  are skipped; stored running or completed productions are inspected and resumed,
  not restarted. A local failure does not erase the production ID.
- Sync inspects and downloads without starting processing. It verifies the
  expected source ownership before accepting completed outputs, including
  productions processed outside Podline.
- A failed optional stem download or invalid archive does not invalidate a usable
  mix/transcript. Stem invalidation and replacement affect only the owning part.
- Output downloads use fresh staging inodes so interrupted retries cannot write
  through a hardlink into a previously registered checksum-cache entry.

### Unknown creation or start outcome

A server may create/start a production even if its response never reaches the
client. Podline therefore does not blindly retry creation/start POST requests.
Creation intent is durable and cannot be claimed again after another command has
already bound a production.

When creation has no stored ID, inspect the Auphonic account. The new production's
title includes a stable episode/part marker. Once its identity is established:

```sh
pod auphonic-sync <production-id> --session @1 --part 2
pod auphonic-prepare @1 --part 2
```

Explicit adoption checks the marker, preset/slots, and any existing source
attachments. An unmatched or unverified candidate is rejected. An incomplete
adopted production still needs preparation; sync does not falsely mark missing
inputs ready or start processing.

If a previous start is uncertain and remote state still reports not started,
inspect and explicitly sync before deciding to retry start. Running and completed
productions are resumed without another start request.

Reset is an explicit abandonment, not retry recovery:

```sh
pod auphonic-reset @1 --part 2
pod auphonic-prepare @1 --part 2
```

Multipart reset requires a part selection and accepts only known unstarted work.
It does not delete the remote production or requeue completed siblings. A new
production must be previewed again; old output rows cannot make a reset/reprocessed
part eligible for finalization.

## Ingest readiness and source integrity

Take lifecycle is authoritative. A session-level `recording` status can coexist
with stopped takes and complete downloadable tracks; Podline does not finalize or
modify the CozyTrack session to ingest them. `ct-list` keeps its existing
ready-session filter; use a known session ID directly when needed.

New multipart episode/part/source plans are registered atomically before media
transfer. Single-take recorder evidence is committed with raw-source registration
or verified local-import association, so an interruption cannot lose a known
partial/reconnect warning after storing the audio.

Each invocation owns disposable staging paths. Durable source registrations are
reused on retry; unregistered files are downloaded again. The final metadata
re-read detects a source that changed or became active during transfer and leaves
affected unfinished ingest blocked rather than ready for preparation.

Repeated ingest preserves the role map, source IDs, part numbers, existing output
names, and completed work. Later stopped takes can append before finalization.
Prepared-source changes, earlier inserted takes that would renumber existing
files, and additions after a final master is registered are rejected rather than
silently altering reviewed or assembled media.

Single-part local-import deduplication still joins verified source IDs by content
hash and heals interrupted legacy associations. Ambiguous content is reported,
not paired by filename or duration. A local folder matching only one part of a
multipart episode is not treated as a complete-episode duplicate.

## Timing warnings are guards, not repairs

Known partial audio, multiple recorder segments, incomplete segments, or
contradictory recorder timing are reasons to avoid duration-shortfall alignment.
The guard applies before the equal-duration/two-track shortcuts. Available
take-aware recorder creation times are checked with the existing 0.5-second
tolerance; a shorter early-ending track is not simply shifted to match the end.

Offset dry-run identifies the part and reason instead of presenting an unsafe
plan. Bulk `--apply` preflights the whole selection and writes no offsets if one
selected part is flagged. A healthy part remains eligible with `--part N`.
Warnings persist across unchanged ingest, preview, and start.

Manual repair remains possible:

```sh
pod workspace @1 --sources --part 2
pod auphonic-preview @1 --part 2
```

Use exact sources or optional processed stems in Cubase, or manually adjust
appropriate Auphonic offsets before explicitly starting. Flagged stem export does
not infer shifts, insert padding, or close reconnect gaps. Missing metadata is
unknown evidence; this is not universal sleep/disconnection detection.

Reconnect timestamp/DAW markers remain out of scope under
[issue #54](https://github.com/pashafateev/podline/issues/54).

## Editing, migration, and rollout

See [multipart editing](multipart-editing.md) for ordered workspace files,
processed FLACs, managed-file protections, and the one-final-master handoff.
See [recording-part ownership and migration](recording-parts.md) before upgrading
an existing SQLite database; back it up before the first command with the new
version. No existing S3 media is renamed or reuploaded by migration.

Deploy CozyTrack's additive take/segment metadata API before relying on multipart
ingest in Podline. Missing lifecycle metadata on a multi-take response is a clear
error, not permission to guess take boundaries. Historical single-part responses
and local imports retain their compatibility path.
