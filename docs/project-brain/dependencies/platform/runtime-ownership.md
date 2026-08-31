# Runtime ownership

Revision: R4
Status: machine-readable authority retained.

## What it is

Maps process/service ownership, health truth, and restart authority.

## Current truth

`deploy/windows/runtime-ownership.json` remains the machine-readable authority. ControlCenter and
Recorder are Nomad-managed; RuntimeSupervisor retains its separate WinSW-owned boundary for its remaining children. The verified
`kalshi_sdk_ws_shadow` lifecycle is also Nomad-managed. Recorder domain truth remains owned by
the Recorder authority leaf, not by Nomad.

## Canonical Production Python runtime

`CANONICAL_LIVE15_PRODUCTION_RUNTIME` is established as LIVE15's default
shared protected Production Python runtime. It is not owned by ControlCenter,
Recorder, or any predefined workload list. The verified physical identities are:

- base interpreter: `C:\Program Files\LIVE15\Python313\python.exe`;
- production interpreter: `C:\Program Files\LIVE15\ControlCenterRuntime\Scripts\python.exe`;
- CPython: `3.13.15`;
- runtime `python.exe` SHA-256:
  `72B29481593C5DA37C99248C82777FBFB56217EA7809B771BC760D0A9ECB179B`.

`ControlCenterRuntime` is historical directory naming, not a ControlCenter-only
scope. The runtime is separate from each immutable application release and from
mutable data, logs, WAL/SHM, health/control/PID state, archives, retention state,
and external credentials. Existing verification is recorded in
`docs/deployment/NOMAD_CONTROL_CENTER_CUTOVER_FINAL_001.md`.

The Project Brain authority is the runtime registry: it records the logical
identity, physical path, base interpreter, Python version, runtime hash/identity,
dependency environment identity, verification evidence, status, and exception
rule while the runtime files remain in their protected host location. No runtime
manager service, runtime database, package server, or selection framework is
introduced.

Whenever any present or future LIVE15 component, service, worker, tool, daemon,
or other Python workload needs a Production Python runtime, it must first
resolve this authority and use the canonical runtime as its runtime source. A
separate runtime may be proposed only after concrete technical evidence shows
that a mandatory interpreter or dependency requirement is incompatible. A
different component/job/service name, application release, directory name, or
`legacy-unproven-*` application provenance is not evidence of incompatibility;
stop and report if incompatibility is found rather than provisioning another
runtime automatically.

The canonical runtime remains separate from each clean-SHA immutable application
release, mutable databases/health/PID/archive/retention state, and external
credential material. `legacy-unproven-*` describes application-release
provenance and does not invalidate this runtime authority.

## Interfaces / dependencies

`docs/runtime_ownership_and_self_healing.md`; `deploy/windows/runtime-ownership.json`.

## Read next

Use `../../capabilities/control-center.md` or `../../capabilities/records/recorder/truth.md` for component context.

## Update rule

Update only when ownership topology changes; change the JSON authority first when applicable.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 authority baseline, moved without semantic change. |
| R2 | CANONICAL-PRODUCTION-RUNTIME-AUTHORITY-001 | Established the universal shared Production Python runtime authority and exception rule. |
| R3 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 | Reconciled verified Recorder Nomad lifecycle ownership in the machine-readable registry. |
| R4 | RUNTIME-KALSHI-SDK-SHADOW-NOMAD-001 | Reconciled verified Nomad ownership of the shadow and retired its Supervisor launch path. |
