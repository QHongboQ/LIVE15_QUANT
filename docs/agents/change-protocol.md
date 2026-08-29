# LIVE15 change protocol

## Protected-main publication

The repository uses protected `main`: changes are published through an isolated worktree and an
`agent/<task-id>` feature branch. The required sequence is Maker implementation, tests and other
validation, independent Checker review, feature-branch push, pull request, resolution of review
conversations, human approval, then Squash or Rebase merge. Direct pushes to `main`, force pushes,
ruleset bypasses, and automatic merges are prohibited. Status checks are not yet required by the
ruleset; adding a canonical required check is deferred to GOV-002. Any host/unsandboxed Git use is
limited to an explicitly approved Git boundary and must not become arbitrary host shell access.

Every coding task should make its boundary explicit before editing:

1. Read `AGENTS.md` and relevant architecture/recovery documents.
2. Inspect actual files, configuration, processes, and tests.
3. State ownership, in-scope files, acceptance criteria, and a time/change budget.
4. Preserve unrelated dirty work; never use broad staging or destructive cleanup.
5. Change the smallest safe surface. Avoid opportunistic refactors.
6. Validate with targeted tests and static checks appropriate to the risk.
7. Review the diff and secret/data boundary.
8. Report facts, changed files, checks, blockers, and rollback/next steps.

For behavioral changes, follow:

```text
reproduce → classify owner → upstream/native check → failing regression test → minimal implementation → targeted tests → relevant broader checks
```

Documentation-only, pure metadata, and audit tasks may use validation without artificial tests.
High-risk changes require explicit review before altering authoritative Recorder writes, gap or
settlement semantics, dataset boundaries, model targets, Hard Risk, or Production execution.

## Platform-owned failure gate

After reproduction, explicitly classify the failure owner before editing application code. If the
cause is configuration, environment/operator state, Windows/Linux/macOS platform behavior,
permissions/ACL/ownership, native service management, scheduler/process lifecycle,
deployment/revert, discovery, telemetry/logging, packaging, or behavior already owned by a selected
mature upstream project, the default action is **native/upstream remediation plus LIVE15
validation**, not a new LIVE15 subsystem.

For such failures:

- a LIVE15 adapter/validator may inspect and fail closed;
- a one-time native installation/admin action may be documented and executed only inside its
  separately authorized host boundary;
- repo code must not become an ACL/owner repair manager, UAC/elevation wrapper, service manager,
  supervisor, restart manager, rollback controller, registry/discovery implementation, or generic
  recovery framework;
- a new Checker finding about another platform prerequisite is a reason to record an
  `environment/operator/installation` blocker and stop, not a reason to keep extending the
  adapter;
- if the proposed solution adds a second/third special-case path or materially increases custom
  platform/lifecycle code, stop for architecture review before editing.

The simplicity target is deliberate: one clear owner per responsibility, the fewest code paths,
small configuration/thin adapters, mature defaults, and deletion of redundant local machinery.
Do not add speculative flexibility, duplicated safety controllers, or abstractions with no current
requirement. Tracking: `GOV-PLATFORM-REUSE-001` / GitHub issue #88.

## Project-brain and model-selection gate

Before issuing a copy-ready LIVE15 Codex task, consult the current Git Project Brain for the
relevant task. Durable project rules should be recovered from Git rather than asking the user to
re-paste them. Then state the selected model and reasoning level explicitly in the prompt.
Selection is dynamic: choose the least expensive adequate model/reasoning level for the task's
complexity, risk, context size, and expected token cost; escalate only when justified.

If scope expands materially, the original acceptance signal disappears, or the failure is owned by
a platform/upstream boundary outside LIVE15, stop and reassess. Do not keep patching through an
unclassified or externally owned failure.
