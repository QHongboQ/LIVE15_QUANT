# NOMAD-CONTROL-CENTER-CUTOVER-001 — runtime prerequisite receipt

## Scope and result

This is the task-time, read-only cutover receipt for the first actual
`LIVE15ControlCenter` ownership migration.  It does not authorize or report a
deployment, service restart, WinSW retirement, Recorder change, or Production
write.

`CONTROL_CENTER_NOMAD_CUTOVER = BLOCKED`.

The single blocker is that no installed Python runtime is both
LocalService-readable/executable, non-user-writable, and populated with the
locked ControlCenter dependencies.  That prerequisite is required before a
Nomad allocation can safely start, so it cannot be deferred to post-cutover
verification.

## Task-time basis

- Protected source basis: clean `origin/main`
  `4488331b94abdb69075f90fafcc30a3f07496035`.
- Host Nomad: `v2.0.5`, Windows SCM service account
  `NT AUTHORITY\\LocalService`; the existing client has `raw_exec` enabled and
  a loopback host network.
- The existing `LIVE15ControlCenter` Windows service remains `Running` under
  its WinSW owner.  No service or Nomad job was changed by this task.
- `LIVE15Recorder` remained `Running`; it was not read, modified, restarted,
  or otherwise included in the cutover boundary.
- The current interpreter is CPython `3.13.15` at the user installation path.
  Its core standard-library and `pip check` verification passed, but importing
  the required ControlCenter package `fastapi` failed.  Its executable ACL
  grants full control to `SYSTEM`, `BUILTIN\\Administrators`, and the interactive
  user only; it does not grant `NT AUTHORITY\\LocalService` read/execute.
- The existing `D:\\LIVE15_QUANT\\.venv\\Scripts\\python.exe` is not an eligible
  substitute: it and its parents inherit `NT AUTHORITY\\Authenticated Users:(M)`.
  A user-writable executable must not become a Nomad LocalService task runtime.
- The existing ControlCenter configuration contains only external credential
  *path-reference variable names*.  No secret content was read.  A future
  candidate may retain that external-reference contract, but it still requires
  an operator-installed minimum-read boundary for LocalService.

## Reused release and upstream evidence

`DEP-PKG-001` / `DEP-PKG-002` remain the sole LIVE15 release provenance system.
`PRODUCTION_RELEASE_PIPELINE_001` verifies a clean detached Git checkout,
creates `git archive <SHA>`, inventories and hashes the application payload,
and supports verified legacy rollback identity.  It deliberately classifies
Python/venv as external mutable host tooling; it does not package an interpreter
or third-party dependency runtime.  Therefore it can produce an auditable code
artifact from the protected SHA, but not a runnable immutable ControlCenter
artifact in the current host state.

Task-time upstream sources were used as implementation authority:

- HashiCorp Nomad `v2.0.x` raw-exec documentation:
  <https://developer.hashicorp.com/nomad/docs/job-declare/task-driver/raw_exec>
  and task documentation:
  <https://developer.hashicorp.com/nomad/docs/job-specification/task>.
  On Windows, a system-service Nomad agent may specify a lower-privilege
  service user such as `NT AUTHORITY\\LocalService`; host executables must use
  absolute paths.
- HashiCorp artifact and update documentation:
  <https://developer.hashicorp.com/nomad/docs/job-specification/artifact> and
  <https://developer.hashicorp.com/nomad/docs/job-specification/update>.
  Artifact checksums and native health-gated auto-revert are available once a
  valid runtime artifact exists.
- HashiCorp's Windows migration tutorial:
  <https://developer.hashicorp.com/nomad/tutorials/job-specifications/job-spec-java-windows>.
  It maps an existing application/runtime startup contract into a declarative
  job; it does not supply an application runtime.
- Official Nomad `v2.0.5` raw-exec source and tests:
  <https://github.com/hashicorp/nomad/blob/v2.0.5/drivers/rawexec/driver.go>
  and
  <https://github.com/hashicorp/nomad/blob/v2.0.5/drivers/rawexec/driver_test.go>.
  The source confirms the raw-exec driver is an upstream-owned lifecycle
  mechanism, not a reason to add a LIVE15 supervisor.
