# Recorder throughput proof

Revision: R2
Status: evidence-gated; ST-005 is not resolved.

## What it is

The authority for measured Recorder throughput/catch-up evidence with safety intact.

## Current truth

`ST-005` is not resolved because UI-013 displays catch-up state. It requires measured
throughput/catch-up evidence and a valid 60-minute proof with Recorder safety intact. The proof may
classify a concrete bottleneck and justify a later, separately bounded smallest upstream
accelerator, queue, or analytical read path. It does not authorize DuckDB, Polars, Arrow, NATS, or
continued expansion of a bespoke LIVE15 throughput subsystem.

## Interfaces / dependencies

`PROJECT_PROGRESS.md`; `docs/evidence/st-005-current-main-preflight-20260829.md`; `truth.md`.

## Read next

Read `truth.md` for Recorder ownership and `../../../plan/current-roadmap.md` for current sequence.

## Update rule

Update only for measured proof or a durable ST-005 decision.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-V2-RECURSIVE-HIERARCHY-001 | Split ST-005 proof from Recorder truth. |
| R2 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 | Kept measured proof while removing implied custom-framework expansion. |
