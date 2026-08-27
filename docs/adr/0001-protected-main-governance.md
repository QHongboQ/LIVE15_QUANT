# ADR 0001: Protected-main governance

- Status: Accepted (existing)
- Sources: `AGENTS.md`, `docs/agents/change-protocol.md`

## Decision

Changes use isolated worktrees and `agent/<task-id>` branches. They receive
Maker validation, independent Checker review, feature-branch publication, a PR,
and human approval before protected-main merge.

## Consequences

Agents do not push to main, force-push, bypass protection, auto-merge, or treat
host Git access as a general shell escape hatch.
