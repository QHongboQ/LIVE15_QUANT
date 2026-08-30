# LIVE15 runtime ownership and self-healing

This document freezes one operational rule:

> ONE COMPONENT · ONE OWNER · ONE HEALTH TRUTH · ONE RECOVERY AUTHORITY

The machine-readable counterpart is [`deploy/windows/runtime-ownership.json`](../deploy/windows/runtime-ownership.json).

## Owners

| Component | Owner | Health truth | Recovery authority |
| --- | --- | --- | --- |
| LIVE15Recorder | WinSW service | Windows service state plus current `data/health.json` | `LIVE15Recorder` WinSW policy |
| LIVE15ControlCenter | Nomad allocation | Nomad allocation state plus native `/api/health` check | Nomad restart/update/auto-revert |
| LIVE15RuntimeSupervisor | WinSW service | Windows service state plus current supervisor receipt | `LIVE15RuntimeSupervisor` WinSW policy |
| current_trainable | RuntimeSupervisor when explicitly enabled | current supervisor receipt plus child PID/heartbeat | RuntimeSupervisor bounded child restart |
| paper_forward | RuntimeSupervisor when explicitly enabled | current supervisor receipt plus child PID/heartbeat | RuntimeSupervisor bounded child restart |
| kalshi_sdk_ws_shadow | RuntimeSupervisor when explicitly enabled | current supervisor receipt plus child PID/heartbeat | RuntimeSupervisor bounded child restart |
| Pyth | Recorder in-process worker | Recorder alive plus accepted Pyth observation progress and market-session state | worker reconnect/fallback, then Recorder fatal exit, then WinSW |
| Coinbase | Recorder in-process worker | Recorder alive plus accepted Coinbase observation progress | worker reconnect, then Recorder fatal policy if classified critical |

The Supervisor never starts, stops, or restarts Recorder or Control Center. It does not infer a
service failure from an old supervisor receipt. A current Windows service with an old receipt is
reported as `STALE_TELEMETRY`; the service itself remains the process authority.

## Service packaging

`LIVE15Recorder` and `LIVE15RuntimeSupervisor` retain their WinSW definitions. The stopped
`LIVE15ControlCenter` WinSW definition is retained solely as the verified rollback owner while its
Nomad allocation owns lifecycle, restart, health, update, and native revert.

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
raises `PythWorkerUnhealthyError`. The Recorder exits and WinSW applies its bounded restart policy.

Gold, Silver, and WTI use the existing market-session calendar. A legitimate closed session is not
treated as a stale critical source. BNB and HYPE retain their independent crypto availability
semantics.

## Auxiliary worker modes

The registry marks `current_trainable` and `kalshi_sdk_ws_shadow` `ON_DEMAND` and
`paper_forward` `PAUSED_BY_DESIGN` in this branch. They are not rendered as stale failures merely
because they are intentionally not running. A future explicit registration can promote an
auxiliary worker to `ALWAYS_ON`; only RuntimeSupervisor may then launch it.

## Migration / deployment boundary

The verified ControlCenter cutover is recorded in
`docs/deployment/NOMAD_CONTROL_CENTER_CUTOVER_FINAL_001.md`. Recorder and RuntimeSupervisor
remain independently WinSW-owned; no Production execution authority changes with this ownership map.
