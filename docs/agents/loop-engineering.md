# LIVE15 bounded engineering loop (LOOP-001)

This document defines the first inspectable engineering loop for LIVE15. It is a human-started
workflow, not an autonomous release system.

```text
approved task -> task contract -> risk gate -> isolated worktree -> Maker
              -> validation gates -> independent Checker
              -> PASS / bounded retry / BLOCKED / human review
```

## Scope and ownership

The loop may inspect, implement, test, and report a scoped task. It may not change the task's
acceptance criteria, grant itself approval, enable Production writes, merge code, deploy, promote
a model, or change protected semantics. The task contract is the source of truth for both Maker
and Checker; the Maker's explanation is not evidence of completion.

The existing local skills remain the playbook:

- diagnosis: `.agents/skills/diagnosing-bugs/SKILL.md`;
- behavioral changes: `.agents/skills/tdd/SKILL.md`;
- ambiguous architecture: `.agents/skills/grill-with-docs/SKILL.md`.

## Task contracts

Use `.agents/contracts/task-contract.schema.json`. The contract records task id/title/type,
objective, allowed and forbidden paths, acceptance criteria, validation commands, risk, change
budget, retry limit, approval boundaries, expected output, and rollback expectations. A small
example is in `.agents/contracts/example-loop-001.json`.

## Risk levels

- **L0**: read-only audit and non-mutating checks; no code changes.
- **L1**: docs, metadata, tests, formatting, and non-behavioral configuration. Maker + Checker
  may run in an isolated worktree.
- **L2**: ordinary code changes and deterministic plumbing outside protected semantics. Requires
  a regression test for behavior, bounded Maker/Checker retries, and human review before merge.
- **L3**: Recorder authority/reliability, settlement truth, dataset boundaries, model targets or
  promotion, Hard Risk, sizing, execution, reconciliation, or Production configuration. Agents
  may analyze and propose; implementation requires explicit human approval in the contract.
- **L4**: enabling live-money writes, raising limits, automatic deployment, or automatic live
  model promotion. The loop never grants L4 authority; a human acts outside the loop.

For L0-L2, touching a protected path is `HUMAN_REVIEW_REQUIRED` unless the contract explicitly
grants L3 scope. Rules are centralized in `.agents/loop/protected-boundaries.json`.

## Worktrees

One mutating task uses one branch/worktree. The preferred naming is `agent/<task-id>-<short-name>`
and `D:\LIVE15_QUANT_worktrees\<task-id>` when native Codex worktree management is unavailable.
Never mutate the user's dirty main checkout for an autonomous task. Reuse a worktree only when
its branch and task id match and it has no competing owner. Never automatically delete a worktree
with uncommitted changes; leave it and report if safety cannot be proved.

GitHub publication follows protected-main governance: push only the feature branch, open a pull
request, resolve conversations, and wait for human approval before an allowed Squash or Rebase
merge. LOOP-001 never direct-pushes or force-pushes `main`, bypasses a ruleset, or auto-merges.
Required checks are not currently mandatory; selecting one is deferred to GOV-002. An explicitly
approved host Git boundary may authenticate, fetch, compare, push a feature branch, and inspect or
open its pull request, but does not authorize unrelated unsandboxed commands.

## Roles

`.codex/agents/maker.toml` and `.codex/agents/checker.toml` are role specifications, not
unattended agents. The Maker reads `AGENTS.md`, the contract and relevant skills, follows the
task's least-cost route (execute an approved replacement and observe, or reproduce a genuine
authoritative defect), makes the smallest scoped change, tests it, and reports evidence. The Checker reads the
original contract and Maker diff independently, reruns critical gates where feasible, and returns
only `PASS`, `FAIL_FIXABLE`, `BLOCKED`, or `HUMAN_REVIEW_REQUIRED`. The Checker does not edit the
Maker worktree in LOOP-001.

## Validation and budgets

`.agents/loop/validation-contract.json` defines base gates: diff check, Python lint/format when
applicable, targeted tests, regression coverage for behavior, secret/data/runtime-artifact scan,
scope review, and protected-boundary review. Task contracts add gates only when relevant; docs-only
tasks do not require runtime validation.

Change budgets are soft defaults unless a contract marks them hard: L1 is normally at most five
files/200 changed lines, L2 ten files/500 lines. A hard overrun stops with `CHANGE_BUDGET_EXCEEDED`.
The default retry limit is three iterations; no infinite retry is supported. Stop on repeated
failure without meaningful progress, scope expansion, missing environment/credentials, unsafe
merge, external blocker, protected change without approval, or Checker `BLOCKED`.

## Durable state and feedback

`.agents/state/schema.json` describes run state. Actual runs belong under `.agents/state/runs/` and
are ignored by Git; templates and schema are tracked. State records task id, iteration, Maker,
validation and Checker results, failure reason, status, timestamps, commit/diff identity, next
action, and blocker. The structured feedback shape is in the schema's `checker_feedback` object.

## Human approval boundaries

Human approval is required before L3 protected implementation, merging, changing Hard Risk,
dataset/model truth, switching the authoritative Recorder, enabling Production writes, raising
Production risk, deploying, or destructive data migration. LOOP-001 can produce PR-ready work;
it is not release authority.

## Safe prototype and dry run

`tools/live15_loop.py` is a local, standard-library-only helper. It validates a task contract,
classifies a list of changed paths against the protected-boundary rules, and persists an explicit
state record for a dry run. It does not invoke shells, agents, GitHub, schedulers, or deployment.

Example:

```powershell
\.venv\Scripts\python.exe tools\live15_loop.py dry-run `
  --contract .agents\contracts\example-loop-001.json `
  --state .agents\state\runs\example-loop-001.json
```

The dry run is a tooling proof (`CREATED -> VALIDATING -> CHECKER_RUNNING -> PASS`); a real Maker
and Checker invocation remains an explicit human command. LOOP-002 defers schedules/cron,
GitHub/CI triggers, issue triage, MCP monitoring, automatic repair/PR/merge, and unattended loops.
