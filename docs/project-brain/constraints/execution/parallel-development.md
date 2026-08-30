# Parallel development isolation

Revision: R2
Status: approved rule; GAP002 Phase-3 frozen set declared.

## What it is

Defines A-line GAP critical-path work and B-line `OUT_OF_GAP002_PATH` upstream work.

## Current truth

The GAP002 Phase-3 frozen set is recorded in
`docs/evidence/GAP002_FROZEN_BASELINE_001.md`. B-line may not modify frozen files, interfaces, services, ports,
health contracts, data paths, ownership contracts, or another declared frozen surface. A required
frozen-surface change means STOP, reconcile/wait, and do not silently widen scope.

## Interfaces / dependencies

`../../plan/current-roadmap.md`; `../../dependencies/gap002-closure.md`.

## Read next

Use `runtime-upstream-boundary.md` for upstream replacement rules.

## Update rule

Update only when a separately authorized task declares or releases a freeze.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | Established dual-lane rule; no frozen set declared. |
| R2 | GAP002-FROZEN-BASELINE-001 | Declared the Phase-3 frozen surface; Phase 4A remains separate. |