- CPython `3.13.15` Windows documentation:
  <https://docs.python.org/3.13/using/windows.html>.  CPython supports an
  application-local embedded distribution, but its installer must provide
  third-party packages alongside it; `pip`-managed dependencies are not the
  supported embedded-distribution pattern.  Introducing such a bundle here
  would be a new LIVE15 packaging system rather than reuse of `DEP-PKG`.
- Microsoft LocalService account documentation:
  <https://learn.microsoft.com/en-us/windows/win32/services/localservice-account>.
  LocalService is a distinct, minimal-privilege service identity, so an
  interactive-user-only executable ACL is not a substitute for its explicit
  read/execute installation boundary.

### Required upstream-resolution receipt

```text
UPSTREAM_OFFICIAL_DOCS = CHECKED
UPSTREAM_TUTORIALS_EXAMPLES = CHECKED
UPSTREAM_GITHUB_SOURCE_TESTS = CHECKED
UPSTREAM_GITHUB_ISSUES_PRS = CHECKED
MATURE_GITHUB_ALTERNATIVES = NOT_NEEDED
STANDARD_UPSTREAM_PATH_FOUND = YES
UPSTREAM_RESOLUTION_EXHAUSTED = YES
BLOCKER_ALLOWED = YES
```

The standard path is Nomad's Windows `raw_exec` task-user model plus a
checksum-bound artifact and Nomad-native health/update controls.  The source
and test links above are the task-time implementation evidence.  Official
GitHub Issue [#6431](https://github.com/hashicorp/nomad/issues/6431) was also
checked: it reinforces that `raw_exec` is an explicitly enabled, upstream
security-sensitive driver rather than a place to add local execution logic.
Issue [#8387](https://github.com/hashicorp/nomad/issues/8387) was checked for
the Windows raw-exec restart history; generic restart/recovery evidence is
already covered by `NOMAD-POC-SECURE-001` and is not reimplemented here.  No
mature third-party alternative was investigated because the current official
Nomad and CPython paths are sufficient to identify the missing installation
prerequisite.

Upstream resolution is exhausted for this host state: Nomad provides process
ownership after a task has an executable runtime, and CPython provides a
supported app-local form only when an installer supplies the dependency bundle.
Neither upstream mechanism can safely derive that runtime from the observed
user-writable venv or from the user interpreter that lacks the dependencies.
The remaining action is therefore an environment/operator installation gate,
not a LIVE15 implementation gap; `BLOCKER_ALLOWED = YES` applies.

No copied vendor procedure was used as implementation authority.

## Decision and required operator action

No Nomad ControlCenter jobspec was submitted because a declarative job pointing
at either observed interpreter would violate the executable trust boundary or
would fail its dependency import before truthful health could be established.
Creating a prestart installer, portable-runtime builder, ACL/UAC repair path,
or second release pipeline is prohibited by the upstream-reuse and subtractive
replacement policy.

The exact next operator action is to provision, through the approved Windows
installation process, one non-user-writable, LocalService-readable/executable
CPython 3.13 runtime that contains the pinned ControlCenter dependency set.
It must be separate from mutable data, runtime state, logs, and external secret
contents.  The operator should provide its fixed path and artifact/dependency
hash evidence.  A resumed cutover task can then build the clean-SHA code archive
through the existing release pipeline, define the checksum-bound Nomad artifact
and native health check, validate it, and perform the authorized reversible
owner transition.

Until then:

- `ACTIVE_CONTROL_CENTER_OWNER = WinSW:LIVE15ControlCenter`.
- `CONTROL_CENTER_SERVICE_CHANGE_PERFORMED = NO`.
- `RECORDER_TOUCHED = NO`.
- `PRODUCTION_WRITES = 0`.
- Existing WinSW rollback capability remains preserved because no cutover was
  attempted.
