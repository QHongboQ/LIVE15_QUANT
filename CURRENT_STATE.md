# LIVE15 current state

## Source-of-truth rule

This file holds only stable workstream orientation. It is not a runtime receipt,
deployment log, or historical evidence store. Runtime facts come from the
current service/health evidence; research facts come from the Research Data
Authority and its evidence artifacts.

## Current phase

**Pre-training reliability/storage closeout plus current-main deployment proof gate.**
The release-pipeline prerequisites are merged, Control Center truth/performance
hardening is merged, and the tracked reliability workstream is execution-gated
before the formal long-run training GO/NO-GO gate.

ControlCenter current truth and its migration boundary are owned by
`docs/project-brain/capabilities/control-center.md`.

## Completed foundations

- protected-main governance;
- Research Data Authority and `/api/research-data`;
- runtime ownership design;
- Terminal V3;
- HOT/COLD archive foundation;
- full Skills/context system implementation;
- auditable SHA-pinned release/rollback pipeline including first-deploy legacy
  rollback bootstrap compatibility (PR #45 + PR #46);
- NIGHT-001 H2 materialization/readiness boundary and bounded real H2 snapshot
  acquisition path (PR #47 + PR #48);
- Control Center truth, polling, and archive-observability hardening (PR #49).

## Workstream orientation

| Area | State | Authoritative source |
| --- | --- | --- |
| Kalshi WS / DataGap reliability | **TRACKED / EXECUTION_GATED** | `WS-RESYNC-001 + GAP-002`; dependency-closure audit, critical-path prerequisite stabilization, and frozen baseline are required before direct execution. |
| Archive/purge throughput | **IN_PROGRESS** | `ST-005`, retention manifests and bounded trend evidence |
| Nomad secure migration | **VERIFIED** | `docs/project-brain/capabilities/control-center.md` |
| Production runtime closeout | **READY_FOR_PHASE_A_PREFLIGHT / HUMAN_GATE_PENDING_DEPLOYMENT_PROOF** | Current installed package, service health, and approved runtime evidence |
| Research coverage | Typed H0/H1/H2 authority | `docs/research_data_authority.md` and `/api/research-data` |
| Dataset/model promotion | Requires fresh forward challenger evidence | `docs/model_vnext_contract.md`, model lineage |
| Hard Risk / Production writes | Human-authorized only | `PROJECT_CHARTER.md`, `AGENTS.md` |

## Current runtime and research limits

- The last bounded read-only runtime receipt showed all three WinSW services
  running; Recorder was 10/10 synchronized with zero sequence gaps and no fatal
  task, with an honest exact-WTI/Pyth feed-local degradation. That receipt does
  not prove the newly merged protected-main code is deployed.
- `MERGED != DEPLOYED` and `DEPLOYED != VERIFIED`. PR #46 and PR #49 are merged
  code only; no current-main deployment claim is made here.
- `H2-TRAIN-003` is not an independent active development lane. Its previous
  BLOCKED result exposed a Kalshi WS/DataGap authority problem; the real H2
  revalidation is now an acceptance step inside `WS-RESYNC-001 + GAP-002`.
- `WS-RESYNC-001 + GAP-002` remains tracked, but direct execution is gated by
  the dependency-closure audit, critical-path prerequisite stabilization, and
  the GAP002 frozen baseline. `GAP002_DEPENDENCY_AUDIT_EXECUTED = NO`.
- H2 capability remains granular. Real snapshot readiness, snapshot-sequence
  readiness, delta/tick readiness, and each microstructure model family remain
  independently gated. DepthFeed HTTP 402 plan restrictions must not be hidden.
- `ST-005` is not resolved merely because UI-013 can display catch-up state. It
  still requires measured throughput/catch-up evidence and a valid 60-minute
  proof with Recorder safety intact.
- ControlCenter migration current truth: `docs/project-brain/capabilities/control-center.md`.
- `LONG_RUN_TRAINING_FINAL_GO_NO_GO` has not run: **NO TRAINING_GO** and
  **NO TRAINING_STARTED**. A broad local artifact search accidentally displayed
  frozen-holdout rows and was stopped immediately. The former `UNREVEALED`
  status is invalid; no exposed content informed WS/GAP/H2 implementation,
  test thresholds, parameters, or code changes. Do not reopen the holdout to
  measure scope. A separate contamination-remediation/replacement decision is
  required before `TRN-001`. **PRODUCTION WRITES 0.**

## Current execution route

The approved sequence, GAP002 dual-lane strategy, and later gates are owned by
`docs/project-brain/plan/current-roadmap.md`. Capability detail is routed by
`docs/project-brain/capabilities/README.md`; execution constraints by
`docs/project-brain/constraints/README.md`.

## Update policy

Update this file only when a durable workstream state or source-of-truth rule
changes. Put measurements, PIDs, timestamps, and transient incidents in their
bounded evidence artifacts instead. Do not use this file to freeze or override
the separate runtime-closeout result.
