# Current roadmap

Revision: R9
Status: approved execution strategy.

## What it is

The sole current execution-sequence authority.

## Current truth

`GAP002` is **CLOSED / PASS**. Its first Production FAIL and repaired second Production PASS remain
immutable evidence; the temporary GAP002 Phase 1/2/3/4A/4B/5 structure is historical and does not
direct future work.

Normal feature/model expansion is temporarily paused. The sole current mainline is:

```text
GAP002 CLOSED
  -> Project Brain authority consolidation COMPLETE (governance closeout; not an upstream replacement)
  -> upstream-consolidation freeze
  -> Runtime/Lifecycle consolidation COMPLETE / VERIFIED
       Nomad + Windows SCM lifecycle replacement ADOPTED / PRODUCTION VERIFIED
       RuntimeSupervisor RETIRED; managed paper_forward wrapper RETIRED
  -> Web Application Shell COMPLETE / VERIFIED
       React Admin + Material UI terminal adopted; legacy handwritten shell retired
  -> storage/archive capacity problem reprioritizes execution
  -> Commercial archive/package upstream assembly is the current NEXT responsibility class
       preserve LIVE15 replay/purge truth; prefer mature upstream serialization, compression,
       object-storage, and analytical components responsibility by responsibility
  -> Vector telemetry remains a deferred later candidate
  -> resume data/training/model progression under existing gates
```

The Project Brain authority-consolidation step is complete as a governance/status closeout; it was
not an upstream replacement. Runtime/Lifecycle consolidation is complete and verified: Recorder,
ControlCenter, and `kalshi_sdk_ws_shadow` are Nomad-managed; `pyth` and `coinbase` remain
in-process Recorder workers; RuntimeSupervisor and the managed `paper_forward` wrapper are retired.
Cold-boot proof passed, no dual owner remains, and final repository cleanup merged in PR #129.

The Web Application Shell replacement is **COMPLETE / VERIFIED**. React Admin + Material UI now
owns the packaged ControlCenter terminal; the legacy handwritten shell is retired. FastAPI typed
domain projections, Recorder truth, settlement truth, training truth, Hard Risk, and Production
authorization remain LIVE15-owned.

The previously selected Vector telemetry follow-up was valid at the post-Web reconciliation point,
but a subsequently confirmed storage/archive capacity problem changed the execution priority before
Vector adoption began. Vector remains an approved later candidate; it is not the current NEXT.

**NEXT:** advance the bounded **COMMERCIAL ARCHIVE/PACKAGE UPSTREAM ASSEMBLY** responsibility from
`docs/roadmap/COMMERCIAL_ARCHIVE_UPSTREAM_ASSEMBLY_001.md`. Preserve the existing HOT SQLite truth,
manifest/replay verification, contiguous-range purge authorization, restart recovery, and fail-closed
safety gates while evaluating mature upstream components one responsibility at a time. The current
preferred direction is Arrow schema/IPC plus Zstandard for raw immutable packaging, Parquet as a
derived analytical tier, S3/MinIO for cold durability, and DuckDB/PyArrow for analytical reads.
No replacement is adopted until its bounded benchmark, semantic-equivalence, recovery, integrity,
and rollback gates pass. This reprioritization does not authorize Production mutation or training.

Candidate-specific boundaries and classifications remain in
`docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md`; replacement mechanics remain in
`docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md`. Those are design/execution references, not
second current roadmaps. `PROJECT_PROGRESS.md` owns task status; `CURRENT_STATE.md` owns compact
whole-project orientation.

## Interfaces / dependencies

`constraints/execution/runtime-upstream-boundary.md`;
`docs/roadmap/COMMERCIAL_ARCHIVE_UPSTREAM_ASSEMBLY_001.md`;
`docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md`;
`docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md`.

## Read next

Use the selected capability and constraint authority before executing a phase.

## Update rule

Update only when an approved phase, freeze, or gate changes.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | Recorded approved dual-lane GAP002 strategy. |
| R2 | GAP002-DEPENDENCY-CLOSURE-AUDIT-001B | Marked Phase 1 complete; Phase 2 migration is complete/no-op; Phase 3 freeze remains pending. |
| R3 | GAP002-FROZEN-BASELINE-001 | Completed Phase 3 and routed next action to Phase 4A; GAP002 not executed. |
| R4 | GAP002-CRITICAL-PATH-UPSTREAM-REPLACEMENT-001 | Held Phase 4A for the selected Recorder lifecycle replacement baseline. |
| R5 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 | Closed obsolete GAP002 phase routing and established the sole upstream-consolidation mainline. |
| R6 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 closeout | Completed authority consolidation and selected runtime/lifecycle consolidation as the one concrete next responsibility class. |
| R7 | RUNTIME-LIFECYCLE-CONSOLIDATION-CLOSEOUT-001 | Recorded verified Nomad/SCM lifecycle adoption, RuntimeSupervisor retirement, and the Web Application Shell as the next generic replacement class. |
| R8 | PROJECT-BRAIN-POST-WEB-RECONCILIATION-001 | Recorded the complete/verified Web Application Shell replacement and selected Vector telemetry as the next generic replacement responsibility. |
| R9 | STORAGE-ARCHIVE-REPRIORITIZATION-001 | Preserved the valid post-Web Vector decision as historical context, then reprioritized current execution to the storage/archive packaging responsibility after the storage-capacity problem emerged. |
