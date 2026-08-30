# Reliability and WebSocket gaps

Revision: R3
Status: tracked; direct execution gated by the current roadmap.

## What it is

Reliability determines sequence continuity, snapshot validity, gap closure, freshness, synchronization, and fail-closed behavior.

## Current truth

`WS-RESYNC-001 + GAP-002` remains the tracked reliability workstream. Dependency closure is
complete with no required pre-GAP migration; direct execution now awaits critical-path prerequisite
stabilization and the GAP002 frozen baseline; `GAP002_DEPENDENCY_AUDIT_EXECUTED = YES`. Generic
lifecycle may use Nomad/SCM; it cannot replace WebSocket gap/recovery semantics or Recorder truth.

## Interfaces / dependencies

`docs/continuous_recorder.md`; `docs/kalshi_native_architecture.md`; `recorder/truth.md`;
`../../dependencies/gap002-closure.md`.

## Read next

Use `../../plan/current-roadmap.md` for sequence and
`../../constraints/execution/parallel-development.md` for the future freeze.

## Update rule

Update only for a durable reliability authority or GAP002 execution decision.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 authority baseline. |
| R2 | PROJECT-BRAIN-V2-REVIEW-CLOSEOUT | Aligned tracked reliability work with roadmap gates. |
| R3 | GAP002-DEPENDENCY-CLOSURE-AUDIT-001B | Removed completed closure audit from direct-execution gates. |
