# Current roadmap

Revision: R2
Status: approved execution strategy.

## What it is

The sole current execution-sequence authority.

## Current truth

PHASE 1 — COMPLETE: GAP002 dependency-closure audit (see `dependencies/gap002-closure.md`).

PHASE 2 — COMPLETE / NO-OP: dependency closure found no generic critical-path migration required before GAP002; LIVE15 gap/data truth semantics remain local.

NEXT — PHASE 3: establish the GAP002 frozen baseline and dependency surface.

PHASE 4A — run `WS-RESYNC-001 + GAP-002` on that stabilized critical path.

PHASE 4B — IN PARALLEL, continue upstream replacement only for `OUT_OF_GAP002_PATH` work.

PHASE 5 — reconcile branches/results and release the GAP freeze.

PHASE 6+ — continue relevant migration, `ST-005`, deployment proof, holdout remediation, and `TRN-001` under their existing gates.

The parallel-isolation reserved surface is not a Phase-3 frozen runtime baseline. Direct GAP002
execution is gated by Phase 3, not by an additional pre-GAP migration task.

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
