# Parallel development isolation

Revision: R3
Status: historical GAP002 isolation record; freeze released.

## What it is

Preserves the former A-line/B-line isolation rule used while GAP002 was active. It is not a current
execution route.

## Current truth

The GAP002 Phase-3 frozen set is recorded in
`docs/evidence/GAP002_FROZEN_BASELINE_001.md`. GAP002 is now CLOSED / PASS and that temporary freeze
is released. The general rule remains historical evidence: while a bounded freeze is active,
parallel work must not silently modify its declared surface.

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
| R2 | GAP002-FROZEN-BASELINE-001 | Declared the Phase-3 frozen surface; Phase 4A remained separate. |
| R3 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 | Marked the completed GAP002 freeze as historical and non-routing. |
