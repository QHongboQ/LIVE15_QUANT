# NOMAD-AUTOMATION-FOUNDATION-001

**Status:** IN_PROGRESS / upstream-boundary review. This is one isolated,
docs-only task; it does not deploy Nomad, change a service, or authorize a
Production cutover.

## Objective

Define how verified Nomad v2.0.5 supplies the mature scheduling, task restart,
deployment health, update, rollback, and observation substrate for a future
non-Production LIVE15 adapter. LIVE15 retains only Kalshi data truth, strict
as-of/freshness and gap policy, persistence, Hard Risk, and safety boundaries.
Qlib, LEAN, model training, and Production integration are outside this task.

## Upstream contract

- The native Windows Service Control Manager owns the Nomad agent boundary;
  use the official `nomad windows service install` path and LocalService POC
  configuration. Do not build a LIVE15 supervisor or service manager.
- Nomad owns scheduling and allocation placement. `nomad job restart` is an
  in-place task restart; `-reschedule` is a distinct migration operation.
- Nomad owns task restart policy and deployment health. A jobspec `update`
  policy, native service checks, and `nomad job status`/`alloc status`/`alloc
  checks` are the authoritative operational signals.
- Nomad owns failed-update rollback through its native update/revert behavior.
  LIVE15 must not add a rollback state machine or infer health from a child PID
  or an HTTP response alone.
- The POC keeps `provider = "nomad"`; a Consul shadow or Production provider
  decision is a separate task with separate evidence.

## LIVE15 adapter boundary

The future adapter may translate Nomad observations into bounded evidence and
apply LIVE15 policy gates. It must not assume Nomad supplies Kalshi settlement
truth, strict as-of validity, gap closure, sizing, Hard Risk, execution
permission, or Production authorization. Any missing/stale/unsynchronized
input remains fail-closed.

## Acceptance and validation

Maker and Independent Checker must confirm, from official HashiCorp docs and
the pinned v2.0.5 source/release:

1. each lifecycle responsibility is assigned to Nomad/SCM or the thin LIVE15
   adapter, with no duplicate supervisor/restart/rollback path;
2. the verified isolated POC evidence is routed as capability evidence only,
   not as a Production self-healing claim;
3. the explicit Nomad discovery provider and LocalService/POC boundary remain
   intact; and
4. no code, framework, Production, holdout, Hard Risk, training, or trading
   change is required by this contract.

Validation for this docs-only task is `git diff --check` plus the repository's
local governance/document checks. Hosted CI remains `CI_DEFERRED_QUOTA`; that
state is not a pass and no merge is authorized by this task.

## Primary upstream references

- [Windows service](https://developer.hashicorp.com/nomad/docs/deploy/production/windows-service)
- [job restart](https://developer.hashicorp.com/nomad/commands/job/restart)
- [restart policy](https://developer.hashicorp.com/nomad/docs/job-declare/failure/restart)
- [update block](https://developer.hashicorp.com/nomad/docs/job-specification/update)
- [job revert](https://developer.hashicorp.com/nomad/commands/job/revert)
- [service discovery](https://developer.hashicorp.com/nomad/docs/job-declare/service-discovery)
- [v2.0.5 release](https://github.com/hashicorp/nomad/releases/tag/v2.0.5)

