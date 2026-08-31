# LIVE15 runtime ownership and self-healing

This document freezes one operational rule:

> ONE COMPONENT · ONE OWNER · ONE HEALTH TRUTH · ONE RECOVERY AUTHORITY

The machine-readable counterpart is [`deploy/windows/runtime-ownership.json`](../deploy/windows/runtime-ownership.json).

## Owners

Current component owner, process source, health truth, and restart authority values are owned only
by `deploy/windows/runtime-ownership.json`; this narrative does not duplicate that registry.
Recorder and ControlCenter currently resolve to Nomad lifecycle ownership, while
RuntimeSupervisor remains only as a zero-responsibility legacy WinSW boundary pending final host
retirement. In-process workers escalate only through their registered parent/restart authority.

The Supervisor never starts, stops, or restarts Recorder or Control Center. It does not infer a
service failure from an old supervisor receipt. A current owner with an old receipt is reported as
`STALE_TELEMETRY`; stale telemetry never overrides the registered process authority.

## Service packaging

Nomad owns lifecycle for Recorder and ControlCenter. RuntimeSupervisor retains its WinSW
definition. Stopped legacy WinSW definitions may remain bounded rollback artifacts; they are not
current process/restart owners and must not evolve in parallel with Nomad.

## Worker recovery

Workers expose current progress metadata in `data/health.json` under `worker_health`:

- `last_progress_timestamp`
- `last_successful_observation_timestamp`
- `consecutive_failures`
- `current_state`
- `last_error_type`
- `next_retry_at`

Pyth is a critical source when enabled. One recovery cycle recreates the SSE client, attempts the
existing bounded REST fallback, and then backs off. Only accepted observations advance Pyth worker
progress; failed retries do not impersonate useful data. If both the attempt budget and outage
timeout are exhausted while an enabled Pyth-backed market is expected to produce data, Recorder
raises `PythWorkerUnhealthyError`. The Recorder exits and its registered Nomad restart policy
applies.

Gold, Silver, and WTI use the existing market-session calendar. A legitimate closed session is not
treated as a stale critical source. BNB and HYPE retain their independent crypto availability
semantics.

## Auxiliary worker modes

`current_trainable` is a mutable checkpointed materializer/training projection, not a runtime
process. `kalshi_sdk_ws_shadow` is Nomad-managed. No auxiliary worker is currently registered
under RuntimeSupervisor; the legacy service remains represented only for bounded host retirement.

## Migration / deployment boundary

Verified ControlCenter and Recorder Nomad ownership is represented in the machine-readable registry
and bounded deployment evidence. Runtime ownership changes do not transfer Recorder truth,
Production execution, Hard Risk, or trading authorization to the scheduler.
