# Parallel development isolation

Revision: R1
Status: approved rule; frozen set not yet declared.

## What it is

Defines A-line GAP critical-path work and B-line `OUT_OF_GAP002_PATH` upstream work.

## Current truth

Once a GAP frozen set exists, B-line may not modify frozen files, interfaces, services, ports, health contracts, data paths, ownership contracts, or another declared frozen surface. A required frozen-surface change means STOP, reconcile/wait, and do not silently widen scope.

## Interfaces / dependencies

`plan/current-roadmap.md`; `dependencies/gap002-closure.md`.

## Read next

Use `runtime-upstream-boundary.md` for upstream replacement rules.

## Update rule

Update only when a separately authorized task declares or releases a freeze.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | Established dual-lane rule; no frozen set declared. |
