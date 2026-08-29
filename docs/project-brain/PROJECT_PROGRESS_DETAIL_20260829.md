# Project Brain detailed overnight update — 2026-08-29

This is the bounded detailed companion to the compact `PROJECT_PROGRESS.md`.
It records durable task state and evidence routing, not a deployment receipt or
an authorization to operate Production.

## Reconciliation basis

- Protected-main authority resolved for this update: `origin/main` at
  `0f72faf` (PR #73 merge). `main` is not a deployment receipt.
- Open isolated POC work visible at this point: draft PR #71
  `agent/nomad-lifecycle-observer-001`; its visible final check suite was
  green on its then-latest SHA. Draft PR #72 is a dependent safety branch with
  a failing full suite and must not be merged or used as completion evidence.
- The Windows canonical checkout is intentionally not altered by this task;
  it contains unrelated mutable runtime/release material. This documentation
  update uses its own `agent/nomad-poc-project-brain-001` worktree.

## Confirmed engineering operating rules

1. **Maker / Checker / protected main.** One bounded feature owns one branch
   and one PR. Maker supplies scoped implementation and local evidence;
   Checker independently reviews the original contract and diff without
   modifying the Maker worktree. This task does not authorize a merge; protected-main governance and the standing authority in `AGENTS.md` remain unchanged.
2. **Upstream Reuse First.** Search official docs and release notes, pinned
   dependency source/tests/examples, upstream issues/PRs/discussions, mature
   license-compatible implementations, then broader authority before a local
   reproduction and narrow LIVE15 change.
3. **Thin adapter.** Keep generic transport, scheduling, lifecycle and
   protocol behavior upstream. LIVE15 adds only Kalshi-domain, strict-as-of,
   reliability, persistence and safety semantics behind the smallest practical
   adapter.
4. **Anti-spaghetti.** A third/fourth execution mode, duplicated modern/legacy
   path, special-case nesting or contradictory invariant is a consolidation
   trigger, not a reason to add another patch. A passing regression alone is
   not sufficient.
5. **Safety.** No merge by this task; no Production cutover, Production writes,
   Hard Risk changes, training/promotion, or holdout access. `MERGED !=
   DEPLOYED != VERIFIED` remains mandatory.

## Nomad secure POC: verified service-model evidence and remaining assurance

### Completed/verified at the capability boundary

- A protected bridge candidate was independently reviewed and the intended
  runner replacement was hash-verified, kept a rollback backup and preserved
  its secure ACL.
- The POC Nomad binary integrity/ACL preparation, service query/start/stop/
  restart, LocalService account enforcement, POC-only inspection and bounded
  cleanup paths were available through the bridge without a general elevated
  command channel.
- The initially reported lifecycle evidence was:
  `PROTECTED_NOMAD_BINARY_READY=True`, `BRIDGE_CHECKER_PASS=True`,
  `NOMAD_SERVICE_ACCOUNT=LocalService`, and POC-only lifecycle readiness.
  These facts do **not** equal full workload readiness.

### Resolved privilege boundary and completed proof

- The native Windows HTTP.sys URLACL for only
  `http://127.0.0.1:18080/` and `NT AUTHORITY\LOCAL SERVICE` was configured
  with `netsh`, immediately read back, and left the protected bridge unchanged.
- The protected Nomad v2.0.5 binary and LocalService service model were
  re-verified. A real LocalService fixture bound loopback `18080`, passed
  Nomad-native HTTP health, survived workload restart and service restart, and
  rediscovered its allocation with POC-only config/data/log access.
- A checked-index native good update became stable. A separately parsed bad
  jobspec returned HTTP 503; Nomad marked its deployment failed at the progress
  deadline and automatically reverted to the prior stable jobspec. The
  recovered allocation again passed native health and HTTP 200. This used
  Nomad's update/revert behavior, not a LIVE15 recovery controller.
- Evidence is isolated under `D:\LIVE15_NOMAD_POC\generic-poc\logs`, notably
  `pre-sleep-end-to-end-burn-in-20260829T0800Z.log` and
  `nomad-native-auto-revert-20260829T0811Z.log`. The external POC handoff
  records their SHA-256 receipts and the controlled failure-marker cleanup.

### Remaining bounded assurance

1. Complete the active observation-only two-hour soak with durable periodic
   checkpoint evidence.
2. Preserve the explicit `provider = "nomad"` POC boundary. Native discovery
   does not imply health-filtered consumer discovery or select a Production
   provider; see
   `docs/deployment/NOMAD_SERVICE_DISCOVERY_PROVIDER_POLICY_001_UPSTREAM_RESEARCH.md`.
3. Any future Production design/cutover, Consul shadow, or alternate consumer
   remains a separately scoped task with its own authority and evidence.

## Current overnight workstreams

| Workstream | Status | Next bounded action | Prohibitions |
| --- | --- | --- | --- |
| `NOMAD-POC-SECURE-001` | IN_PROGRESS / burn-in and native auto-revert PASS; bounded soak running | Complete the observation-only soak and retain its final checksum/checkpoint evidence | No Production service/data/control-plane changes; no arbitrary bridge capability |
| `ST-005` archive/purge throughput | IN_PROGRESS / proof pending | Read-only healthy-runtime 60-minute catch-up proof; processing must exceed ingress with backlog decline and intact safety | No restart, compaction, retention mutation, or speculative optimization |
| Web / Control Center | Existing truth/observability foundation retained | Keep UI as a truthful projection of runtime evidence; deepen only through independently scoped tasks | UI must not manufacture health, control Production, or replace runtime authority |
| Shadow Recorder | Existing SDK reliability shadow retained | Compare SDK-authoritative recorder behavior with bounded parity/reliability evidence; isolate mismatch | Not a replacement Recorder or a backdoor execution channel |
| Fault/soak assurance | Planned after a healthy POC boundary | Use bounded, reproducible, reversible POC matrices with evidence | No destructive stress against Production or frozen holdout |

## CI quota policy

`CI_DEFERRED_QUOTA` is active: use local validation and draft PRs when useful,
but do not intentionally trigger GitHub-hosted CI or treat deferred CI as PASS.
The required final hosted CI may run only after the user explicitly re-enables
quota, and no merge follows from a green result.

## Forward roadmap routing

The detailed operational assurance roadmap is
`docs/roadmap/ROADMAP_003_RUNTIME_OPERATIONAL_ASSURANCE.md`. It is deliberately
separate from model/research planning and preserves the order:

`runtime truth → bounded operations proof → POC assurance → separate human
cutover decision → Paper/Shadow evidence → training gate → any future
Production decision`.

## New-chat continuation pointer

Use `NOMAD_OVERNIGHT_HANDOFF_20260829.md` for the compact continuation
snapshot: it records the resolved URLACL/UAC sequence, verified POC boundary,
active soak state, recovery protocol and the exact safety constraints that a
new chat must preserve. This detailed file remains the longer history; neither
file is a Production authorization or a replacement for live evidence.
