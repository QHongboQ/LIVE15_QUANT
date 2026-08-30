# LIVE15 project progress

Compact ledger; detail is in `docs/project-brain/PROJECT_PROGRESS_DETAIL_20260829.md`.
`CURRENT_STATE.md` records whole-project orientation.

## Reading and update rule

Task status: `PLANNED`, `IN_PROGRESS`, `BLOCKED`, `PR_OPEN`, `MERGED`, `DEPLOYED`,
`VERIFIED`, `CLOSED`, or `CANCELLED`; research result is separately `PASS`,
`FAIL`, or `NO_GO` when applicable.

`MERGED != DEPLOYED`; `DEPLOYED != VERIFIED`; technical `PASS != TRAINING_GO`.
Keep volatile receipts/PIDs/measurements in bounded evidence.

## Current reconciliation basis

- Resolve `origin/main` at task start; this index is reconciled through PR #94
  `63285f74`. `MERGED != DEPLOYED`.
- Recovery detail: `docs/project-brain/PROJECT_PROGRESS_DETAIL_20260829.md` and
  `docs/project-brain/NOMAD_OVERNIGHT_HANDOFF_20260829.md`.

## Recent completed foundations

| Task | Status / result | Evidence | Durable implication |
| --- | --- | --- | --- |
| DEP-PKG-001 | MERGED / COMPLETE | PR #45 `e6cb02fd` | SHA-pinned package/activation/rollback prerequisite exists. |
| DEP-PKG-002 | MERGED / COMPLETE | PR #46 `7fd9b4da` | First-deploy legacy rollback compatibility complete; no deployment claim. |
| H2-TRAIN-001 / NIGHT-001 | MERGED / PARTIAL | PR #47 `7fe9f17a` | H2 boundary exists; real H2 stays validation-gated. |
| H2-TRAIN-002 | MERGED / BLOCKED | PR #48 `6bb24775` | Snapshot acquisition works; delta plan-restricted; prior overlap blocked by gap authority. |
| UI-013 | MERGED / COMPLETE | PR #49 `30fcdd85` | Control Center truth/performance/observability hardened; ST-005 remains unresolved. |
| DEV-TOOLING-GH-001 | VERIFIED / AVAILABLE | Windows dev host | `gh` is authenticated for PR/CI/issue/review workflows; elevated-review zones keep their human gates. |
| DEP-ROOT-HYGIENE-PREVENT-001 | MERGED / ENFORCEMENT_READY | PR #79 | Pytest-cache/temp-fixture hygiene and fail-closed path enforcement merged. |
| SHADOW-REC-DISCOVERY-CONTRACT-001 | MERGED / CONTRACT | PR #80 | Non-Production validation remains separately authorized. |
| NOMAD-LIFECYCLE-UPSTREAM-AUDIT-001 | MERGED / AUDIT_PASS | PR #85 | Windows SCM/Nomad lifecycle is authoritative; manual agent restart is superseded. |
| NOMAD-AUTOMATION-FOUNDATION-001 | MERGED / FOUNDATION_READY | PR #86 | Nomad responsibility boundary, receipt contract and replacement matrix merged. |
| NOMAD-FIRST-WORKLOAD-SHADOW-001 | MERGED / SHADOW_ACCEPTANCE_PASS | PR #91 | Sealed read-only ControlCenter shadow passed allocation/health/hash, Maker/Checker and CI evidence. |

## Active and gated work

| Task | Status / result | Next action / caution | Human gate |
| --- | --- | --- | --- |
| WS-RESYNC-001 + GAP-002 | IN_PROGRESS / HISTORICAL_LOCAL_VALIDATION_PASS | 72 targeted tests passed; runtime recovery, clean-segment proof and H2 revalidation remain deployment-gated. | Runtime/deployment for live rollout |
| SHADOW-REC-001 | BLOCKED / STALE_RECEIPTS | Reported PIDs absent; revalidate only in a separately scoped non-Production task. | Non-Production only |
| NOMAD-POC-SECURE-001 | VERIFIED / POC PASS | Burn-in, auto-revert and two-hour soak passed; preserve final receipt. | POC only; no Production/holdout |
| NOMAD-POC-VALIDATE-001 | PR_OPEN | Draft PR #71 is code evidence only and does not supersede verified service-model POC evidence. | POC only |
| NOMAD-CONTROL-CENTER-CUTOVER-PREP-001 | PLANNED | Prepare real `LIVE15ControlCenter` ownership migration from WinSW to Nomad/Windows SCM, bounded rollback, and legacy-retirement candidates; stop before service change. | Deployment/restart/cutover requires explicit human approval |
| GITHUB-ACTIONS-PUBLIC-20260830 | VERIFIED / STANDARD_HOSTED_CI_AVAILABLE | Public repo standard GitHub-hosted CI may run normally; larger/billable runners remain cost-gated. | Normal green-CI merge policy |
| H2-TRAIN-003 | BLOCKED / historical | Preserve blocker evidence; do not continue independently unless WS-RESYNC leaves a new smallest blocker. | Training/holdout |
| ST-005 | BLOCKED / PROOF_NEEDS_DEPLOYMENT | Legacy `UNPROVEN` pointer means current-main instrumentation is unactivated; deploy proof precedes fresh 60-minute proof. | Human-authorized deployment; no restart/storage mutation/Production write |
| DEP-001 | BLOCKED / PREFLIGHT_NOT_READY | Prior read-only snapshot found stale dirty checkout and legacy `UNPROVEN` pointer. | Deployment/restart requires `DEP001_DEPLOY_APPROVED` |
| TRN-001 | BLOCKED / HOLDOUT_CONTAMINATION_REMEDIATION_REQUIRED | Frozen-holdout rows were accidentally displayed but not used; do not reopen. Remediation/replacement is required before the formal gate. | Training/holdout |

## Route to formal overnight training

1. finish/reconcile `WS-RESYNC-001 + GAP-002`;
2. finish/reconcile `ST-005`;
3. rerun `DEP-001` Phase A read-only preflight; deploy/prove reviewed main only
   after separate human approval;
4. resolve holdout contamination without reopening it;
5. run `TRN-001 LONG_RUN_TRAINING_FINAL_GO_NO_GO`;
6. start formal overnight training only on explicit `TRAINING_GO`.

H2 is optional-by-validation: use H0 + H1 + validated H2; exclude unvalidated or
plan-restricted H2 capability rather than fabricating it.

## Durable operating rules

See `AGENTS.md` for canonical upstream-reuse, adapter, autonomy, safety and blocker rules.

### 2026-08-29 architecture replacement decision

Preserve data/API/domain core; gate runtime, UI, telemetry, throughput,
environment, lineage and model-tool candidates. See the replacement matrix.
