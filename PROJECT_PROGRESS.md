# LIVE15 project progress

Compact ledger; `CURRENT_STATE.md` records whole-project orientation. Git commits and PRs are canonical history. Dated detail and handoff files are legacy evidence/task-detail only.

## Reading and update rule

Task status is one of `PLANNED`, `IN_PROGRESS`, `BLOCKED`, `PR_OPEN`, `MERGED`, `DEPLOYED`, `VERIFIED`, `CLOSED`, or `CANCELLED`; research result is separately `PASS`, `FAIL`, or `NO_GO` when applicable.

`MERGED != DEPLOYED`; `DEPLOYED != VERIFIED`; technical `PASS != TRAINING_GO`. Put volatile receipts, PIDs and measurements in bounded evidence, not here.

## Current reconciliation basis

- Current base authority is `origin/main` at `c557d52` (merged PR #103). Resolve
  `origin/main` at task start. `MERGED != DEPLOYED` outside explicitly verified work.
- When an authority leaf selects dated evidence, use
  `docs/project-brain/PROJECT_PROGRESS_DETAIL_20260829.md`,
  `docs/project-brain/NOMAD_OVERNIGHT_HANDOFF_20260829.md`, or
  `docs/project-brain/NOMAD_MIGRATION_STATUS_20260830.md` as legacy evidence/task detail,
  not as canonical or detailed history.
- Current execution sequence: `docs/project-brain/plan/current-roadmap.md`.

## Recent completed foundations

| Task | Status / result | Evidence | Durable implication |
| --- | --- | --- | --- |
| DEP-PKG-001 | MERGED / COMPLETE | PR #45 `e6cb02fd` | Auditable SHA-pinned package/activation/rollback prerequisite exists. |
| DEP-PKG-002 | MERGED / COMPLETE | PR #46 `7fd9b4da` | First-deploy legacy rollback bootstrap compatibility complete; no deployment claim. |
| H2-TRAIN-001 / NIGHT-001 | MERGED / PARTIAL | PR #47 `7fe9f17a` | H2 code/materialization boundary exists; real H2 remains validation-gated. |
| H2-TRAIN-002 | MERGED / BLOCKED | PR #48 `6bb24775` | Real snapshot acquisition works; delta endpoint plan-restricted; prior H0 overlap blocked by gap authority. |
| UI-013 | MERGED / COMPLETE | PR #49 `30fcdd85` | Control Center truth/performance/observability hardened; ST-005 itself remains unresolved. |
| DEV-TOOLING-GH-001 | VERIFIED / AVAILABLE | Windows development host | GitHub CLI (`gh`) is installed and authenticated; Codex may use it for PR, Actions/CI, issue, review, and GitHub API workflows. Ordinary repo-local maintenance may be autonomously merged after the standing Upstream Reuse First + regression + Checker + green-CI gates; elevated-review zones retain their explicit human gates. |
| SHADOW-REC-DISCOVERY-CONTRACT-001 | MERGED / CONTRACT | PR #80 | Non-Production validation remains separately authorized. |

## Active and gated work

| Task | Status / result | Next action / caution | Human gate |
| --- | --- | --- | --- |
| WS-RESYNC-001 + GAP-002 | BLOCKED / EXECUTION_PREREQUISITE_PENDING | Historical evidence: 72 WS/Recorder/SDK-shadow tests passed on `4d088930`. Direct execution awaits the GAP002 dependency-closure audit, critical-path prerequisite stabilization, and GAP002 frozen baseline; `GAP002_DEPENDENCY_AUDIT_EXECUTED = NO`. | Prerequisite closure before any runtime/deployment live rollout |
| SHADOW-REC-001 | BLOCKED / STALE_RECEIPTS | PIDs absent; no health/restart. Detail: `docs/reliability/SHADOW_RECORDER_EVIDENCE_AUDIT_20260829.md`. | Non-Production only |
| NOMAD-POC-SECURE-001 | VERIFIED / isolated POC burn-in + auto-revert + two-hour soak PASS | Final receipt: 24 healthy observations; terminal observer entry and evidence rule are in the POC handoff. No cutover. | POC only; no Production/holdout |
| NOMAD-POC-VALIDATE-001 | PR_OPEN | Draft PR #71 remains code evidence only; do not merge or treat it as deployment proof. Its separate restart-validation lineage does not supersede the verified service-model POC evidence. | POC only |
| NOMAD-MIGRATION-STATUS-20260830 | VERIFIED / COMPLETE | `docs/project-brain/capabilities/control-center.md` | No retirement or Recorder change without separate approval |
| NOMAD-CONTROL-CENTER-CUTOVER-FINAL-001 | VERIFIED / COMPLETE | `docs/deployment/NOMAD_CONTROL_CENTER_CUTOVER_FINAL_001.md` | `capabilities/control-center.md` owns current truth |
| GITHUB-ACTIONS-PUBLIC-20260830 | VERIFIED / STANDARD_HOSTED_CI_AVAILABLE | Public repo: standard GitHub-hosted CI may run normally; no task-specific quota approval is required. Larger/billable runners remain separately cost-gated. | Normal green-CI merge policy |
| H2-TRAIN-003 | BLOCKED / historical | Preserve prior blocker evidence. Do not continue as an independent development branch unless WS-RESYNC leaves a new smallest blocker. | Training/holdout |
| ST-005 | BLOCKED / PROOF_NEEDS_DEPLOYMENT | 2026-08-29 preflight: legacy `UNPROVEN` pointer; then-current main instrumentation was unactivated. A SHA-verifiable deployment gate precedes a fresh 60-minute proof. Detail: `docs/evidence/st-005-current-main-preflight-20260829.md`. | Human-authorized deployment; no restart, storage mutation, or Production write |
| DEP-001 | BLOCKED / PREFLIGHT_NOT_READY | 2026-08-29 read-only snapshot: dirty protected checkout, 37 commits behind then-main, active legacy `UNPROVEN` pointer. No deployment/restart. Detail: `docs/deployment/DEP001_PHASE_A_PREFLIGHT_20260829.md`. | Deployment/restart requires separate explicit `DEP001_DEPLOY_APPROVED` |
| DEP-ROOT-HYGIENE-PREVENT-001 | MERGED / ENFORCEMENT_READY | PR #79 merged the pytest cache isolation, WinSW fixture temp storage and fail-closed startup guard; detailed validation remains in `docs/project-brain/PROJECT_PROGRESS_DETAIL_20260829.md`. | Production cleanup remains separately authorized |
| TRN-001 | BLOCKED / HOLDOUT_CONTAMINATION_REMEDIATION_REQUIRED | A broad local artifact search displayed frozen-holdout rows and was stopped immediately. The previous `UNREVEALED` state is invalid; exposed content was not used for WS/GAP/H2 implementation, test thresholds, parameters, or code changes. Do not reopen it to measure scope. A separate remediation/replacement decision is required before the formal gate or any training. | Training/holdout |

## Planning route

`docs/project-brain/plan/current-roadmap.md` owns execution sequence. The
training and H2 capability authorities are under `capabilities/README.md`.

## Durable operating rules

See AGENTS.md for the canonical upstream-reuse, adapter, autonomy, safety, and blocker rules.

### 2026-08-29 architecture replacement decision

Approved incremental replacement: preserve data/API/domain core; gate runtime, UI, telemetry, throughput, environment, lineage, and model-tool candidates. See the replacement matrix.
