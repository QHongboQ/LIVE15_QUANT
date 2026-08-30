# Current roadmap

Revision: R2
Status: approved execution strategy.

## What it is

The sole current execution-sequence authority.

## Current truth

PHASE 1 — COMPLETE: GAP002 dependency-closure audit (see `dependencies/gap002-closure.md`).

PHASE 2 — COMPLETE / NO-OP: dependency closure found no generic critical-path migration required before GAP002; LIVE15 gap/data truth semantics remain local.

PHASE 3 — COMPLETE: GAP002 frozen baseline and dependency surface recorded in
`docs/evidence/GAP002_FROZEN_BASELINE_001.md`.

NEXT — resolve and prove `RECORDER_LIFECYCLE_TO_NOMAD` before establishing a replacement baseline
for Phase 4A; authority: `docs/evidence/GAP002_CRITICAL_PATH_UPSTREAM_REPLACEMENT_001.md`.

PHASE 4B — IN PARALLEL, continue upstream replacement only for `OUT_OF_GAP002_PATH` work.

PHASE 5 — reconcile branches/results and release the GAP freeze.

PHASE 6+ — continue relevant migration, `ST-005`, deployment proof, holdout remediation, and `TRN-001` under their existing gates.

Phase 4A execution remains separately gated. The Phase-3 receipt is a before-replacement reference;
do not execute GAP002 until the selected lifecycle replacement has its own proven baseline.

## Interfaces / dependencies

`dependencies/gap002-closure.md`; `constraints/execution/parallel-development.md`; `docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md`.

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
