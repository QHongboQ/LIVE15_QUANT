# LIVE15 project progress

This is the compact durable task ledger. It answers where important work stands; detailed history lives in `docs/project-brain/PROJECT_PROGRESS_DETAIL_20260828.md`. `CURRENT_STATE.md` answers where the whole project is now.

## Reading and update rule

Task status is one of `PLANNED`, `IN_PROGRESS`, `BLOCKED`, `PR_OPEN`, `MERGED`, `DEPLOYED`, `VERIFIED`, `CLOSED`, or `CANCELLED`; research result is separately `PASS`, `FAIL`, or `NO_GO` when applicable.

`MERGED != DEPLOYED`; `DEPLOYED != VERIFIED`; technical `PASS != TRAINING_GO`. Put volatile receipts, PIDs and measurements in bounded evidence, not here.

## Current reconciliation basis

- Current merged-code authority is always live `origin/main` HEAD and must be resolved at task start.
- Latest reconciled protected-main point for this update is PR #49 merge `30fcdd8516c343110c0bb7b0f23729a70b5eea8f`.
- Runtime evidence remains separate from merged-code authority; a merge never proves deployment.
- Full normalized task history, branches, PRs, merge SHAs, completed foundations, legacy reconciliation and operating-rule detail are preserved in `docs/project-brain/PROJECT_PROGRESS_DETAIL_20260828.md`.

## Recent completed foundations

| Task | Status / result | Evidence | Durable implication |
| --- | --- | --- | --- |
| DEP-PKG-001 | MERGED / COMPLETE | PR #45 `e6cb02fd` | Auditable SHA-pinned package/activation/rollback prerequisite exists. |
| DEP-PKG-002 | MERGED / COMPLETE | PR #46 `7fd9b4da` | First-deploy legacy rollback bootstrap compatibility complete; no deployment claim. |
| H2-TRAIN-001 / NIGHT-001 | MERGED / PARTIAL | PR #47 `7fe9f17a` | H2 code/materialization boundary exists; real H2 remains validation-gated. |
| H2-TRAIN-002 | MERGED / BLOCKED | PR #48 `6bb24775` | Real snapshot acquisition works; delta endpoint plan-restricted; prior H0 overlap blocked by gap authority. |
| UI-013 | MERGED / COMPLETE | PR #49 `30fcdd85` | Control Center truth/performance/observability hardened; ST-005 itself remains unresolved. |

## Active and gated work

| Task | Status / result | Next action / caution | Human gate |
| --- | --- | --- | --- |
| WS-RESYNC-001 + GAP-002 | IN_PROGRESS | Kalshi self-healing: dirty-book detection → official `get_snapshot` → bounded resubscribe/reconnect → verified snapshot → precise gap closure/clean segment. H2-TRAIN-003 revalidation is acceptance work, not a separate active lane. | Runtime/deployment for live rollout |
| H2-TRAIN-003 | BLOCKED / historical | Preserve prior blocker evidence. Do not continue as an independent development branch unless WS-RESYNC leaves a new smallest blocker. | Training/holdout |
| ST-005 | BLOCKED / `ST_005_PROOF_BLOCKED_PENDING_DEPLOYMENT` | Read-only preflight on 2026-08-28 found no runtime SHA binding to current main and no merged comparable ingress/effective-processing metrics; no 60-minute window was started. See `docs/evidence/st-005-current-main-preflight-20260828.md`. | Requires separately human-authorized SHA-verifiable current-main deployment, then a fresh read-only preflight and continuous proof; no restart or storage mutation in this task |
| DEP-001 | PLANNED | DEP-PKG-002 blocker removed. Next action: Phase A current-main read-only preflight. Deployment/restart requires separate explicit `DEP001_DEPLOY_APPROVED`. | Deploy/restart |
| TRN-001 | BLOCKED / HOLDOUT_CONTAMINATION_REMEDIATION_REQUIRED | A broad local artifact search displayed frozen-holdout rows and was stopped immediately. The previous `UNREVEALED` state is invalid; exposed content was not used for WS/GAP/H2 implementation, test thresholds, parameters, or code changes. Do not reopen it to measure scope. A separate remediation/replacement decision is required before the formal gate or any training. | Training/holdout |

## Route to formal overnight training

1. finish/reconcile `WS-RESYNC-001 + GAP-002`;
2. finish/reconcile `ST-005`;
3. rerun `DEP-001` Phase A read-only preflight, then only with separate human approval deploy and prove reviewed protected main;
4. complete a separate holdout-contamination remediation/replacement decision
   without reopening the frozen holdout;
5. run `TRN-001 LONG_RUN_TRAINING_FINAL_GO_NO_GO` only after that decision;
6. start formal overnight training only on explicit `TRAINING_GO`.

H2 is optional-by-validation: formal research may use H0 + H1 + **validated** H2. Unvalidated or plan-restricted H2 capability is excluded rather than fabricated and must not block unrelated valid model families without evidence.

## Durable operating rules

- Complex Codex tasks normally specify Terra / High, goal, authority, prohibitions, acceptance, validation and return format.
- Upstream First: official docs → pinned dependency source/tests → GitHub Issues/PR → mature/reference implementation → broader web → local reproduction → narrow fix → regression → Checker → CI.
- Record one true smallest blocker only after safe investigation. Never create a second project-memory system.
