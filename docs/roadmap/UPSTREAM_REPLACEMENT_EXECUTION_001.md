# LIVE15 upstream replacement execution rules

**Status:** governance/execution routing only. No Production, runtime, model,
training, holdout, risk, execution, or trading authorization.

This document complements `UPSTREAM_REPLACEMENT_MATRIX_001.md`. It exists to
keep upstream adoption simple and subtractive rather than turning integration
work into another custom platform layer.

## Mandatory outcome

A successful upstream replacement must reduce LIVE15-owned generic machinery.
The expected direction is:

`custom lifecycle/platform code down -> thin config/adapter/validation up`

Do not count generated/vendor/upstream code as LIVE15-owned implementation.
If a migration materially increases custom lifecycle/platform code, adds a
second or third special-case path, or leaves both old and new control planes
actively growing, stop for architecture review.

Prefer deletion or freezing of redundant local machinery over preserving it for
speculative flexibility. A feature is complete when the required behavior is
reliably provided by one clear owner.

## Legacy freeze during migration

Once a mature upstream owner is selected for a generic responsibility, the
legacy LIVE15 implementation is frozen except for a narrowly scoped Production
safety fix or rollback-preservation change. Do not add new features to both the
legacy and replacement paths.

Retire legacy components one workload/responsibility at a time only after the
replacement has its own bounded evidence and rollback plan. Never delete the
legacy path merely because a POC passed.

## Agent/Codex task shape

For generic infrastructure tasks, prompts and implementation plans should ask
for the upstream/native mechanism first and keep LIVE15 work to:

1. pinned configuration/jobspec/integration wiring;
2. the thinnest domain adapter needed at the boundary;
3. fail-closed read-only validation and evidence receipts; and
4. regression coverage for LIVE15-specific contracts.

Do not ask an agent to reproduce upstream lifecycle, ACL repair, restart,
rollback, discovery, dashboard plumbing, telemetry routing, queueing, or
workflow-engine behavior locally. When a host/platform prerequisite blocks the
adapter, record `environment/operator/installation` and stop.

## Execution order

1. **Nomad + Windows SCM** — finish the first isolated read-only ControlCenter
   shadow, then migrate additional low-risk workloads one at a time. Recorder
   is intentionally late in the sequence.
2. **React Admin + Material UI** — first web POC is display-only health and
   markets; keep FastAPI typed projections and domain truth in LIVE15.
3. **Vector**, then **Grafana** where an operations dashboard is actually
   useful. Use one telemetry collector by default; do not deploy overlapping
   collectors without a measured requirement.
4. **Measure before adding throughput infrastructure.** Choose NATS JetStream
   only for demonstrated buffering/replay/backpressure needs. Choose
   DuckDB/Polars/Arrow only for demonstrated analytical/read-path throughput
   needs. Use the smallest candidate that addresses the measured bottleneck.
5. Do not introduce Consul, Temporal, Kafka/Redpanda, or another control plane
   merely because it is mature. Require a concrete unmet requirement first.

## LIVE15 responsibilities that remain local

Upstream replacement must not displace authoritative LIVE15 contracts:

- Kalshi domain identity and the immutable gateway boundary;
- Recorder/RecorderStore, official settlement truth, provenance and quarantine;
- strict as-of/freshness/synchronization and WebSocket gap/recovery semantics;
- Research Data Authority, dataset/snapshot/leakage boundaries;
- Hard Risk, sizing, execution authorization and Production safety.

Upstream projects may host, transport, display, schedule, accelerate, or observe
these functions; they do not become the authority that decides their truth.

## Review gates

Each migration owns one bounded task, branch and PR. Review must ask:

- Did the change reduce or freeze redundant custom machinery?
- Is there exactly one clear owner for generic behavior?
- Is the LIVE15 adapter smaller and simpler than the subsystem it replaces?
- Did Checker findings stay validation findings rather than become new repair
  controllers?
- Are measured requirements, hashes, health/evidence and rollback boundaries
  explicit?

If the answer to the first three questions is no, the migration is not ready
regardless of whether its tests pass.

Tracking: `GOV-PLATFORM-REUSE-001` / issue #88 and
`UPSTREAM-MIGRATION-SEQUENCE-001` / issue #90.
