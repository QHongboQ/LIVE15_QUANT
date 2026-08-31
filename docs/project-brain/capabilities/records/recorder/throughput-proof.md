# Recorder throughput proof

Revision: R3
Status: on-demand measurement contract; standalone ST-005 is cancelled/superseded.

## What it is

The authority for measured Recorder throughput/catch-up evidence with safety intact.

## Current truth

The standalone `ST-005` custom-throughput optimization task is retired and is not a current
blocker. Its bounded 60-minute throughput/catch-up measurement contract remains available on
demand when a later bounded task must classify a concrete bottleneck. That evidence may inform
whether the smallest conditional upstream accelerator, queue, or analytical read path deserves a
separate proposal. No historical 60-minute proof is claimed to have passed. It does not authorize DuckDB, Polars, Arrow, NATS, or continued expansion of a bespoke LIVE15 throughput subsystem.

## Interfaces / dependencies

`PROJECT_PROGRESS.md`; `docs/evidence/st-005-current-main-preflight-20260829.md`; `truth.md`.

## Read next

Read `truth.md` for Recorder ownership and `../../../plan/current-roadmap.md` for current sequence.

## Update rule

Update only when an authorized bounded measurement changes the evidence contract or its result.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-V2-RECURSIVE-HIERARCHY-001 | Split ST-005 proof from Recorder truth. |
| R2 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 | Kept measured proof while removing implied custom-framework expansion. |
| R3 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 closeout | Retired standalone ST-005 while retaining its bounded measurement contract on demand. |
