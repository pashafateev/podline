# Spec Delta

## Purpose

Represent a recorded episode as ordered, independently identifiable recording parts while preserving source audio and consistent speaker identity across host-controlled stops and restarts.

## ADDED Requirements

### Requirement: Take-based episode ingest

Podline SHALL import a CozyTrack session as one episode, grouping tracks by take ID and ordering nonempty takes by their start time with a deterministic tie-breaker. Participant joins, departures, and reconnects within a take SHALL NOT create additional parts.

#### Scenario: Three takes with changing participant counts
- **WHEN** one session contains three stopped takes with three, three, and two complete participant tracks respectively
- **THEN** one `pod ct-ingest <session-id>` registers one episode with three ordered parts containing exactly those track groups
- **AND** the eight source tracks are not classified as eight simultaneous participants

#### Scenario: Reconnect during an ongoing take
- **WHEN** one logical participant track contains multiple recorder segments under one take ID
- **THEN** that track remains within the same part
- **AND** the reconnect evidence is retained for the part's timing warning

#### Scenario: Participant leaves before the next take
- **WHEN** a guest appears in Parts 1 and 2 but not Part 3
- **THEN** Part 3 contains only its actual participants, without a fabricated silent guest track

### Requirement: Stable episode speaker identity

Podline SHALL persist a stable participant-to-role association across an episode. Track list order, display-name changes, missing participants, and repeated ingest SHALL NOT change an already assigned speaker role.

#### Scenario: Shuffled source order
- **WHEN** API track order differs across three takes containing the same participant IDs
- **THEN** each participant retains its episode role in raw tracks, processing input associations, and repair manifests

#### Scenario: Guest disappears and another guest remains
- **WHEN** a later part contains an already assigned `guest2` but no `guest1`
- **THEN** the remaining guest keeps the `guest2` identity
- **AND** processing uses the number of participants present rather than the highest role suffix

#### Scenario: Ambiguous identity
- **WHEN** source metadata cannot unambiguously associate the tracks of a take with participant roles
- **THEN** Podline reports the affected take and track IDs instead of guessing from display names or merging tracks
- **AND** it does not silently omit or alter the source recordings

### Requirement: Ingest metadata contract

The CozyTrack ingest detail response SHALL retain existing fields and additionally expose take IDs, lifecycle state and start/stop timestamps, plus each track's partial flag, segment count, and incomplete-segment count. Podline SHALL distinguish unavailable metadata from an explicit healthy value.

#### Scenario: Known reconnect is not marked partial
- **WHEN** a complete track has `partial=false` but more than one recorder segment
- **THEN** the ingest contract exposes that segment count
- **AND** Podline records known reconnect evidence without claiming the audio is complete or aligned

#### Scenario: Metadata is absent on an older response
- **WHEN** a legacy single-part response omits take lifecycle or segment summaries
- **THEN** Podline supports the legacy single-part path and reports the missing evidence as unavailable
- **AND** it does not manufacture segment counts, stopped-take state, or a guarantee that no disconnection occurred

### Requirement: Recording readiness and unambiguous grouping

For take-aware multipart ingest, Podline SHALL require stopped takes and downloadable complete tracks before media processing. An episode-level `recording` status alone SHALL NOT reject stopped takes. Mixed or unresolved take associations SHALL produce an actionable error instead of heuristic grouping.

#### Scenario: Session remains recording after its takes stop
- **WHEN** the session status is `recording` but every exposed take is stopped and every included track is complete
- **THEN** ingest can proceed without finalizing or otherwise modifying the CozyTrack session

#### Scenario: Recording or upload is still active
- **WHEN** a take remains active or an included track is not complete
- **THEN** ingest reports the take or track that is not ready
- **AND** it does not start Auphonic or silently import only a ready subset as the complete episode

#### Scenario: Ambiguous take associations
- **WHEN** a session mixes tracks with and without take IDs, references a missing take, or exposes multiple take IDs without their ordering/lifecycle metadata
- **THEN** ingest stops with the unresolved source IDs and required metadata identified
- **AND** it does not group tracks by duration, filename, or approximate creation-time clusters

### Requirement: Durable part ownership and repeatable ingest

Part source and pre-edit artifacts SHALL remain associated with their episode and part. Repeated ingest SHALL reuse registered source artifacts and resume unfinished parts without replacing another part's media. Existing explicit exclusion behavior SHALL remain available without silently excluding an ambiguous role across parts.

#### Scenario: Same speaker appears in several parts
- **WHEN** Parts 1 and 2 both contain a `host1` raw track
- **THEN** both artifacts remain independently addressable under the same episode
- **AND** updating Part 2 does not change Part 1's artifact or source association

#### Scenario: Ingest stops after one part is durable
- **WHEN** ingest is interrupted after Part 1 is registered but before the remaining parts finish
- **THEN** a repeated command preserves Part 1 and resumes the unfinished source registrations
- **AND** the episode is not presented as fully ingested until its selected source snapshot is complete

#### Scenario: New stopped take is added before finalization
- **WHEN** repeated ingest finds a new complete stopped take ordered after existing parts and the episode has not been finalized
- **THEN** Podline appends it without renumbering existing parts, changing existing roles, or reprocessing their completed productions
- **AND** any subsequent start still requires the new part to be prepared and previewed

#### Scenario: A processed source changes
- **WHEN** repeated ingest detects changed source membership or materialization metadata for an already prepared part
- **THEN** Podline reports the affected part and refuses automatic source replacement or production recreation

#### Scenario: A source change appears after finalization
- **WHEN** repeated ingest finds a new take or changed source after an episode's final master has been registered
- **THEN** it reports that the finalized episode cannot be changed automatically
- **AND** the existing final master, refreshed transcript, and publishing progress remain intact

#### Scenario: A role exclusion matches several parts
- **WHEN** `--exclude` names a role that matches more than one source track
- **THEN** ingest requires an exact track ID or an unambiguous part-qualified selector rather than dropping that role from every part

### Requirement: Single-part and migration compatibility

New single-take sessions and local imports SHALL behave as one default part without additional required commands. Migration of existing sessions SHALL preserve identity, workflow progress, source provenance, current transcripts, media objects, and existing production IDs without triggering remote processing.

#### Scenario: Normal single-take recording
- **WHEN** a supported uninterrupted recording is ingested and processed
- **THEN** the existing commands require no part selector and use one Auphonic production
- **AND** introducing part ownership causes no extra media copies or paid processing

#### Scenario: Existing completed or finalized session
- **WHEN** migration runs on an existing single-part session
- **THEN** its completed production and current final-master/transcript associations remain usable
- **AND** repeating migration does not create duplicate default parts or reset completed work

#### Scenario: Local import deduplicated by CozyTrack ingest
- **WHEN** a legacy single-part local import content-matches a CozyTrack session
- **THEN** the surviving episode retains its identity and gains verified take/source associations without duplicate media registration
- **AND** interrupted source-ID reconciliation remains recoverable by verified content matching
