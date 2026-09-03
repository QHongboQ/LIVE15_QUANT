# Runtime ownership

Revision: R7
Status: machine-readable authority retained.

## What it is

Maps process/service ownership, health truth, and restart authority.

## Current truth

`deploy/windows/runtime-ownership.json` remains the machine-readable authority. ControlCenter,
Recorder, and the verified `kalshi_sdk_ws_shadow` lifecycle are Nomad-managed. RuntimeSupervisor
is fully retired and has no current registry entry. Recorder domain truth remains owned by the
Recorder authority leaf, not by Nomad.

`current_trainable` is a mutable checkpointed materializer/training projection owned through the
model/data authorities; it is not a RuntimeSupervisor process and has no runtime-process registry
entry.

## Canonical Production Python runtime

`CANONICAL_LIVE15_PRODUCTION_RUNTIME` is LIVE15's default shared protected Production Python
runtime authority. It is logical authority over approved immutable runtime revisions; it is not
owned by ControlCenter, Recorder, or any predefined workload list.

The verified legacy Production revision remains in service until a reviewed Nomad rollout replaces
it:

- base interpreter: `C:\Program Files\LIVE15\Python313\python.exe`;
- legacy/current interpreter: `C:\Program Files\LIVE15\ControlCenterRuntime\Scripts\python.exe`;
- CPython: `3.13.15`;
- legacy/current `python.exe` SHA-256:
  `72B29481593C5DA37C99248C82777FBFB56217EA7809B771BC760D0A9ECB179B`.

`ControlCenterRuntime` is historical directory naming, not a ControlCenter-only scope. It is now
classified as the retained legacy runtime revision, not the target location for future runtime
promotion.

Future Production runtime revisions are created directly at their final immutable path beneath:

`C:\Program Files\LIVE15\CanonicalRuntimeRevisions\runtime-py<version>-<production-lock-sha>\`

A Python virtual environment is never promoted by moving or copying it. A prepared revision remains
inactive merely because it exists. Workloads select an approved revision explicitly through their
Nomad job configuration; Nomad owns rolling deployment, stopped-job start, deployment auto-revert,
and job-version revert. During a bounded rollout, the old and new immutable revisions may coexist
on disk while different allocations transition, without creating a second runtime manager.

Each prepared revision carries `live15-runtime-manifest.json` binding its final runtime root,
interpreter path/version/SHA-256, Production-lock SHA-256, and exact dependency identity. The
runtime contains shared Production dependencies only; application source remains in immutable
application releases and is not installed into the dependency runtime.

The canonical runtime remains separate from each immutable application release and from mutable
data, logs, WAL/SHM, health/control/PID state, archives, retention state, and external credentials.
Existing legacy verification is recorded in `docs/deployment/NOMAD_CONTROL_CENTER_CUTOVER_FINAL_001.md`.

Whenever any present or future LIVE15 component, service, worker, tool, daemon, or other Python
workload needs a Production Python runtime, it must first resolve this authority and use an approved
canonical runtime revision. A separate runtime may be proposed only after concrete technical
evidence shows that a mandatory interpreter or dependency requirement is incompatible. A different
component/job/service name, application release, directory name, or `legacy-unproven-*` application
provenance is not evidence of incompatibility.

Production dependency additions use `requirements.production.lock` as an exact, fully closed
transitive dependency inventory for the Windows Production interpreter. The immutable runtime
preparer validates the lock against pip's resolver, installs it in the final revision path, verifies
that the installed non-tooling inventory equals the lock exactly, runs `pip check`, and verifies
LIVE15 Production imports through repository/release source. `pytest`, `pytest-asyncio`, and `ruff`
remain development-only; a package such as `httpx` is not classified development-only when a
Production dependency requires it transitively.

Runtime preparation is non-disruptive and does not require stopping Recorder or ControlCenter.
Changing a running workload to a new prepared revision is a Nomad deployment concern. A stopped job
with no configuration change is resumed with Nomad `job start`; rollback of a submitted job version
uses Nomad job history/revert rather than a LIVE15 runtime rollback controller.

No runtime manager service, runtime database, package server, symlink switcher, movable-venv
promotion mechanism, or custom restart/rollback state machine is introduced.

## Interfaces / dependencies

`docs/runtime_ownership_and_self_healing.md`; `deploy/windows/runtime-ownership.json`;
`tools/prepare_live15_production_runtime.ps1`;
`tools/deploy_live15_recorder_nomad.ps1`.

## Read next

Use `../../capabilities/control-center.md` or `../../capabilities/records/recorder/truth.md` for component context.

## Update rule

Update only when ownership topology or canonical runtime policy changes; change the JSON authority
first when machine-readable ownership changes.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 authority baseline, moved without semantic change. |
| R2 | CANONICAL-PRODUCTION-RUNTIME-AUTHORITY-001 | Established the universal shared Production Python runtime authority and exception rule. |
| R3 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 | Reconciled verified Recorder Nomad lifecycle ownership in the machine-readable registry. |
| R4 | RUNTIME-KALSHI-SDK-SHADOW-NOMAD-001 | Reconciled verified Nomad ownership of the shadow and retired its Supervisor launch path. |
| R5 | RUNTIME-PAPER-FORWARD-WRAPPER-RETIREMENT-001 | Retired the paper wrapper and removed stale Supervisor child ownership; RuntimeSupervisor is now a zero-responsibility legacy boundary. |
| R6 | RUNTIME-SUPERVISOR-FINAL-REPOSITORY-CLEANUP-001 | Retired the final host service and removed RuntimeSupervisor from current repository ownership. |
| R7 | RUNTIME-DEPLOY-SIMPLIFICATION-001 | Replaced movable-venv promotion and custom lifecycle recovery with immutable runtime revisions plus native Nomad deployment/start/revert ownership; required exact Production dependency closure. |
