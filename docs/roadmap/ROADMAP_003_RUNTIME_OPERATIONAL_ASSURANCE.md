# ROADMAP 003 — Runtime and operational assurance

## Purpose and authority

This volume plans operational assurance for LIVE15. It does not authorize a
Production cutover, Production write, Hard Risk change, training/promotion,
or frozen-holdout access. Runtime health and bounded evidence remain the truth;
this document is a route map, not a service receipt.

## Principles that govern every task

- One bounded function, isolated worktree/branch/PR, Maker validation,
  independent Checker review, and green CI when CI is authorized.
- Apply Upstream Reuse First and the thin-adapter rule. Nomad owns generic
  scheduling, allocation, restart, health, update and rollback mechanisms;
  LIVE15 should not recreate them.
- Apply the anti-spaghetti rule. Remove duplicate control planes and special
  recovery paths before adding a new mode.
- Preserve `ONE COMPONENT · ONE OWNER · ONE HEALTH TRUTH · ONE RECOVERY
  AUTHORITY`. A UI may show evidence; it cannot create health or authority.
- Fail closed for stale, gapped, malformed, unverifiable, or unauthorised
  state. Keep Production writes at zero unless separately approved.

## Web and Control Center

| Task | Intended output | Acceptance boundary |
| --- | --- | --- |
| WEB-CC-001 | Control Center truthful view of Recorder, archive, POC, and bounded evidence freshness | Explicit stale/unknown state; no health inference or production control beyond existing approved boundaries |
| WEB-CC-002 | Operator evidence navigation for POC matrices, archival throughput and fault results | Links/receipts are immutable or clearly timestamped; no log fabrication |
| WEB-CC-003 | Read-only POC status projection | POC status must remain visibly distinct from Production readiness/cutover |

## Shadow Recorder / data reliability

| Task | Intended output | Acceptance boundary |
| --- | --- | --- |
| SHADOW-REC-001 | SDK-authoritative Recorder shadow parity evidence | No replacement of the authoritative Recorder; typed SDK contract remains primary |
| SHADOW-REC-002 | Bounded mismatch taxonomy and replayable evidence | Missing/gapped state fails closed; no synthesized sequence or settlement label |
| SHADOW-REC-003 | Shadow telemetry surfaced through Control Center | Projection only; no automatic restart/write path |

## Throughput, soak and fault injection

1. **ST-005 archive throughput.** First establish a healthy read-only runtime.
   Measure ingress, archive and purge rates, backlog slope, HOT/COLD growth,
   quarantine/FAILED/WAITING and sequence-gap integrity over a valid 60-minute
   window. Optimize only a measured bottleneck and retain a regression/evidence
   trail.
2. **Operational soak.** Start with isolated POC duration and resource budget;
   define stop conditions, bounded logs, pass/fail metrics and cleanup before
   execution. Do not use Production as a load test.
3. **Fault injection.** Each matrix is reproducible, reversible and one fault
   class at a time: process crash, unhealthy check, port reservation failure,
   agent restart, delayed allocation visibility, log/data permission failure,
   update failure and rollback. Evidence must bind to the exact POC SHA and
   configuration.

## Nomad migration sequence

### Phase N0 — upstream suitability (completed research, not cutover)

Nomad 2.0.5 is a viable candidate for an isolated Windows AMD64 POC under the
official upstream service, `raw_exec`, restart, check-restart, update/history,
logging and security documentation. Its CE licensing/security/HA consequences
remain separate later human decisions.

### Phase N1 — secure isolated service boundary (active)

- Protect and hash-verify the Nomad binary and configuration.
- Run the service as LocalService with a fixed-action, least-privilege bridge.
- Permit only fixed POC lifecycle/inspection/cleanup actions.
- Resolve the single fixed HTTP.sys URLACL requirement for
  `http://127.0.0.1:18080/` and LocalService after independent review.
- Do not report end-to-end readiness until the real fixture binds, native health
  passes, service restart succeeds and allocation rediscovery is observed.

### Phase N2 — POC operational proof (planned after N1)

- Ten task-crash recovery cycles and ten health-failure recovery cycles.
- Five full agent-restart/rediscovery cycles with two spaced, consecutive
  Nomad-native healthy observations per cycle.
- Soak, throughput and fault-injection matrix with a bounded resource/time
  budget and no Production dependencies.
- Native job history, bad deployment, auto-revert/revert and rollback proof.
- Checker-reviewed evidence package; a green CI/PR documents code only.

### Phase N3 — separate decision point

Only after N2 is proven may a new explicitly authorized task evaluate any
Production migration. It must separately resolve HA/single-host trade-offs,
license terms, authentication/TLS/ACLs, raw-exec confinement, secrets,
data-path migration, rollback, monitoring, security audit, Hard Risk and a
human cutover decision. No approval is implied by this roadmap.

## Relationship to Paper, Shadow and future Production

Operational proof increases confidence in service ownership and data
reliability; it does not unlock training, Paper/Shadow promotion, execution or
Production trading. The existing ordering remains:

`Research Data Authority and runtime truth → controlled forward evidence →
formal training GO/NO-GO → Paper/Shadow gate → Hard Risk / security / human
approval → any tiny Production pilot`.
