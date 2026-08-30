# NOMAD-CONTROL-CENTER-CUTOVER-RESUME-001 — candidate artifact gate

## Result and scope

`CONTROL_CENTER_NOMAD_CUTOVER = BLOCKED`.

The single smallest blocker is the absence of a clean-SHA, hash-verifiable
ControlCenter application artifact in a non-user-writable, LocalService-readable
release root. The newly provisioned Python runtime is valid, but an interpreter
is not an application artifact. The current Nomad client allocation root is
mutable by `Authenticated Users`, and cannot become the code source for a
LocalService workload.

No ControlCenter service, WinSW definition, Nomad service allocation, Recorder,
Production write path, ACL, UAC, registry, data file, or secret was modified by
this task. The temporary Nomad `batch` runtime-contract job completed and was
purged; it never opened a port or ran LIVE15 application code.

## Task-time basis

- Starting protected source: `origin/main` at
  `55b1a076c6f836186da95a04efa0bd26b83e1760`.
- Current active owner remains `WinSW:LIVE15ControlCenter`: SCM reports the
  service `Running` as `LocalSystem`, with WinSW process PID `5984` and child
  Python PID `7176`. It was observed only; it was not stopped or restarted.
- `LIVE15Recorder` was not queried, restarted, or otherwise touched.
- Existing WinSW configuration was inspected only for the entrypoint and
  environment-variable *names*. It starts `-m live15_quant.control_center` from
  the legacy root and references two external Kalshi credential paths. No
  credential content or path value was read.
- The ControlCenter's truthful native endpoint is `/api/health`; its code binds
  to `127.0.0.1` and uses a configured UI port. A future Nomad service check
  must call that endpoint, not a synthetic success endpoint.

## Reused upstream and release mechanisms

`DEP-PKG-001` / `DEP-PKG-002` remain the sole release provenance mechanism.
`live15_quant.release_pipeline build` verifies a clean detached Git checkout,
uses `git archive <SHA>`, inventories every payload file, records the
requirements-lock hash, and `verify-package` fails closed on any inventory or
lock mismatch. It is deliberately package-only and does not own a service.

The task-time official-source investigation is recorded in
`docs/research/NOMAD_CONTROL_CENTER_CUTOVER_RESUME_OFFICIAL_SOURCES_001.md`.
It confirms the supported composition: Nomad `raw_exec` with an absolute host
executable, a checksum-bound artifact, Nomad-native HTTP checks and health-gated
`auto_revert`; CPython's normal venv runtime; and Windows LocalService ACL
boundaries. No copied prompt procedure was used as authority, no third-party
deployment framework is required, and no LIVE15 lifecycle implementation was
added.

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

## Runtime and dependency evidence

The operator-provisioned runtime is suitable as the external interpreter:

```text
base interpreter = C:\Program Files\LIVE15\Python313\python.exe
runtime interpreter = C:\Program Files\LIVE15\ControlCenterRuntime\Scripts\python.exe
CPython = 3.13.15
runtime-python SHA-256 = 72B29481593C5DA37C99248C82777FBFB56217EA7809B771BC760D0A9ECB179B
pip check = PASS
```

Both `C:\Program Files\LIVE15` and `ControlCenterRuntime` grant
`BUILTIN\Users` read/execute only, while `SYSTEM` and `Administrators` retain
full control. Nomad service account `NT AUTHORITY\LocalService` therefore
executes the runtime by inheriting its service token rather than by adding a
task-level service-account string. The first attempted preflight used
`user = "LocalService"` and failed before process launch because Nomad v2.0.5
requires a domain-qualified task user. The corrected, smaller official pattern
omits `task.user`: the LocalService Nomad agent owns `raw_exec` task execution.

`deploy/nomad/live15-control-center-runtime-preflight.nomad.hcl` was validated
with Nomad and ran as allocation
`d8fb0204-f9af-55ed-b32b-ac86f6263669`. It exited `0` with
`LIVE15_CONTROL_CENTER_LOCAL_SERVICE_RUNTIME_PASS`; its final jobspec SHA-256
is `707E79D185C1FECA392499C0F2196FF0FD5E0FF6B1826B2F2A8B85892370FD13`.
The job was then removed using `nomad job stop -purge`.

The repository lock is a manually maintained full resolved closure: CI installs
it before the project with `--no-deps`, and all other resolved dependencies are
explicitly pinned. Runtime metadata shows the supported Intel chain
`intel-openmp==2026.1.1 -> intel-cmplr-lib-ur==2026.1.1 -> umf==1.1.0 ->
tcmlib>=1.5`; the installed terminal package is `tcmlib==1.5.0`. The prior lock
omitted only that production transitive package. The minimal closure correction
adds `tcmlib==1.5.0`; an isolated installation of the corrected lock passed
`pip check`, reported no missing lock packages, no locked-version mismatch, and
no unlocked runtime dependency. The corrected lock SHA-256 is
`4521A9151C00797B004CD6AEB12A054DD5759BD211333D012736CED3E635A67E`.

The existing installed venv intentionally omits development-only pytest/Ruff
packages present in the repository lock; it contains every production runtime
dependency at the locked version. Its installation-time lock receipt remains
the historical pre-correction value
`ADD9987E27D2C3074698097E46EE1C60D7AA6C57A8C0FE4321972717FDC49FA3` and is not
rewritten as new evidence.

## Candidate artifact boundary

Read-only inspection found no `C:\Program Files\LIVE15\ControlCenterReleases`
or other installed application-release root. `C:\Program Files\LIVE15`
contains only `Python313` and `ControlCenterRuntime`.

The existing `D:\LIVE15_NOMAD_POC\control-center-shadow` root is an
Administrator-owned, LocalService-read-only, non-Production POC artifact root;
it contains only its sealed shadow script/jobspec and is not a real ControlCenter
application release. The Nomad client's
`D:\LIVE15_NOMAD_POC\generic-poc\agent-data` grants
`NT AUTHORITY\Authenticated Users:(M)`. Nomad artifact download or an allocation
directory under that root can validate a checksum at download time, but cannot
satisfy the required non-user-writable code-source boundary afterwards.

Accordingly, neither the legacy `D:\LIVE15_QUANT` working tree nor the Nomad
allocation directory may be used as the LocalService application source. This
is why no actual ControlCenter jobspec, service submission, WinSW stop, or port
change was attempted.

## Exact next operator gate

After the corrected lock is reviewed and merged to protected `main`, an operator
must place the `DEP-PKG-001` output for that clean protected SHA under a dedicated
Administrator-owned, non-user-writable, LocalService-readable release root
separate from mutable data, runtime receipts, and logs. The operator action must
return the absolute root plus the release manifest and package-verification
hashes; it must not copy secret contents or change existing ACL/UAC repair
logic. That one installed artifact is the prerequisite for a checksum-bound
Nomad jobspec and the authorized reversible ownership cutover.

Until then:

```text
ACTIVE_CONTROL_CENTER_OWNER = WinSW:LIVE15ControlCenter
CONTROL_CENTER_SERVICE_CHANGE_PERFORMED = NO
CONTROL_CENTER_NOMAD_CUTOVER = BLOCKED
RECORDER_TOUCHED = NO
PRODUCTION_WRITES = 0
SUBTRACTIVE_REPLACEMENT = PASS
```
