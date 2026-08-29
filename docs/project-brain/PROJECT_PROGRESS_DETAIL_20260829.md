# Project Brain detailed overnight update — 2026-08-29 00:53 PT

This is the bounded detailed companion to the compact `PROJECT_PROGRESS.md`.
It records durable task state and evidence routing, not a deployment receipt or
an authorization to operate Production.

## Reconciliation basis

- Protected-main authority resolved for this update: `origin/main` at
  `0466423` (PR #69 merge). `main` is not a deployment receipt.
- Open isolated POC work visible at this point: draft PR #71
  `agent/nomad-lifecycle-observer-001`; its visible final check suite was
  green on its then-latest SHA. Draft PR #72 is a dependent safety branch with
  a failing full suite and must not be merged or used as completion evidence.
- The Windows canonical checkout is intentionally not altered by this task;
  it contains unrelated mutable runtime/release material. This documentation
  update uses its own `agent/project-brain-overnight-20260829` worktree.

## Confirmed engineering operating rules

1. **Maker / Checker / protected main.** One bounded feature owns one branch
   and one PR. Maker supplies scoped implementation and local evidence;
   Checker independently reviews the original contract and diff without
   modifying the Maker worktree. Human review remains required before a merge.
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

## Nomad secure POC: confirmed progress and current blocker

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

### Current controlled blocker

- The actual LocalService fixture cannot register `HttpListener` on
  `http://127.0.0.1:18080/` without the matching HTTP.sys URLACL.
- The allowed action is intentionally fixed to that loopback prefix and
  `NT AUTHORITY\LOCAL SERVICE`. It must not accept request-provided URLs,
  identities, commands, paths, PowerShell, or executables.
- The repaired runner recognized the action, but its empty-list parser had a
  whitespace expression that consumed the line break. This is a parser defect,
  not an allowlist or authorization-scope failure.
- Therefore `PRE_SLEEP_ADMIN_READY` is superseded as a full POC conclusion and
  `PRE_SLEEP_END_TO_END_READY=True` must not be reported yet. The candidate
  parser correction must be independently reviewed; if installing it still
  requires elevation, that is a new explicit human UAC gate, not carried over
  from the earlier correction.

### Required next proof, once the blocker is validly removed

1. Verify the exact reviewed candidate hash and protected runner ACL/rollback
   backup.
2. Create/query only the fixed POC URLACL and inspect HTTP.sys read-back.
3. Start the real LocalService workload; prove fixture bind on `18080` and
   Nomad-native health.
4. Exercise workload restart/recovery, a Nomad service restart and allocation
   rediscovery; capture bounded log/data/config access evidence.
5. Only then schedule isolated POC soak, throughput, fault injection,
   ten-cycle crash/health recovery, five-cycle agent rediscovery, native update
   and rollback/history proof. A new human decision is required before any
   Production design/cutover work.

## Current overnight workstreams

| Workstream | Status | Next bounded action | Prohibitions |
| --- | --- | --- | --- |
| `NOMAD-POC-SECURE-001` | BLOCKED_PENDING_FIXED_URLACL_CORRECTION | Checker-review parser correction; use UAC only if separately approved; run real end-to-end burn-in | No Production service/data/control-plane changes; no arbitrary bridge capability |
| `ST-005` archive/purge throughput | IN_PROGRESS / proof pending | Read-only healthy-runtime 60-minute catch-up proof; processing must exceed ingress with backlog decline and intact safety | No restart, compaction, retention mutation, or speculative optimization |
| Web / Control Center | Existing truth/observability foundation retained | Keep UI as a truthful projection of runtime evidence; deepen only through independently scoped tasks | UI must not manufacture health, control Production, or replace runtime authority |
| Shadow Recorder | Existing SDK reliability shadow retained | Compare SDK-authoritative recorder behavior with bounded parity/reliability evidence; isolate mismatch | Not a replacement Recorder or a backdoor execution channel |
| Fault/soak assurance | Planned after a healthy POC boundary | Use bounded, reproducible, reversible POC matrices with evidence | No destructive stress against Production or frozen holdout |

## CI quota policy and this one-time exception

`CI_DEFERRED_QUOTA` remains the durable default: use local validation and
draft PRs, and do not treat a deferred hosted run as PASS. The user granted a
single narrow exception for this Project-Brain update:

- exactly one GitHub-hosted CI run;
- only after the tracking branch has one final prepared SHA;
- no intermediate push, no manual rerun, no retry authorization;
- record the branch/PR, exact SHA and final result in the task response;
- the exception does not re-enable CI for any other task or change the default
  quota policy; and
- no merge follows from a green result.

## Forward roadmap routing

The detailed operational assurance roadmap is
`docs/roadmap/ROADMAP_003_RUNTIME_OPERATIONAL_ASSURANCE.md`. It is deliberately
separate from model/research planning and preserves the order:

`runtime truth → bounded operations proof → POC assurance → separate human
cutover decision → Paper/Shadow evidence → training gate → any future
Production decision`.
