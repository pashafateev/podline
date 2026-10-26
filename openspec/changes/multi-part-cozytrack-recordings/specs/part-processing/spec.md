# Spec Delta

## Purpose

Process an episode's recording parts independently through the existing Auphonic review workflow, with reliable resumption and safeguards against applying invalid duration-based alignment.

## ADDED Requirements

### Requirement: Episode-oriented part commands

Prepare, offset, preview, start, inspect, reset, status, and sync SHALL identify work by episode and expose ordered per-part results. Commands SHALL support selecting a part for targeted inspection or recovery without treating it as an unrelated episode.

#### Scenario: Default operation across three parts
- **WHEN** the user runs an episode-oriented preparation or synchronization command without a part selector
- **THEN** the command handles the episode's applicable parts in order and reports each part's production and progress

#### Scenario: Targeted recovery
- **WHEN** the user selects Part 2 for inspection or recovery
- **THEN** the command affects only Part 2's processing state and artifacts
- **AND** another part's production cannot be silently adopted into Part 2

#### Scenario: Multipart reset is explicit
- **WHEN** a multipart episode has more than one prepared production and the user requests reset without selecting a part
- **THEN** the command requires an explicit part selection before abandoning a production
- **AND** the existing unqualified reset remains valid for a single-part session

### Requirement: One supported production per part

Podline SHALL prepare one Auphonic production per part using the existing preset for that part's supported participant count. Preparation SHALL attach only that part's source inputs and retain their stable speaker associations. Unsupported counts SHALL fail before creating a production.

#### Scenario: Three plus three plus two
- **WHEN** the episode has participant counts of three, three, and two
- **THEN** prepare uses the three-speaker preset for Parts 1 and 2 and the two-speaker preset for Part 3
- **AND** each production references only its own source tracks

#### Scenario: Unsupported part
- **WHEN** a part has a participant count outside the existing two-to-four-participant capability
- **THEN** preparation identifies that part as unsupported without creating a production for it
- **AND** it does not split that take into artificial parts or discard a track to fit a preset

### Requirement: Preserve review before start

Ingest, prepare, offset, preview, inspect, workspace creation, and sync SHALL NOT start paid processing. The multipart start command SHALL require every selected not-yet-started part to have been previewed. A timing warning SHALL remain visible without introducing a new mandatory start-override flag.

#### Scenario: Preview lists all edit pages
- **WHEN** the user previews a prepared multipart episode
- **THEN** Podline displays the ordered edit pages and input associations for its parts
- **AND** it does not start any production

#### Scenario: A new part has not been reviewed
- **WHEN** start selects both a previewed part and a part that has not been previewed
- **THEN** it reports the missing preview before starting any selected not-yet-started production

#### Scenario: Manual alignment with a timing warning
- **WHEN** the user previews a flagged part, makes any needed manual adjustment, and explicitly starts it
- **THEN** the existing preview/start control remains usable without automatic duration-derived alignment
- **AND** the timing warning is not cleared merely because preview or start occurred

### Requirement: Checkpoint and resume each part

Repeated processing commands SHALL reuse each part's stored production and completed outputs. Interrupted attachment, starting, polling, downloading, or output registration SHALL resume from durable state after inspecting remote state when needed, without restarting completed processing or replacing another part's files.

#### Scenario: Attachment interrupted
- **WHEN** preparation creates a production but fails while attaching its inputs
- **THEN** retry reuses that production and completes the missing verified input associations
- **AND** it does not create a replacement production

#### Scenario: Middle part interrupted
- **WHEN** Part 1 is complete, Part 2 is in progress or has a local download failure, and Part 3 has not started
- **THEN** retry preserves Part 1, resumes Part 2's stored production, and handles Part 3 using its own stored preparation and preview state
- **AND** every output remains associated with the correct part

#### Scenario: Remote start succeeded but its response was lost
- **WHEN** a start request has an ambiguous result
- **THEN** retry inspects the existing production before deciding whether a start is still necessary
- **AND** it never restarts a completed or already-running production

#### Scenario: Remote creation result is unknown
- **WHEN** a production-creation request might have succeeded but no production ID was durably recorded
- **THEN** Podline reports the affected part as requiring explicit reconciliation
- **AND** neither automatic HTTP retry nor a repeated command blindly creates another production

#### Scenario: Concurrent preparation
- **WHEN** two commands attempt to prepare the same part concurrently
- **THEN** at most one issues a production-creation request
- **AND** the other reports or reuses the existing durable operation rather than creating a duplicate

### Requirement: Guard known unreliable timing

Podline SHALL flag a part when its included source tracks have known reconnect segments, partial media, incomplete segments, or contradictory recorder timing. Such a part SHALL NOT receive duration-derived automatic offsets, regardless of whether it has two, three, or four tracks or equal durations.

#### Scenario: Reconnect with otherwise complete tracks
- **WHEN** a track is complete and not partial but has more than one recorder segment
- **THEN** the containing part is flagged with that track and the reconnect reason
- **AND** a dry-run offset reports manual alignment is needed rather than presenting a confident automatic offset

#### Scenario: Partial two-track recording
- **WHEN** one of two source tracks is partial or has an incomplete segment
- **THEN** automatic offset application is refused for that part
- **AND** the shorter track is not shifted merely to match the longer track's end

#### Scenario: Early ending disagrees with recorder timing
- **WHEN** stored duration differences conflict with the available recorder creation-time differences beyond the existing timing tolerance
- **THEN** Podline identifies the part and conflicting tracks and refuses automatic duration-derived alignment
- **AND** the same rule applies to take-aware two-track and larger parts

#### Scenario: Equal durations do not erase a known problem
- **WHEN** a part has equal source durations but a known reconnect or partial flag
- **THEN** the timing warning remains visible instead of declaring the part safely aligned

#### Scenario: Bulk offset application includes a flagged part
- **WHEN** the user requests `--apply` across a selection containing an unsafe part
- **THEN** Podline reports the blocking part before making any automatic offset writes for that selection
- **AND** the user can select an unaffected part to apply its validated offsets separately

### Requirement: Conservative alignment evidence

Podline SHALL retain normal duration-derived alignment for supported recordings without a known timing risk, using available recorder timing to validate shared-stop assumptions. Missing metadata SHALL NOT be described as proof of reliable timing. The system SHALL NOT claim universal disconnection detection or reconstruct reconnect gaps.

#### Scenario: Healthy part and shorter late-starting track
- **WHEN** an otherwise eligible part has shorter tracks whose duration differences agree with the available recorder-start evidence
- **THEN** offset continues to offer the existing duration-derived adjustments within that part only

#### Scenario: Historical two-track or local recording
- **WHEN** a legacy single-part or local recording lacks the new take metadata and has no known timing warning
- **THEN** its existing supported offset behavior remains available
- **AND** Podline does not claim to have ruled out sleep or disconnection problems

### Requirement: Part-isolated output synchronization

Synchronization SHALL register a production's mix, transcript, and optional stem set only for its owning part. An optional stem failure SHALL NOT invalidate that part's usable mix/transcript or another part's stems.

#### Scenario: Reprocess one part without stems
- **WHEN** Part 2's completed production is synchronized without an individual-track output
- **THEN** only Part 2's obsolete processed-stem set is invalidated
- **AND** Parts 1 and 3 and all original source tracks remain unchanged

#### Scenario: Output persistence interrupted
- **WHEN** a part's mix is durable but transcript persistence or workspace creation fails
- **THEN** retry can complete the same production's outputs without starting processing again
- **AND** the episode does not become ready for finalization until every required part output is registered
