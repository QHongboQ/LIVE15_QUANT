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

Before composing a user-facing Codex task, read the current Git Project Brain
for the relevant task instead of asking the user to re-paste durable project
rules. Git is durable external memory; chat text is not the authority.

Every copy-ready Codex task must explicitly state its selected model and
reasoning level. Choose them dynamically from task complexity, risk, context
size and expected token cost. Use the least expensive adequate setting; do not
hard-code Terra/High as a default. Escalate only when the task justifies it.

For generic infrastructure tasks, prompts and implementation plans should ask
for the upstream/native mechanism first and keep LIVE15 work to:

1. pinned configuration/jobspec/integration wiring;
2. the thinnest domain adapter needed at the boundary;
3. fail-closed read-only validation and evidence receipts; and
4. regression coverage for LIVE15-specific contracts.

Do not ask an agent to reproduce upstream lifecycle, ACL repair, restart,
rollback, discovery, dashboard plumbing, telemetry routing, queueing, or
workflow-engine behavior locally.

## Mandatory upstream-resolution gate

For **any** non-trivial problem, especially platform, deployment, packaging,
permissions, service/lifecycle, discovery, telemetry, dependency, protocol, or
integration failures, local invention is the last resort. The required search
order is:

1. official product/project documentation, release notes, migration guides,
   tutorials and maintained examples;
2. the selected/pinned upstream project's official GitHub repository: source,
   tests, examples, changelog, Issues, Pull Requests and Discussions;
3. other mature, actively maintained, license-compatible GitHub projects that
   already solve the same problem;
4. broader community/web material only to fill gaps or compare real-world
   deployment experience;
5. a LIVE15-specific implementation only when the requirement is genuinely
   project-specific or the upstream/community search found no suitable reusable
   path.

The agent must prefer **following the official tutorial/mechanism directly**
over translating it into a new LIVE15 subsystem. Studying upstream and then
rewriting equivalent behavior locally is not compliance.

The concrete vendor procedure is **not** meant to be copied into Project Brain
or pre-expanded into a permanent Codex recipe. At task execution time, the
agent must retrieve the current official instructions and current official
GitHub evidence itself, then derive the supported procedure from those sources.
Project Brain should hold the routing rule, LIVE15 boundaries and acceptance
criteria, not a stale external tutorial. Detailed policy:
`docs/agents/runtime-official-source-policy.md`.

A platform blocker is not allowed merely because the current checkout,
installation, account, ACL, secret path, or service state cannot execute the
official solution immediately. First determine whether the official/upstream
path can still be fully prepared as configuration, jobspec, artifact, install
plan, operator step, or bounded validation while leaving the privileged/runtime
mutation for its human gate. "Cannot perform the operator action now" is not
the same as "cannot prepare the upstream solution now."

Before any `environment/operator/installation` blocker is final, record:

- `UPSTREAM_OFFICIAL_DOCS = CHECKED`
- `UPSTREAM_TUTORIALS_EXAMPLES = CHECKED`
- `UPSTREAM_GITHUB_SOURCE_TESTS = CHECKED`
- `UPSTREAM_GITHUB_ISSUES_PRS = CHECKED`
- `MATURE_GITHUB_ALTERNATIVES = CHECKED/NOT_NEEDED`
- `STANDARD_UPSTREAM_PATH_FOUND = YES/NO`
- `UPSTREAM_RESOLUTION_EXHAUSTED = YES/NO`
- `BLOCKER_ALLOWED = YES/NO`

If `STANDARD_UPSTREAM_PATH_FOUND = YES`, continue with that upstream path to the
maximum extent permitted by the current task. Stop only at the **specific**
human/operator/installation mutation that is actually unauthorized or
impossible. Do not convert that future operator step into a custom repair
subsystem.

Only when the required upstream-resolution pass is complete and no reusable
standard path can satisfy the requirement, or when the next unavoidable step
is a specifically unauthorized host mutation, may the task record an
`environment/operator/installation` blocker. Custom LIVE15 behavior remains a
last-last-last option and requires an explicit reason why all reusable upstream
paths are unsuitable.

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
- Was the mandatory upstream-resolution gate completed before any blocker or
  local implementation decision?
- Did the executing agent retrieve the current official procedure itself rather
  than rely on a frozen Project Brain/prompt copy?
- If a standard upstream path exists, did the task follow it as far as current
  authorization allows instead of stopping early or rewriting it locally?
- Are measured requirements, hashes, health/evidence and rollback boundaries
  explicit?
- Did the prompt state a dynamically chosen model and reasoning level without
  over-spending tokens for a simpler task?

If the answer to the first three questions is no, the migration is not ready
regardless of whether its tests pass. A platform blocker is also invalid if the
upstream-resolution gate was not completed.

Tracking: `GOV-PLATFORM-REUSE-001` / issue #88 and
`UPSTREAM-MIGRATION-SEQUENCE-001` / issue #90.
