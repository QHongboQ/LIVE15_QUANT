# Current roadmap

Revision: R10
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
  -> Project Brain authority consolidation COMPLETE
  -> Runtime/Lifecycle consolidation COMPLETE / VERIFIED
  -> Web Application Shell COMPLETE / VERIFIED
  -> storage/archive capacity problem reprioritized execution
  -> commercial storage bakeoff COMPLETE
       Parquet + ZSTD selected for verified cold archive packaging
       Arrow IPC prototype/bakeoff is historical evidence, not the selected Production cold format
  -> Parquet HOT->COLD closed loop MERGED
       semantic digest + deterministic replay verification + manifest state + bounded purge gates retained
  -> named multi-root archive layout MERGED
       centralized manifest; one active writer root; historical roots fail closed
  -> Production Parquet Phase 1 attempt STOPPED safely before mutation
       historical PR #159 is a stop receipt only; HOT rows deleted = 0
  -> Runtime/deploy blocker RESOLVED in PR #167
       immutable runtime revisions; complete Production dependency closure; native Nomad start/revert ownership
  -> CURRENT NEXT: prepare and verify the immutable Production runtime, then perform a separately authorized
       Nomad Recorder rollout and verify the single-writer/heartbeat/WS/no-drop gates
  -> retire WTI completely as its own narrow task/PR (10 assets -> 9; no compatibility layer)
  -> rerun fresh PARQUET-PRODUCTION-ACCEPTANCE-PHASE1-002 while Recorder remains RUNNING
       one bounded historical unit -> Parquet+ZSTD -> semantic/replay verify -> VERIFIED/PURGE_ELIGIBLE
       STOP BEFORE PURGE; PRODUCTION HOT ROWS DELETED = 0
  -> only after separate human authorization may a tiny bounded Production purge be evaluated
  -> Vector telemetry remains deferred
  -> resume data/training/model progression under existing gates
```

The Project Brain authority-consolidation step is complete as a governance/status closeout; it was
not an upstream replacement. Runtime/Lifecycle consolidation is complete and verified: Recorder,
ControlCenter, and `kalshi_sdk_ws_shadow` are Nomad-managed; `pyth` and `coinbase` remain
in-process Recorder workers; RuntimeSupervisor and the managed `paper_forward` wrapper are retired.
Cold-boot proof passed and no dual owner remains.

The Web Application Shell replacement is **COMPLETE / VERIFIED**. React Admin + Material UI owns the
packaged ControlCenter terminal; the legacy handwritten shell is retired. FastAPI typed domain
projections, Recorder truth, settlement truth, training truth, Hard Risk, and Production authorization
remain LIVE15-owned.

The storage/archive responsibility is no longer at candidate-selection stage. The bounded commercial
bakeoff in PR #157 selected Parquet + ZSTD for the verified cold path; PR #158 merged the Production-capable
Parquet closed loop while leaving activation disabled; PR #160 added named multi-root storage. The earlier
Arrow IPC direction is retained only as benchmark/prototype history and must not be treated as the current
selected cold format.

PR #159 is historical evidence of a correctly stopped first Production acceptance attempt. It predates the
later runtime/deploy work and must not be merged or extended as the current execution branch. A fresh Phase
1 acceptance must start from current `main` after the runtime rollout and WTI retirement gates are closed.

PR #167 replaced movable-venv promotion and custom lifecycle recovery with immutable runtime revisions plus
native Nomad lifecycle ownership. Runtime preparation is non-disruptive; it does not require stopping the
Recorder. A running workload changes runtime only through a separately reviewed Nomad deployment. `MERGED`
does not mean the new runtime has been prepared or deployed on the host.

**NEXT:** execute the runtime preparation/verification sequence from the runtime authority, then a separately
authorized Recorder deployment Preview/Apply/verification. Do not activate archive or purge during this
step. After runtime deployment verification, retire WTI as its own narrow responsibility before starting a
fresh `PARQUET-PRODUCTION-ACCEPTANCE-PHASE1-002`.

Vector telemetry remains a deferred later candidate. This roadmap does not authorize Production purge,
training, holdout access, Hard Risk changes, or trading writes.

## Interfaces / dependencies

`dependencies/platform/runtime-ownership.md`;
`constraints/execution/runtime-upstream-boundary.md`;
`docs/roadmap/COMMERCIAL_ARCHIVE_UPSTREAM_ASSEMBLY_001.md`;
`docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md`;
`docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md`.

## Read next

Use the selected capability and constraint authority before executing a phase. For the immediate next step,
read `dependencies/platform/runtime-ownership.md` before any host Runtime/Recorder action.

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
| R9 | STORAGE-ARCHIVE-REPRIORITIZATION-001 | Reprioritized current execution to the storage/archive packaging responsibility after the storage-capacity problem emerged. |
| R10 | PROJECT-BRAIN-STORAGE-RUNTIME-RECONCILE-001 | Reconciled PRs #157/#158/#160/#167: Parquet+ZSTD is selected and implemented, the first Production acceptance remains a historical stop receipt, and the immediate next gate is immutable Runtime preparation/Recorder rollout before WTI retirement and fresh Phase1-002. |
