# LIVE15 current state

## Source-of-truth rule

This file holds only stable workstream orientation. It is not a runtime receipt,
deployment log, or historical evidence store. Runtime facts come from the
current service/health evidence; research facts come from the Research Data
Authority and its evidence artifacts.

## Current phase

**Production runtime closeout / agent-context finalization.** The runtime
closeout is not complete. This branch records its known state but does not
operate services or claim a root cause still under diagnosis.

## Completed foundations

- protected-main governance;
- Research Data Authority and `/api/research-data`;
- runtime ownership design;
- Terminal V3;
- HOT/COLD archive foundation;
- full Skills/context system implementation.

## Workstream orientation

| Area | State | Authoritative source |
| --- | --- | --- |
| Production runtime closeout | **BLOCKED_PENDING_EXTERNAL_CLOSEOUT** | Separate approved runtime-closeout task and current service health |
| Recorder/archive data truth | Active, fail-closed | `docs/continuous_recorder.md`, retention manifest, current health |
| Research coverage | Typed H0/H1/H2 authority | `docs/research_data_authority.md` and `/api/research-data` |
| Dataset/model promotion | Requires fresh forward challenger evidence | `docs/model_vnext_contract.md`, model lineage |
| Hard Risk / Production writes | Human-authorized only | `PROJECT_CHARTER.md`, `AGENTS.md` |

## Active runtime blockers

1. **RuntimeSupervisor Codex service-control delegation** — the intended target
   Codex ACE was not persisted. Status: `UNRESOLVED_ACTIVE`.
2. **Pyth worker unhealthy / `PythNetworkError`** — this prevents a clean
   runtime-health proof. Its root cause remains under active diagnosis; do not
   infer one from this status. Status: `UNRESOLVED_ACTIVE`.

## Next after blocker resolution

1. sync `CURRENT_STATE.md` from the approved runtime-closeout evidence;
2. merge the Skills/context PR through protected-main governance;
3. complete archive/trainability closeout;
4. run the long-run training preflight.

## Update policy

Update this file only when a durable workstream state or source-of-truth rule
changes. Put measurements, PIDs, timestamps, and transient incidents in their
bounded evidence artifacts instead. Do not use this file to freeze or override
the separate runtime-closeout result.
