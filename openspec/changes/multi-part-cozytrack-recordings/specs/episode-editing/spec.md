# Spec Delta

## Purpose

Provide one durable editing workspace for all parts of an episode, including optional repair assets, and reconnect the editor's assembled export to the existing final-master publishing workflow.

## ADDED Requirements

### Requirement: Ordered multipart editing workspace

A multipart episode SHALL have one editing workspace containing clearly ordered mixes and corresponding per-part transcripts. Workspace refresh SHALL preserve part boundaries and SHALL NOT concatenate audio or present one part's transcript as the transcript of the assembled episode.

#### Scenario: Three processed parts
- **WHEN** an episode was initially imported with three parts and all three are complete
- **THEN** the workspace contains `part-01.mp3`, `part-02.mp3`, `part-03.mp3` and their corresponding `.vtt` files
- **AND** its manifest associates each output with its take, part order, production, and checksum

#### Scenario: Some parts are still pending
- **WHEN** one part is complete and other parts are unfinished
- **THEN** its completed outputs remain available in the same workspace
- **AND** the manifest identifies unfinished parts without presenting the workspace as a fully processed episode

#### Scenario: Refresh after reprocessing Part 2
- **WHEN** the user refreshes the workspace after only Part 2 changes
- **THEN** the updated Part 2 files replace only their Podline-managed counterparts
- **AND** other parts, Cubase projects, user exports, and unrelated files remain intact

### Requirement: Single-part workspace compatibility

A single-part session SHALL retain the existing workspace location, generated filenames, optional flat stems layout, and final-master path resolution. Internal part ownership SHALL NOT require the user to reorganize a normal episode's files.

#### Scenario: Existing single-part workspace
- **WHEN** a migrated single-part session's workspace is refreshed
- **THEN** its existing pre-edit mix/transcript naming and `stems/` layout remain usable
- **AND** the user is not required to rename a Cubase project or move an exported master

#### Scenario: A materialized single-part episode gains another take
- **WHEN** a later stopped take is appended before finalization
- **THEN** already materialized Part 1 filenames and repair assets are not renamed or moved
- **AND** new part outputs use separate ordered names and the manifest identifies the full part order

### Requirement: Source-track repair access

Podline SHALL retain original source artifacts and provide an explicit workspace source-export option that is usable before paid processing. Exported sources SHALL identify their part and participant/source track and SHALL NOT be shifted, padded, concatenated, or replaced by processed stems.

#### Scenario: Flagged part needs manual inspection
- **WHEN** the user requests `pod workspace <episode> --sources --part 2` after ingest
- **THEN** Part 2's registered source recordings and timing-warning details are materialized in the episode workspace without requiring an Auphonic mix
- **AND** their bytes match the registered source checksums

#### Scenario: Source export is repeated
- **WHEN** the source-export command is repeated
- **THEN** Podline reuses verified source artifacts and refreshes only its managed source copies
- **AND** user-created Cubase files and repaired exports are preserved

### Requirement: Optional processed stems per part

The existing optional Individual Tracks / FLAC workflow SHALL remain available per part, with exact production-track-to-speaker mapping and separate repair manifests. Invalid archives SHALL fail safely without replacing original sources or another part's stems.

#### Scenario: Multipart stem workspace
- **WHEN** the user requests stems for processed parts with available valid FLAC exports
- **THEN** the workspace separates them under `stems/part-01/`, `stems/part-02/`, and subsequent part directories
- **AND** each part's repair manifest records the correct production, roles, track bindings, checksums, offsets, and loudness warning

#### Scenario: Unsafe timing during stem export
- **WHEN** a flagged part's processed stems are materialized
- **THEN** Podline does not infer or apply alignment from duration shortfalls
- **AND** any alignment already rendered by Auphonic or explicitly configured by the user is documented without claiming reconnect gaps were repaired

#### Scenario: Existing archive protections
- **WHEN** an individual-track archive contains missing, duplicate, unexpected, unsafe-path, symlink, or non-FLAC entries
- **THEN** it is rejected without mutating another part's registered stem set
- **AND** a usable mix and transcript remain available for normal editing

### Requirement: Episode readiness reflects all parts

An episode SHALL become ready for the editor's final master only when every required part has its registered processed mix and transcript. A failed or unfinished part SHALL remain identifiable, and a retry SHALL NOT demote completed sibling parts.

#### Scenario: Only two of three parts are complete
- **WHEN** Parts 1 and 3 are complete but Part 2 is unfinished
- **THEN** the episode is not reported as `waiting_for_finalize`
- **AND** finalization reports Part 2 as blocking instead of accepting a last-part-wins artifact

#### Scenario: Last unfinished part completes
- **WHEN** synchronization registers the final required part's mix and transcript
- **THEN** the episode reaches `waiting_for_finalize` with all ordered outputs available in one workspace

### Requirement: Preserve the single-final-master handoff

After manual assembly, finalize SHALL accept one edited episode master. Transcript refresh, metadata generation, and draft creation SHALL operate on the episode's final-master/refreshed-transcript artifacts, not individual part outputs. Part processing SHALL NOT overwrite these episode-level artifacts or create separate drafts.

#### Scenario: Assemble three parts in Cubase
- **WHEN** all parts are ready and the user supplies one assembled export to finalize
- **THEN** Podline registers one episode final master
- **AND** transcript refresh produces one transcript from that master for the existing metadata and draft workflow

#### Scenario: No assembled transcript yet
- **WHEN** a multipart episode is finalized but its final-master transcript has not been refreshed
- **THEN** metadata generation and draft creation identify transcript refresh as required
- **AND** they do not use one part's transcript or automatically concatenate part transcripts

#### Scenario: Resume final transcript refresh
- **WHEN** final-master transcript refresh is interrupted after its production is recorded
- **THEN** retry reuses the existing episode-level refresh production independently of all part-production IDs

#### Scenario: Part synchronization after finalization
- **WHEN** a completed part is synchronized after the episode's final master and refreshed transcript exist
- **THEN** those episode-level artifacts, metadata, draft association, and post-edit workflow state remain unchanged
