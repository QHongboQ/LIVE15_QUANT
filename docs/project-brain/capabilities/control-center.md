# ControlCenter

Revision: R2
Status: Nomad-owned, verified.

## What it is

The read-oriented ControlCenter exposes truthful status and health projections.

## Current truth

`CONTROL_CENTER_NOMAD_CUTOVER = VERIFIED`. Nomad owns ControlCenter; the stopped WinSW definition is
rollback only. That cutover did not itself authorize Recorder migration; Recorder lifecycle was
later migrated and is now resolved through the separate runtime-ownership authority.

## Interfaces / dependencies

`docs/runtime_ownership_and_self_healing.md`; `docs/deployment/NOMAD_CONTROL_CENTER_CUTOVER_FINAL_001.md`; `dependencies/platform/runtime-ownership.md`.

## Read next

For current status use `status/README.md`; for retirement constraints use `constraints/execution/runtime-upstream-boundary.md`.

## Update rule

Update only after an approved ControlCenter ownership, retirement, or health-contract change.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 authority baseline. |
| R2 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 | Removed the stale implication that Recorder remained outside Nomad ownership. |
