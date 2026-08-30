# NOMAD-CONTROL-CENTER-CUTOVER-RESUME-001 official-source evidence

**Retrieved:** 2026-08-29 (task time)
**Scope:** Primary-source evidence only. This note does not prescribe a vendor
procedure, inspect credentials, or authorize a service, ACL, or runtime change.

## Result

`OFFICIAL_DOCS_RETRIEVED_AT_TASK_TIME = YES`
`PROMPT_COPIED_VENDOR_PROCEDURE_USED_AS_AUTHORITY = NO`

The official Nomad, CPython, and Microsoft sources cover the generic behaviour
needed for a thin ControlCenter Nomad integration. A mature alternative or
LIVE15-owned lifecycle/runtime subsystem is not indicated.

## Nomad 2.0.5

- The [Nomad v2.0.5 release](https://github.com/hashicorp/nomad/releases/tag/v2.0.5)
  identifies the released tag and commit (`5c8612b`), including the native
  service-check SHA-256 change. The workload host must use its observed binary
  version; this document does not infer that a different release is installed.
- The official [raw_exec documentation](https://developer.hashicorp.com/nomad/docs/job-declare/task-driver/raw_exec)
  says a host executable must use an absolute path, permits an artifact-relative
  executable, warns that raw_exec has no isolation, and specifically permits a
  less-privileged Windows service user such as `NT AUTHORITY\\LocalService` when
  Nomad runs as a Windows system service.
- The matching [v2.0.5 raw_exec source](https://github.com/hashicorp/nomad/blob/v2.0.5/drivers/rawexec/driver.go#L2120-L2211)
  passes `cfg.User` to `executor.ExecCommand.User`; its capabilities declare
  `FSIsolation: fsisolation.None`. The official [v2.0.5 tests](https://github.com/hashicorp/nomad/blob/v2.0.5/drivers/rawexec/driver_test.go#L2824-L2943)
  include Windows raw-exec behaviour. The upstream implementation change is
  documented by [PR #25496](https://github.com/hashicorp/nomad/pull/25496),
  “raw_exec windows: add support for setting the task user”.
- The official [artifact block documentation](https://developer.hashicorp.com/nomad/docs/job-specification/artifact)
  states that Nomad fetches/unpacks artifacts into the task working directory
  and verifies a configured checksum before task start, returning an error when
  it does not match. This supports reusing an immutable SHA-identified package,
  rather than inventing a second package manager.
- The official Windows migration [tutorial](https://developer.hashicorp.com/nomad/tutorials/job-specifications/job-spec-java-windows)
  maps existing application start/configuration into a Nomad task and uses a
  task artifact fetched into the allocation. It is evidence for the generic
  Nomad pattern only, not a copied ControlCenter deployment recipe.
- The official [service](https://developer.hashicorp.com/nomad/docs/job-specification/service)
  and [check](https://developer.hashicorp.com/nomad/docs/job-specification/check)
  documents confirm that the Nomad service provider supports native `http` and
  `tcp` checks; HTTP checks use a service port and relative path, and allocation
  check state is inspectable with `nomad alloc status` / `nomad alloc checks`.
- The official [update block documentation](https://developer.hashicorp.com/nomad/docs/job-specification/update)
  defines `health_check = "checks"` and `auto_revert = true`: a failed
  deployment returns to the last stable job. This is the native update/revert
  mechanism; no LIVE15 rollback controller is required.

**Nomad conclusion:** A job can use a verified immutable application artifact,
an absolute, operator-provisioned interpreter path, `task.user =
"NT AUTHORITY\\LocalService"`, and a Nomad-native HTTP health check. Because
raw_exec intentionally provides no filesystem isolation, its artifact and
runtime must be non-user-writable and hash-audited by the existing release
process and operator installation gate.

## CPython 3.13.15 runtime model

- The [CPython 3.13.15 release page](https://www.python.org/downloads/release/python-31315/)
  identifies 3.13.15 and publishes the Windows installer/source checksums. The
  official [CPython v3.13.15 GitHub release](https://github.com/python/cpython/releases/tag/v3.13.15)
  and [source tag](https://github.com/python/cpython/tree/v3.13.15) are the
  corresponding source evidence.
- The [Python 3.13 `venv` documentation](https://docs.python.org/3.13/library/venv.html)
  describes a Windows virtual environment as a base-interpreter-linked
  environment with `Scripts` and `Lib\\site-packages`; it also states that a
  full interpreter path can be used without activation and that environments
  should be recreated rather than moved.
- The [Python 3.13 Windows documentation](https://docs.python.org/3.13/using/windows.html#the-embeddable-package)
  says the embeddable ZIP is isolated, excludes pip, and does not support normal
  pip dependency management. It is therefore not an official substitute for a
  pre-provisioned, pip-managed ControlCenter virtual environment.

**Python conclusion:** Audit and use the operator-provisioned venv by its
absolute `Scripts\\python.exe` path. Do not replace it with the embeddable
distribution, add an in-job installer, or create a second dependency-management
or runtime-packaging system.

## Windows service identity and file access

- Microsoft defines [LocalService](https://learn.microsoft.com/en-us/windows/win32/services/localservice-account)
  as the least-privileged local service account (`NT AUTHORITY\\LocalService`,
  well-known SID `S-1-5-19`); see also its [local-account reference](https://learn.microsoft.com/en-us/windows/security/identity-protection/access-control/local-accounts).
- Microsoft [file security and access-rights](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights),
  [DACL/ACE](https://learn.microsoft.com/en-us/windows/win32/secauthz/dacls-and-aces),
  and [file-access constants](https://learn.microsoft.com/en-us/windows/win32/fileio/file-access-rights-constants)
  documentation establish that effective access is the process token evaluated
  against the object's DACL, and that read/execute rights are the relevant file
  rights for an interpreter and its import hierarchy.

**Windows conclusion:** The correct non-invasive validation is an effective
LocalService read/execute test of the entire base-Python and venv hierarchy.
If it fails, ACL modification remains an operator/install gate rather than a
LIVE15 ACL manager or automated repair path.

## Boundary summary

Official mechanisms cover: Windows SCM ownership of the Nomad agent,
Nomad allocation/task lifecycle and recovery, native service health checks,
SHA-verified artifact retrieval, and native failed-update reversion. LIVE15
need only supply the domain entrypoint, thin configuration, health contract,
and evidence. No mature GitHub alternative was consulted because the official
mechanisms cover this scope.
