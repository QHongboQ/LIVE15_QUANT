# LIVE15 project progress

This is the durable, human- and AI-readable task ledger. It answers **where
each important task is**; it is not architecture authority, live telemetry,
detailed evidence, or a chronological diary. `CURRENT_STATE.md` answers where
the whole project is now.

## Reading and update rule

Bootstrap through `AGENTS.md` and `CURRENT_STATE.md` before this ledger. Task
status is one of `PLANNED`, `IN_PROGRESS`, `BLOCKED`, `PR_OPEN`, `MERGED`,
`DEPLOYED`, `VERIFIED`, `CLOSED`, or `CANCELLED`. Research result is separately
`PASS`, `FAIL`, or `NO_GO` when applicable.

`MERGED != DEPLOYED`; `DEPLOYED != VERIFIED`; and technical `PASS !=
TRAINING_GO`. Evidence paths contain volatile receipts, PIDs, and measurements;
this file stores only their durable implication. Close an important task by
updating this ledger only if its status, result, caution, or next action changed.

## Current reconciliation basis

- Merged-code authority: `origin/main` at `cce1ebc1ad7e29fb85fea9f86d9f1b9cb924fb17`
  (PR #40) when CTX-002 was reconciled.
- Legacy evidence: `origin/agent/ctx-002-recovery-source:LEGACY_RECOVERY_2026-08-27.md`
  (PR #41, unmerged temporary recovery source). It informs this ledger but is
  not a competing authority.
- Runtime basis: bounded read-only service/health receipts observed during
  CTX-002. They show a running 10/10 synchronized Recorder with an honest
  exact-WTI feed-local degradation; they do not prove current-main deployment.

## Completed and merged foundations

| Task ID | Title / area | Status / result | Git evidence | Deployment / verification | Caution and next action | Human gate |
| --- | --- | --- | --- | --- | --- | --- |
| UI-010 | Terminal V3 / UI | VERIFIED / historical Production proof | PR #25 | Historical deployment proof only | Do not imply later UI changes are deployed. | Deployment |
| AR-003 | Baseline-gap classification and quarantine / archive | MERGED / guarded | PR #21, #26 | No new deployment claim | Preserve raw quarantined evidence; never invent replay baselines. | Retention destruction |
| RD-001 | Research Data Authority / data | MERGED / COMPLETE | PR #29 `080e7804` | Not a deployment claim | H0/H1/H2 registry and universe remain coverage authority. | Research policy |
| RT-OWN-001 | One-owner runtime governance / ops | MERGED / COMPLETE | PR #30 `cd5d64d9` | Current services observed running; code deployment not inferred | Recorder, Control Center, Supervisor each remain WinSW-owned. | Runtime changes |
| CTX-001 | Skills and Git Project Brain / context | MERGED / COMPLETE | PR #32 `e788a235` | N/A | CTX-002 adds task recovery, not a second brain. | None |
| PYTH-001 | Hermes compatibility / provider | MERGED / COMPLETE | PR #33 `17ba78bb` | Not a current-main proof | Exact WTI has no authoritative replacement. | Provider policy |
| GAP-001 | Restart-idempotent DataGap / data | MERGED / GUARDED | PR #34 `c2ded1d4` | Historical runtime closeout exists; current deployment unproven | Logical identity excludes mutable provenance; semantic conflicts fail closed. | Data truth |
| MVN-003 | Isolated research preflight runner / research | MERGED / COMPLETE | PR #35 `c8afe5e2` | Not training authorization | Use only authority-chain snapshots and opaque holdout. | Holdout/training |
| AR-RD-001 | Verified COLD research adapter / archive | MERGED / COMPLETE | PR #36 `53380072` | Not a deployment claim | Checksums, replay, provenance, and quarantine exclusion remain required. | Archive authority |
| PYTH-002 | Feed-local circuit breaker / provider | MERGED / GUARDED | PR #37 `eeed2e5b` | Runtime receipt shows intended exact-WTI degradation; current-main deployment unproven | Isolate/reprobe exact source only; never silently substitute a feed. | Provider policy |
| RUN-004 | Archive → RDA → MVN-003 integration / research | MERGED / PASS | PR #38 `79e4f708` | Not training authorization | Technical integration PASS is not `TRAINING_GO`. | Training |
| UI-012 | Intentional auxiliary health projection / ops | MERGED / GUARDED | PR #39 `7f790fb8` | Runtime receipt confirms neutral ON_DEMAND/PAUSED projection; code deployment unproven | Historical child receipts never override desired state; stale RUNNING remains strict. | Runtime changes |
| KWS-001 | Kalshi sparse snapshot and rollover / SDK boundary | MERGED / GUARDED | PR #40 `cce1ebc1` | Current-main deployment and rollover proof unproven | One missing side may normalize; both invalid fail closed; replace SDK session, no concurrent receive. | Runtime deployment |
| ST-006 | Retention effectiveness audit / storage | VERIFIED / `TEMPORARY_BACKLOG` | Bounded storage audit evidence | Read-only classification | No compaction/VACUUM without its independent gate. | Storage mutation |

## Active, gated, and roadmap work

| Task ID | Title / area | Status / result | Evidence / blocker | Important notes and next action | Human gate |
| --- | --- | --- | --- | --- | --- |
| CTX-002 | Project Brain recovery and reconciliation / context | IN_PROGRESS | This ledger; legacy source PR #41 | Complete review, tests, Checker, CI, and open a PR. | Human merge |
| DEP-001 | Current-main Production deployment and bounded proof / deployment | BLOCKED | Current installed package receipt cannot prove `cce1ebc`; runtime health alone is insufficient | Requires explicit approval; then deploy/prove only the reviewed current main. | Deploy/restart |
| ST-005 | 60-minute archive/purge catch-up trend / storage | BLOCKED | Prior run stopped safely during unsynchronized WS; not rerun here | Requires approved read-only run after valid runtime proof; never auto-start. | Runtime/read-only authorization |
| TRN-001 | `LONG_RUN_TRAINING_FINAL_GO_NO_GO` / training gate | PLANNED / `NO_GO` | No formal gate execution evidence | Do not train until data/runtime/resource/anti-overfit gates pass; holdout remains opaque. | Training/holdout |
| DATA-004 | Independent UTC-day and regime coverage / data | PLANNED | Research Data Authority | More rows are not independent evidence. | Data policy |
| FAC-002 / FAC-003 | Decision-time-safe factor evidence / factors | PLANNED | Fixed-set evidence and chronological validation required | Ablate, use grouped validation/BH-FDR; do not widen search first. | Research policy |
| VAL-001 | Chronological anti-overfit gate / validation | PLANNED | Model vNext contract | Require event grouping, purge/embargo, cost stress, opaque holdout. | Holdout |
| MVN-001 / MVN-002 / MVN-004 | Path target, after-cost edge, dynamic exit / models | PLANNED | Model contracts | Contract and evidence precede training. | Model policy |
| MOD-UNC-001 / MOD-004 / MOD-005 | Uncertainty, promotion/rollback, retraining / models | PLANNED | Forward Challenger evidence | Immutable versions; no promotion from backtest alone. | Model promotion |
| DEC-001 / SIG-001..004 | Decision engine and signal groups / decision | PLANNED | Future ablation evidence | No signal group enters without an ablation. | Strategy/model policy |
| RISK-001 / EXE-001 / EXE-002 / SEC-001 | Hard Risk, execution, reconciliation, security / production | PLANNED | Required before any real-money pilot | Production writes remain 0; unknown reconciliation fails closed. | Hard Risk/execution/security |
| PROD-001 | Tiny 1-contract pilot / production | PLANNED | Requires all preceding forward/risk/security evidence | Not authorized by paper, merge, or training alone. | Explicit human approval |
| SESS-001 / OPS-002 / ST-004 / AI-001 / AI-002 / CLOUD-001 / DF-001 | Optimization and optional future lanes | PLANNED | Roadmap only | Do not displace the current data/runtime/training gates. | Varies |

## Reconciliation classification

| Legacy category | CTX-002 disposition |
| --- | --- |
| ALREADY_PRESENT | Charter invariants, Research Data Authority, immutable datasets, runtime ownership, protected-main governance, and frozen-holdout safeguards. |
| PRESENT_BUT_WEAKER | Bootstrap routing, task-level history, Upstream First/task-spec convention, explicit merge/deploy/verify separation, and bug status wording. |
| MISSING | This ledger, per-task persistence protocol, PR #29–#40 normalized history, and fresh-session questions. |
| SUPERSEDED | Generic unresolved Pyth-worker and RuntimeSupervisor ACL blockers: replaced by guarded feed-local exact-WTI behavior and resolved ACL evidence. |
| TRANSIENT_DO_NOT_STORE | PIDs, exact heartbeats, row counts, bytes, and one-off runtime measurements; retain them only in bounded receipts. |
| CONFLICT_REQUIRES_HUMAN | None found: lower-authority legacy claims did not override current runtime or merged-code evidence. |

## Durable operating rules

- Complex Codex tasks normally specify **Terra / High**, goal, authority,
  prohibitions, acceptance, validation, and return format. ChatGPT owns
  strategy/research/acceptance judgment; Codex owns implementation/tests/Checker/CI/PR.
- Upstream First: official documentation → pinned dependency source/tests →
  GitHub Issues/PR → mature/reference implementation → broader web → local
  reproduction → narrow fix → regression → Checker → CI.
- Record a true single smallest blocker only after safe investigation. Never
  create a second memory system; normal sessions do not need the legacy source.
