# Reliability and WebSocket gaps

Revision: R1
Status: LIVE15-owned data-truth semantics.

## What it is

Reliability determines sequence continuity, snapshot validity, gap closure, freshness, synchronization, and fail-closed behavior.

## Current truth

`WS-RESYNC-001 + GAP-002` remains active. Generic lifecycle may use Nomad/SCM; it cannot replace WebSocket gap/recovery semantics or Recorder truth.

## Interfaces / dependencies

`docs/continuous_recorder.md`; `docs/kalshi_native_architecture.md`; `capabilities/recorder.md`; `dependencies/gap002-closure.md`.

## Read next

Use `plan/current-roadmap.md` for sequence and `constraints/parallel-development.md` for the future freeze.

## Update rule

Update only for a durable reliability authority or GAP002 execution decision.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 authority baseline. |
