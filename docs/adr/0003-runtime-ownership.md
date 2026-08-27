# ADR 0003: Runtime ownership and bounded self-healing

- Status: Accepted (existing)
- Sources: `docs/runtime_ownership_and_self_healing.md`,
  `deploy/windows/runtime-ownership.json`

## Decision

Each component has one process owner, one health truth, and one recovery
authority. Windows/WinSW owns service lifecycle; an in-process worker may
escalate to its parent only through bounded, explicit policy.

## Consequences

No parallel Python watchdog may claim process-tree ownership. Recorder, Control
Center, and RuntimeSupervisor remain independently owned; stale telemetry does
not override current service health.
