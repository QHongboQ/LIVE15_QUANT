# Current roadmap

Revision: R10
Status: approved execution strategy.

## What it is

The sole current execution-sequence authority.

## Current truth

`GAP002` is **CLOSED / PASS**. Its Production FAIL/PASS receipts remain immutable evidence; the old
GAP002 phase structure is historical and does not direct future work.

Normal feature/model expansion is temporarily paused. The sole current mainline is:

```text
GAP002 CLOSED
  -> Project Brain authority consolidation COMPLETE
  -> upstream-consolidation freeze
  -> Runtime/Lifecycle consolidation COMPLETE / VERIFIED
  -> Web Application Shell COMPLETE / VERIFIED
  -> storage/archive capacity problem reprioritizes execution
  -> COMMERCIAL ARCHIVE/PACKAGE UPSTREAM ASSEMBLY = CURRENT RESPONSIBILITY
       PR #157 bakeoff COMPLETE: Parquet + ZSTD selected
       PR #158 HOT->COLD closed loop MERGED; Production activation still disabled
       PR #160 named multi-root archive layout MERGED
       PR #159 = historical safe-stop receipt only; HOT rows deleted = 0
       PR #167 = Runtime/deploy blocker resolved in code; host rollout still pending
       immediate gate: prepare/verify immutable Production Runtime, then separately authorize
       Nomad Recorder rollout and verify single-writer/heartbeat/WS/no-drop
       next narrow gate: retire WTI completely 10 -> 9, no compatibility layer
       then fresh PARQUET-PRODUCTION-ACCEPTANCE-PHASE1-002 with Recorder RUNNING
       STOP BEFORE PURGE; PRODUCTION HOT ROWS DELETED = 0
  -> Vector telemetry remains deferred
  -> resume data/training/model progression under existing gates
```

The archive responsibility is no longer at candidate-selection stage. Parquet + ZSTD is the selected
verified cold format; Arrow IPC is retained only as benchmark/prototype history. Existing LIVE15
semantic digest, deterministic replay verification, manifest state, contiguous-range purge
authorization, restart recovery, and fail-closed storage behavior remain mandatory.

PR #167 replaced movable-venv promotion/custom recovery with immutable runtime revisions and native
Nomad lifecycle ownership. `MERGED != DEPLOYED`: Runtime preparation and Recorder rollout remain a
gate inside the archive/package responsibility because Production Parquet acceptance requires the
PyArrow-capable runtime. Runtime preparation must not stop Recorder; rollout is separately reviewed.

PR #159 must not be merged or extended as the current execution branch. After Runtime rollout is
verified, WTI retirement is a separate narrow task, followed by a fresh Phase1-002 from current main.

**NEXT:** advance the bounded **COMMERCIAL ARCHIVE/PACKAGE UPSTREAM ASSEMBLY** responsibility by
closing its current Runtime deployment gate: prepare/verify the immutable Production Runtime, then
run Recorder deploy Preview and a separately authorized rollout/verification. Do not activate archive
or purge during that gate. WTI retirement and fresh Phase1-002 follow as separate narrow tasks.

Vector telemetry remains deferred. This roadmap does not authorize Production purge, training,
holdout access, Hard Risk changes, or trading writes.

## Interfaces / dependencies

`dependencies/platform/runtime-ownership.md`;
`constraints/execution/runtime-upstream-boundary.md`;
`docs/roadmap/COMMERCIAL_ARCHIVE_UPSTREAM_ASSEMBLY_001.md`;
`docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md`;
`docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md`.

## Read next

Use the selected capability/constraint authority before executing a gate. For the immediate Runtime
gate, read `dependencies/platform/runtime-ownership.md` before any host Runtime/Recorder action.

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
| R9 | STORAGE-ARCHIVE-REPRIORITIZATION-001 | Reprioritized current execution to storage/archive packaging after the capacity problem emerged. |
| R10 | PROJECT-BRAIN-STORAGE-RUNTIME-RECONCILE-001 | Reconciled PRs #157/#158/#160/#167: Parquet+ZSTD is selected/implemented, #159 is historical stop evidence, and the archive mainline now closes Runtime rollout, WTI retirement, then fresh Phase1-002. |
