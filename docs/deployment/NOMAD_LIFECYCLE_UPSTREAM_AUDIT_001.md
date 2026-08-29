# NOMAD-LIFECYCLE-UPSTREAM-AUDIT-001

**Result:** PASS for the bounded, evidence-only audit.  No Nomad process,
service, allocation, configuration, or data was changed while producing this
record.

**Scope.** The existing POC lifecycle helpers in immutable source commit
`c52c9d8` were compared with the official Nomad v2.0.5 CLI and Windows-service
behavior.  The audited scripts are `tools/nomad_poc/Invoke-NomadLifecycleCycle.ps1`
and `tools/nomad_poc/Invoke-NomadAgentRediscovery.ps1`.  This audit does not
authorize running either script.

## Upstream baseline

- `nomad job restart` is the native allocation-restart operation.  It restarts
  tasks in place and does not create a deployment; `-reschedule` is a distinct
  stop-and-migrate operation.  The observer's use of `job restart -yes` is
  therefore an upstream operation, not a LIVE15 restart implementation.
- `nomad job status -json`, `nomad alloc status -json`, and `nomad alloc checks
  -json` are native read paths.  A running allocation plus successful native
  checks is stronger evidence than a child PID or direct HTTP response alone.
- A Windows Nomad agent is owned by the native Windows Service Control Manager
  after `nomad windows service install` (or the documented `sc.exe` path).  The
  service, not a LIVE15 helper, owns process startup and restart boundaries.

## Findings against the existing helpers

1. `Invoke-NomadLifecycleCycle.ps1` is a thin observer around native Nomad
   commands.  Its `Wait-NomadHealthyAllocation` logic tolerates transient empty
   allocation snapshots, requires `DesiredStatus=run`, and requires successful
   native checks.  Its `-Run` mode still submits a restart and writes POC
   evidence, so it remains an explicitly authorized validation action rather
   than an unattended supervisor.
2. `Invoke-NomadAgentRediscovery.ps1` contains a deliberate fail-closed guard:
   `-Run` throws unless `-AllowUnsafeForceRestart` is supplied.  The guarded
   path uses `Stop-Process` followed by `Start-Process nomad agent`, which is a
   manual process lifecycle and conflicts with the documented Windows-service
   architecture.  `-AllowUnsafeForceRestart` must remain disabled and is not a
   supported overnight path.
3. Port/PID observation can supplement, but cannot replace, SCM service state,
   Nomad allocation state, and native check results.  No new supervisor,
   restart manager, PID controller, or rollback state machine is warranted.

## Decision and boundary

The service-model POC must use the official Nomad Windows service and the
existing constrained service bridge for lifecycle operations.  The manual
agent-restart helper is superseded by that boundary; it must not be extended or
re-enabled.  Any future rediscovery validation is a separately scoped,
non-Production operation that records the service query, Nomad allocation/check
state, allocation logs, and durable POC checkpoint before and after the native
service restart.

No Production path, holdout, trading write, training action, UAC operation, or
hosted-CI trigger is part of this audit.  Hosted CI remains
`CI_DEFERRED_QUOTA`.

## Official primary references

- [Nomad Windows service](https://developer.hashicorp.com/nomad/docs/deploy/production/windows-service)
- [Nomad Windows command](https://developer.hashicorp.com/nomad/commands/windows)
- [Nomad job restart](https://developer.hashicorp.com/nomad/commands/job/restart)
- [Nomad restart policy](https://developer.hashicorp.com/nomad/docs/job-declare/failure/restart)
- [Nomad service lifecycle and checks](https://developer.hashicorp.com/nomad/docs/job-specification/service)
- [Nomad v2.0.5 service-install source](https://raw.githubusercontent.com/hashicorp/nomad/v2.0.5/command/windows_service_install.go)
