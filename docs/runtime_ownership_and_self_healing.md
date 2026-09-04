# LIVE15 runtime ownership and self-healing

This document freezes one operational rule:

> ONE COMPONENT · ONE OWNER · ONE HEALTH TRUTH · ONE RECOVERY AUTHORITY

The machine-readable counterpart is [`deploy/windows/runtime-ownership.json`](../deploy/windows/runtime-ownership.json).

## Owners

Current component owner, process source, health truth, and restart authority values are owned only
by `deploy/windows/runtime-ownership.json`; this narrative does not duplicate that registry.
Recorder and `kalshi_sdk_ws_shadow` resolve to Nomad lifecycle ownership. The ControlCenter registry
entry is the intended Nomad ownership model, not proof of current host operation: current host
ownership, desktop launch, listener, and deployed identity are **BLOCKED / RECOVERY REQUIRED** in the
ControlCenter authority. RuntimeSupervisor is retired and old supervisor receipts are stale telemetry,
never current process authority. In-process workers escalate only through their registered parent/
restart authority.

## Service packaging

Nomad owns lifecycle for Recorder and `kalshi_sdk_ws_shadow`. The historical ControlCenter Nomad
cutover is retained implementation evidence, but its current host lifecycle owner is not reconciled.
Shared WinSW bootstrap metadata may remain for bounded retained artifacts, but it does not create a
current process or restart owner.

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
process. `kalshi_sdk_ws_shadow` is Nomad-managed.

## Migration / deployment boundary

Recorder Nomad ownership and historical ControlCenter cutover are represented in the machine-readable
registry and bounded deployment evidence. The latter is not current ControlCenter host acceptance.
Runtime ownership changes do not transfer Recorder truth, Production execution, Hard Risk, or trading
authorization to the scheduler.
