# Current roadmap

Revision: R1
Status: approved execution strategy.

## What it is

The sole current execution-sequence authority.

## Current truth

PHASE 1 — GAP002 dependency-closure audit.

PHASE 2 — upstreamize only generic infrastructure actually on the GAP002 critical path; LIVE15 gap/data truth semantics remain local.

PHASE 3 — establish the GAP002 frozen baseline and dependency surface.

PHASE 4A — run `WS-RESYNC-001 + GAP-002` on that stabilized critical path.

PHASE 4B — IN PARALLEL, continue upstream replacement only for `OUT_OF_GAP002_PATH` work.

PHASE 5 — reconcile branches/results and release the GAP freeze.

PHASE 6+ — continue relevant migration, `ST-005`, deployment proof, holdout remediation, and `TRN-001` under their existing gates.

`GAP002_DEPENDENCY_AUDIT_EXECUTED = NO`.

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
