# LIVE15 project progress

Compact ledger; detailed history is in `docs/project-brain/PROJECT_PROGRESS_DETAIL_20260829.md`; `CURRENT_STATE.md` records whole-project orientation.

## Reading and update rule

Task status is one of `PLANNED`, `IN_PROGRESS`, `BLOCKED`, `PR_OPEN`, `MERGED`, `DEPLOYED`, `VERIFIED`, `CLOSED`, or `CANCELLED`; research result is separately `PASS`, `FAIL`, or `NO_GO` when applicable.

`MERGED != DEPLOYED`; `DEPLOYED != VERIFIED`; technical `PASS != TRAINING_GO`. Put volatile receipts, PIDs and measurements in bounded evidence, not here.

## Current reconciliation basis

- Resolve `origin/main` at task start; this index tracks PR #74 `77cb7ce`.
  `MERGED != DEPLOYED`.
- Detail/new-chat recovery: `docs/project-brain/PROJECT_PROGRESS_DETAIL_20260829.md`
  and `docs/project-brain/NOMAD_OVERNIGHT_HANDOFF_20260829.md`.

## Recent completed foundations

| Task | Status / result | Evidence | Durable implication |
| --- | --- | --- | --- |
| DEP-PKG-001 | MERGED / COMPLETE | PR #45 `e6cb02fd` | Auditable SHA-pinned package/activation/rollback prerequisite exists. |
| DEP-PKG-002 | MERGED / COMPLETE | PR #46 `7fd9b4da` | First-deploy legacy rollback bootstrap compatibility complete; no deployment claim. |
| H2-TRAIN-001 / NIGHT-001 | MERGED / PARTIAL | PR #47 `7fe9f17a` | H2 code/materialization boundary exists; real H2 remains validation-gated. |
| H2-TRAIN-002 | MERGED / BLOCKED | PR #48 `6bb24775` | Real snapshot acquisition works; delta endpoint plan-restricted; prior H0 overlap blocked by gap authority. |
| UI-013 | MERGED / COMPLETE | PR #49 `30fcdd85` | Control Center truth/performance/observability hardened; ST-005 itself remains unresolved. |
| DEV-TOOLING-GH-001 | VERIFIED / AVAILABLE | Windows development host | GitHub CLI (`gh`) is installed and authenticated; Codex may use it for PR, Actions/CI, issue, review, and GitHub API workflows. Ordinary repo-local maintenance may be autonomously merged after the standing Upstream Reuse First + regression + Checker + green-CI gates; elevated-review zones retain their explicit human gates. |

## Active and gated work

| Task | Status / result | Next action / caution | Human gate |
| --- | --- | --- | --- |
| WS-RESYNC-001 + GAP-002 | IN_PROGRESS | Kalshi self-healing: dirty-book detection → official `get_snapshot` → bounded resubscribe/reconnect → verified snapshot → precise gap closure/clean segment. H2-TRAIN-003 revalidation is acceptance work, not a separate active lane. | Runtime/deployment for live rollout |
| NOMAD-POC-SECURE-001 | VERIFIED / isolated POC burn-in + auto-revert + two-hour soak PASS | Final receipt: 24 healthy observations; terminal observer entry and evidence rule are in the POC handoff. No cutover. | POC only; no Production/holdout |
| NOMAD-POC-VALIDATE-001 | PR_OPEN | Draft PR #71 remains code evidence only; do not merge or treat it as deployment proof. Its separate restart-validation lineage does not supersede the verified service-model POC evidence. | POC only |
| GITHUB-ACTIONS-QUOTA-20260829 | CI_DEFERRED_QUOTA | Do not intentionally trigger GitHub-hosted CI, and do not record deferred CI as PASS. Run the required final CI only after the user explicitly re-enables quota. | No merge |
| H2-TRAIN-003 | BLOCKED / historical | Preserve prior blocker evidence. Do not continue as an independent development branch unless WS-RESYNC leaves a new smallest blocker. | Training/holdout |
| ST-005 | CODE_READY_PENDING_60MIN_PROOF | Code and offline validation now expose fail-closed, comparable 60-second archive/ingress evidence; formal runtime proof has not run. | Requires approved read-only 60-minute proof after a valid runtime/deployment gate; no restart or storage mutation |
| DEP-001 | PLANNED | DEP-PKG-002 blocker removed. Next action: Phase A current-main read-only preflight. Deployment/restart requires separate explicit `DEP001_DEPLOY_APPROVED`. | Deploy/restart |
| DEP-ROOT-HYGIENE-PREVENT-001 | PLANNED | Prevent recurrence of Production-root test/tool residue: pytest, Checker, and Codex temporary artifacts/workspaces must not create ad-hoc top-level entries under `D:\LIVE15_QUANT`. Use a dedicated temporary root outside Production by default, or an explicitly approved excluded mutable path such as `runtime/tmp`. Add configuration/guard/regression after the current root-cleanup/deployment blocker is cleared. This is post-cleanup hardening and is not an extra gate for the current DEP-001 path unless residue recurs. | No deployment/trading gate for the code rule; any Production filesystem cleanup remains separately authorized |
| LOCAL-REGRESSION-001 | PR_PENDING / LOCAL_VALIDATION_PASS | Full current-main local regression at `4d088930`: 1,176 passed, 14 existing opt-in smoke tests skipped, 0 failed. Cache confined to isolated `runtime/tmp`; no runtime or Production operation. | CI remains `CI_DEFERRED_QUOTA`; no merge/deployment claim |
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
- **Upstream Reuse First is mandatory:** official docs/release notes → pinned dependency source/tests/examples → upstream GitHub Issues/PR/Discussions/merged fixes → mature actively maintained license-compatible GitHub implementation → broader authoritative web → only then local reproduction and a LIVE15-specific fix.
- **Reuse beats reimplementation:** when a suitable mature implementation exists, prefer `dependency → pinned dependency/fork → vendored upstream module → narrow attributed port → local reimplementation`. Do not read a mature project and then rewrite the same subsystem from scratch unless reuse is demonstrably unsuitable; record that justification.
- **Thin-adapter rule:** keep upstream-owned generic behavior upstream and put LIVE15-specific Kalshi/domain/safety semantics behind the thinnest practical adapter. Do not fork generic infrastructure into a growing LIVE15-only patch pile.
- **Anti-spaghetti rule:** repeated special cases, third/fourth execution modes, duplicated modern/legacy paths, or contradictory invariants trigger consolidation/refactoring/upstream reuse before more patching. A green regression is insufficient if the architecture becomes less coherent.
- **Standing autonomy for ordinary maintenance:** ordinary repo-local engineering bugs/maintenance may be researched, changed, optimized, tested, reviewed, and merged autonomously after Upstream Reuse First, regression coverage, Independent Checker, and green CI. Elevated-review zones in `AGENTS.md`, Production trading writes, holdout/training/promotion gates, Hard Risk, and irreversible policy changes still require their explicit human approvals.
- Use authenticated GitHub CLI for repository work; it does not relax safety boundaries.
- Production-root hygiene: pytest, Checker, and Codex temporary/test artifacts must not create ad-hoc top-level directories under `D:\LIVE15_QUANT`; use a dedicated temp root outside Production by default, or an explicitly approved excluded mutable path such as `runtime/tmp`. Unknown exceptions remain fail-closed and must not be silently added to capture exclusions.
- Record one true smallest blocker only after safe investigation. Never create a second project-memory system.
