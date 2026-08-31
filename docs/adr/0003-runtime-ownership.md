# ADR 0003: Runtime ownership and bounded self-healing

- Status: Accepted (evolved)
- Sources: `docs/runtime_ownership_and_self_healing.md`,
  `deploy/windows/runtime-ownership.json`

## Decision

Each component has one process owner, one health truth, and one recovery authority. Current owner
values are resolved from `deploy/windows/runtime-ownership.json`; this ADR does not duplicate them.
Nomad or Windows/WinSW may own a component lifecycle as registered, and an in-process worker may
escalate to its parent only through bounded, explicit policy.

## Consequences

No parallel Python watchdog may claim process-tree ownership. Components remain independently
owned according to the machine-readable registry; stale telemetry does not override current owner
health.
