# NOMAD migration status — 2026-08-30

This bounded detail file preserves the current Nomad migration state without forcing the compact Project Brain bootstrap to discard durable context. It is an indexed continuation record, not a Production deployment receipt or cutover authorization.

## Current durable state

- `NOMAD-POC-SECURE-001` is VERIFIED for the isolated POC boundary. The LocalService Windows-service model, native task recovery, service restart/allocation rediscovery, bad-update native auto-revert, and two-hour observation-only soak are already proven and must not be re-run merely because later tracking files change.
- `NOMAD-LIFECYCLE-UPSTREAM-AUDIT-001` merged in PR #85. Windows SCM and Nomad are the intended generic lifecycle owners; manual agent restart logic is superseded/fail-closed rather than extended.
- `NOMAD-AUTOMATION-FOUNDATION-001` merged in PR #86. The Nomad responsibility boundary, receipt contract, and upstream replacement matrix are durable architecture guidance.
- `NOMAD-FIRST-WORKLOAD-SHADOW-001` merged in PR #91. The sealed read-only `LIVE15ControlCenter` shadow passed its hash/ACL boundary, Nomad allocation/health checks, Maker review, Independent Checker review, and CI. It remains non-Production evidence only.
- The actual `LIVE15ControlCenter` runtime owner has not yet been changed. The tracked runtime ownership still points to `WinSW:LIVE15ControlCenter`; `MERGED != DEPLOYED != VERIFIED` continues to apply.

## Next bounded Nomad task

`NOMAD-CONTROL-CENTER-CUTOVER-PREP-001` is the next migration step.

Its purpose is to prepare, but not execute, the first real ownership migration of `LIVE15ControlCenter` from the current WinSW-owned lifecycle to `Windows SCM -> Nomad agent -> Nomad allocation -> LIVE15ControlCenter`.

The task must:

- reuse the already-verified generic Nomad POC evidence instead of re-running long generic soak/recovery proof;
- inspect the real ControlCenter entrypoint, working directory, ports, environment, local-secret boundary, Recorder/data reads, health/status outputs, WinSW packaging, release identity, and rollback path;
- keep Nomad/Windows SCM responsible for generic lifecycle, restart, health, update and revert behavior;
- keep LIVE15 responsible for typed projections, domain truth and fail-closed semantics;
- prepare exact source/artifact/jobspec identity and a bounded operator cutover/rollback plan;
- identify which ControlCenter-specific legacy lifecycle machinery can be retired only after a later successful cutover plus bounded runtime proof;
- stop before any service stop/restart, deployment, WinSW retirement, Recorder change, Production write, Hard Risk change, holdout access or training action.

A successful preparation task may conclude `READY_FOR_CONTROL_CENTER_CUTOVER = YES`, but it must still stop at the human deployment gate. Actual cutover belongs to a separate `NOMAD-CONTROL-CENTER-CUTOVER-001` task with explicit human authorization.

## Indexed evidence

- `docs/deployment/NOMAD_FIRST_WORKLOAD_SHADOW_001.md`
- `docs/project-brain/NOMAD_OVERNIGHT_HANDOFF_20260829.md`
- `docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md`
- `docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md`
- `deploy/windows/runtime-ownership.json`
- PR #85, PR #86, PR #91

## Project Brain size rule

The five bootstrap files remain bounded by `tests/test_agent_context.py`. When new durable detail would exceed that budget, preserve the information in a bounded detail file like this one and add an index pointer from the compact bootstrap. Do not satisfy the bootstrap budget by deleting durable facts or semantically compressing them until decision-relevant meaning is lost.
