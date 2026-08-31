# Reliability and WebSocket gaps

Revision: R4
Status: GAP002 closed/pass; reliability authority retained.

## What it is

Reliability determines sequence continuity, snapshot validity, gap closure, freshness, synchronization, and fail-closed behavior.

## Current truth

`WS-RESYNC-001 + GAP-002` is **CLOSED / PASS**. PR #117 preserves the first Production FAIL and PR
#120 preserves the repaired second Production PASS. Buffered old-session events remained durable
history without regaining active authority; replacement-session snapshots restored authority and
all episode gaps recovered effectively. Generic lifecycle may use Nomad/SCM; it cannot replace
WebSocket gap/recovery semantics or Recorder truth.

## Interfaces / dependencies

`docs/continuous_recorder.md`; `docs/kalshi_native_architecture.md`; `recorder/truth.md`;
`../../dependencies/gap002-closure.md`.

## Read next

Use `../../plan/current-roadmap.md` for sequence. GAP002 freeze/parallel-development material is
historical evidence, not an active execution constraint.

## Update rule

Update only for a durable reliability authority or GAP002 execution decision.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 authority baseline. |
| R2 | PROJECT-BRAIN-V2-REVIEW-CLOSEOUT | Aligned tracked reliability work with roadmap gates. |
| R3 | GAP002-DEPENDENCY-CLOSURE-AUDIT-001B | Removed completed closure audit from direct-execution gates. |
| R4 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 | Reconciled merged GAP002 PASS and retired stale phase/freeze routing. |
