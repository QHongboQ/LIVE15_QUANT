# Current roadmap

Revision: R6
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
  -> upstream-consolidation freeze
  -> runtime / lifecycle consolidation first (Nomad + Windows SCM)
  -> bounded responsibility-by-responsibility replacement
       owner resolution -> freeze legacy generic owner -> replace one responsibility
       -> verify -> retire corresponding old owner -> next responsibility
  -> final global legacy/dead-code cleanup
  -> CLEAN BASELINE
  -> resume data/training/model progression under existing gates
```

The Project Brain authority-consolidation step is complete in this bounded change and is being
closed by PR #122.

**NEXT:** begin the bounded **RUNTIME / LIFECYCLE CONSOLIDATION** responsibility class using
Nomad + Windows SCM. The next task must apply Existing Owner First to inventory remaining WinSW /
RuntimeSupervisor generic responsibilities, select exactly one responsibility, consult current
official Nomad/SCM sources, freeze only that legacy owner, replace and verify it, then retire only
its corresponding old owner. This does not authorize a big-bang RuntimeSupervisor/WinSW deletion
or parallel evolution of old and replacement owners.

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
