# Current roadmap

Revision: R7
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
  -> bounded responsibility-by-responsibility replacement
       owner resolution -> freeze legacy generic owner -> replace one responsibility
       -> verify -> retire corresponding old owner -> next responsibility
  -> Web Application Shell is the next generic replacement class
  -> Vector telemetry remains a later candidate
  -> resume data/training/model progression under existing gates
```

The Project Brain authority-consolidation step is complete as a governance/status closeout; it was
not an upstream replacement. Runtime/Lifecycle consolidation is complete and verified: Recorder,
ControlCenter, and `kalshi_sdk_ws_shadow` are Nomad-managed; `pyth` and `coinbase` remain
in-process Recorder workers; RuntimeSupervisor and the managed `paper_forward` wrapper are retired.
Cold-boot proof passed, no dual owner remains, and final repository cleanup merged in PR #129.

**NEXT:** select the bounded **WEB APPLICATION SHELL** replacement class, with React Admin +
Material UI as the candidate upstream owner. Scope is limited to generic routing, tables,
loading/error handling, refresh plumbing, and theme/shell. Preserve the FastAPI typed domain API,
Recorder truth, settlement truth, training truth, Hard Risk, and Production authorization. This
closeout does not begin the React Admin migration or authorize Production mutation.

Candidate-specific boundaries and classifications remain in
`docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md`; replacement mechanics remain in
`docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md`. Those are design/execution references, not
second current roadmaps. `PROJECT_PROGRESS.md` owns task status; `CURRENT_STATE.md` owns compact
whole-project orientation.

## Interfaces / dependencies

`constraints/execution/runtime-upstream-boundary.md`;
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
