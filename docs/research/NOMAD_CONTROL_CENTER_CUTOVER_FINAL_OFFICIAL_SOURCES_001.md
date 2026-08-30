# NOMAD-CONTROL-CENTER-CUTOVER-FINAL-001 official-source evidence

**Retrieved:** 2026-08-29 (task time)
**Scope:** Current primary-source evidence for a reviewable, thin Nomad
integration. This is not a copied vendor deployment procedure and does not
authorize a service, ACL, registry, credential, or runtime mutation.

## Result

`OFFICIAL_DOCS_RETRIEVED_AT_TASK_TIME = YES`
`PROMPT_COPIED_VENDOR_PROCEDURE_USED_AS_AUTHORITY = NO`

```text
UPSTREAM_OFFICIAL_DOCS = CHECKED
UPSTREAM_TUTORIALS_EXAMPLES = CHECKED
UPSTREAM_GITHUB_SOURCE_TESTS = CHECKED
UPSTREAM_GITHUB_ISSUES_PRS = CHECKED
MATURE_GITHUB_ALTERNATIVES = NOT_NEEDED
STANDARD_UPSTREAM_PATH_FOUND = YES
UPSTREAM_RESOLUTION_EXHAUSTED = YES
BLOCKER_ALLOWED = NO
```

The official [deploy and update a job tutorial](https://developer.hashicorp.com/nomad/tutorials/get-started/gs-deploy-job),
maintained job-declaration documentation, and raw-exec Windows test are the
relevant current tutorial/example path for this small, host-process deployment.
Official Windows and CPython documentation cover the remaining service-account
and virtual-environment inputs. The standard path was usable, so no alternative
project or local generic implementation was needed.

Official Nomad, Microsoft, and CPython sources cover the generic mechanisms
in scope. No LIVE15 lifecycle, rollback, ACL, or runtime-management subsystem
is justified by this research.

## Nomad v2.0.5

- The official [v2.0.5 release](https://github.com/hashicorp/nomad/releases/tag/v2.0.5)
  identifies release tag `v2.0.5` and commit `5c8612b`; this is the source tag
  used below rather than `main`.
- The official [raw_exec documentation](https://developer.hashicorp.com/nomad/docs/job-declare/task-driver/raw_exec)
  requires an absolute path for a host executable, warns that the driver has
  no isolation, and permits `NT AUTHORITY\\LocalService` as a less-privileged
  task user on Windows when Nomad runs as a system service.
- The matching [v2.0.5 raw_exec source](https://raw.githubusercontent.com/hashicorp/nomad/v2.0.5/drivers/rawexec/driver.go#L363-L447)
  passes the configured `cfg.User` to `executor.ExecCommand.User`; the
  [v2.0.5 Windows executor](https://raw.githubusercontent.com/hashicorp/nomad/v2.0.5/drivers/shared/executor/executor_windows.go#L42-L96)
  uses a service logon token for a nonempty domain-qualified user. The
  [v2.0.5 Windows driver test](https://github.com/hashicorp/nomad/blob/v2.0.5/drivers/rawexec/driver_windows_test.go)
  exercises Windows driver behavior; upstream [PR #25496](https://github.com/hashicorp/nomad/pull/25496)
  records the Windows task-user support. These sources support use of the
  pinned upstream feature, not a local supervisor.
- Official [service](https://developer.hashicorp.com/nomad/docs/job-specification/service)
  and [check](https://developer.hashicorp.com/nomad/docs/job-specification/check)
  documentation define the Nomad service provider and native HTTP checks. An
  HTTP check uses the registered service port and relative health path; a
  non-2xx response fails the check.
- The official [update block](https://developer.hashicorp.com/nomad/docs/job-specification/update)
  defines `health_check = "checks"` and `auto_revert = true` for failed
  deployments. This is the native update/revert ownership, rather than a
  LIVE15 rollback controller.
- The official [job validate command](https://developer.hashicorp.com/nomad/commands/job/validate)
  checks an HCL jobspec for syntax and validation errors and returns exit code
  zero on success. The [v2.0.5 implementation](https://raw.githubusercontent.com/hashicorp/nomad/v2.0.5/command/job_validate.go#L80-L182)
  also records that driver configuration is not validated when no agent
  connection is available. Therefore static validation is necessary but not
  evidence that the target client can launch a LocalService raw_exec task.

**Nomad conclusion:** with raw_exec there is no filesystem-security boundary
inside an allocation. The interpreter and application paths therefore need to
be absolute, operator-provisioned, non-user-writable paths; deployment health,
restart, update, and revert remain native Nomad responsibilities.

## Windows service identity and listener facts

- Microsoft defines [LocalService](https://learn.microsoft.com/en-us/windows/win32/services/localservice-account)
  as the predefined least-privileged local service account, named
  `NT AUTHORITY\\LocalService`, with anonymous network credentials. Microsoft
  also documents its well-known SID (`S-1-5-19`) in the [local accounts
  reference](https://learn.microsoft.com/en-us/windows/security/identity-protection/access-control/local-accounts).
- Microsoft documents that a [DACL is evaluated for requested file access](https://learn.microsoft.com/en-us/windows/win32/secauthz/dacls-and-aces).
  That supports effective LocalService read/execute verification only; it does
  not imply an automated ACL repair mechanism.
- The official Windows [Winsock bind](https://learn.microsoft.com/en-us/windows/win32/api/winsock/nf-winsock-bind)
  API documents an address-in-use failure when another socket owns the same
  address/port. Capturing the old owner and listener, stopping the old owner
  before the new owner binds, and checking the resulting listener avoids a
  duplicate ControlCenter/port claim.

## CPython 3.13.15 runtime model

- The official [Python 3.13.15 release](https://www.python.org/downloads/release/python-31315/)
  and official [CPython v3.13.15 GitHub release](https://github.com/python/cpython/releases/tag/v3.13.15)
  identify the installed interpreter release.
- The official [Python 3.13 `venv` documentation](https://docs.python.org/3.13/library/venv.html)
  describes Windows virtual environments under `Scripts` and supports invoking
  their interpreter by full path without activation. It also recommends
  recreating, rather than moving, environments.
- The official [Python Windows documentation](https://docs.python.org/3.13/using/windows.html#the-embeddable-package)
  says the embeddable package omits normal pip dependency management. It is not
  a substitute for the already provisioned, pip-managed runtime.

## Boundary conclusion

The current official upstream path is: Windows SCM owns the Nomad agent; Nomad
owns the LocalService raw_exec allocation, native HTTP health, restart,
deployment, and failed-update reversion; the release pipeline supplies the
immutable source identity; LIVE15 supplies only its entrypoint, configuration
mapping, truthful health contract, and evidence. This evidence does not supply
or replace an operator deployment recipe.
