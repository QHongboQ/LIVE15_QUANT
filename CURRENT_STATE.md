# LIVE15 current state

## Source-of-truth rule

This file holds stable workstream orientation, not runtime receipts or history.
Runtime facts come from current service/health evidence; research facts come
from the Research Data Authority and its evidence artifacts.

## Current phase

**Pre-training reliability/storage closeout plus first Nomad workload cutover-preparation gate.**
Release-pipeline and Control Center hardening are merged. WS/GAP reliability
and ST-005 remain active before the formal long-run training GO/NO-GO gate.

The isolated Nomad POC is verified and the first read-only ControlCenter shadow
is merged; both remain non-Production. Next is a separately scoped
ControlCenter cutover-preparation task.

## Completed foundations

- protected-main governance;
- Research Data Authority and `/api/research-data`;
- runtime ownership design;
- Terminal V3;
- HOT/COLD archive foundation;
- full Skills/context system implementation;
- SHA-pinned release/rollback pipeline including first-deploy legacy rollback
  compatibility (PR #45 + PR #46);
- NIGHT-001 H2 materialization/readiness and bounded real H2 snapshot path
  (PR #47 + PR #48);
- Control Center truth/polling/archive-observability hardening (PR #49).

## Workstream orientation

| Area | State | Authoritative source |
| --- | --- | --- |
| Kalshi WS / DataGap reliability | **IN_PROGRESS** | `WS-RESYNC-001 + GAP-002`, current Kalshi protocol, Recorder evidence |
| Archive/purge throughput | **IN_PROGRESS** | `ST-005`, retention manifests and bounded trend evidence |
| Nomad secure migration | **VERIFIED POC; ControlCenter shadow MERGED; cutover prep next** | `NOMAD-POC-SECURE-001`, PR #91, `NOMAD-CONTROL-CENTER-CUTOVER-PREP-001` |
| Production runtime closeout | **READY_FOR_PHASE_A_PREFLIGHT / HUMAN_GATE_PENDING_DEPLOYMENT_PROOF** | Current installed package, service health, approved runtime evidence |
| Research coverage | Typed H0/H1/H2 authority | `docs/research_data_authority.md` and `/api/research-data` |
| Dataset/model promotion | Requires fresh forward challenger evidence | `docs/model_vnext_contract.md`, model lineage |
| Hard Risk / Production writes | Human-authorized only | `PROJECT_CHARTER.md`, `AGENTS.md` |

## Current runtime and research limits

- Last read-only runtime receipt had all three WinSW services running and
  Recorder 10/10 synchronized with zero sequence gaps/fatal task; it does not
  prove current protected-main code is deployed.
- `MERGED != DEPLOYED != VERIFIED`; no current-main deployment claim is made.
- `H2-TRAIN-003` is historical. Real H2 revalidation is an acceptance step in
  `WS-RESYNC-001 + GAP-002`; plan-restricted capabilities must stay excluded.
- `ST-005` still needs measured throughput/catch-up evidence and a valid
  60-minute proof with Recorder safety intact.
- Nomad lifecycle proof is verified and PR #91 merged the first read-only
  ControlCenter shadow. This permits cutover preparation only, not deployment,
  WinSW retirement, or Production cutover.
- `LONG_RUN_TRAINING_FINAL_GO_NO_GO` has not run: **NO TRAINING_GO** and
  **NO TRAINING_STARTED**. Frozen-holdout rows were accidentally displayed and
  not used for implementation or tuning; do not reopen them. A separate
  remediation/replacement decision is required before `TRN-001`.
  **PRODUCTION WRITES 0.**

## Immediate sequence

1. finish `WS-RESYNC-001 + GAP-002`, including dirty-interval closure,
   self-healing snapshot recovery, clean-segment authority and bounded H2
   revalidation;
2. finish `ST-005` throughput recovery and valid 60-minute catch-up proof; it
   may proceed in parallel with step 1;
3. prepare `NOMAD-CONTROL-CENTER-CUTOVER-PREP-001`: move real
   `LIVE15ControlCenter` lifecycle design from WinSW to Nomad/Windows SCM,
   preserve bounded rollback, and stop before any service change;
4. reconcile/merge reviewed tasks, then rerun `DEP-001` Phase A read-only
   preflight; deployment/restart still requires separate explicit approval;
5. resolve holdout contamination without reopening the holdout, then run
   `TRN-001`;
6. start overnight training only after explicit `TRAINING_GO`.

Validated H2 is additive authority: use H0 + H1 + validated H2; exclude any H2
capability that remains unvalidated or plan-restricted.

## Update policy

Update this file only for durable workstream state/source-of-truth changes.
Keep measurements, PIDs, timestamps and transient incidents in bounded evidence.
