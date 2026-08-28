# LIVE15 current state

## Source-of-truth rule

This file holds only stable workstream orientation. It is not a runtime receipt,
deployment log, or historical evidence store. Runtime facts come from the
current service/health evidence; research facts come from the Research Data
Authority and its evidence artifacts.

## Current phase

**Project Brain reconciliation / Production closeout gate.** CTX-002 is the
active documentation task. The runtime is running, but current-main deployment
and post-deployment proof remain a separately human-approved gate.

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
| Production runtime closeout | **HUMAN_GATE_PENDING_DEPLOYMENT_PROOF** | Current installed package, service health, and approved runtime evidence |
| Recorder/archive data truth | Active, fail-closed | `docs/continuous_recorder.md`, retention manifest, current health |
| Research coverage | Typed H0/H1/H2 authority | `docs/research_data_authority.md` and `/api/research-data` |
| Dataset/model promotion | Requires fresh forward challenger evidence | `docs/model_vnext_contract.md`, model lineage |
| Hard Risk / Production writes | Human-authorized only | `PROJECT_CHARTER.md`, `AGENTS.md` |

## Current runtime limits and gates

- The current read-only receipt shows all three WinSW services running; Recorder
  is 10/10 synchronized with zero sequence gaps and no fatal task. It is
  honestly `degraded` because exact WTI and its Pyth stream are unavailable.
  The feed-local circuit breaker is the intended guarded behavior; no substitute
  feed may be chosen.
- Service ACL delegation and the generic Pyth-worker diagnosis are resolved
  code/operational history, not active blockers. See `BUG_REGISTRY.md`.
- Current-main deployment is **not proven**: the non-editable installed package
  has bounded provenance at an earlier protected-main hash, while the deployed
  commit for `origin/main` cannot be determined from this receipt. Merged is not
  deployed or verified.
- `LONG_RUN_TRAINING_FINAL_GO_NO_GO` has not run: **NO TRAINING_GO**.

## Immediate sequence

1. finish CTX-002 through protected-main review;
2. only with human approval, perform DEP-001 current-main deployment and bounded
   runtime proof;
3. after that proof, authorize the read-only ST-005 trend if still needed;
4. evaluate TRN-001 gates without reading the frozen holdout.

## Update policy

Update this file only when a durable workstream state or source-of-truth rule
changes. Put measurements, PIDs, timestamps, and transient incidents in their
bounded evidence artifacts instead. Do not use this file to freeze or override
the separate runtime-closeout result.
