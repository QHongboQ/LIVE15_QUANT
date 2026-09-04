# LIVE15 full-system root-cause audit 001

Status: evidence receipt / read-only audit. This is not a current-state authority, deployment
record, or remediation design. Current execution remains owned by
`docs/project-brain/plan/current-roadmap.md`.

## Scope and safety receipt

`AUDIT_MODE = READ_ONLY`

The audit inspected the host, Nomad state, service logs, health projections, runtime identities, and
storage state. It did not restart Recorder or ControlCenter, deploy Nomad jobs, mutate the database,
activate/archive/purge data, change runtime/application/frontend/deployment code, commit, or create a
PR.

```text
PRODUCTION_MUTATION = NONE
RECORDER_RESTART = NO
CONTROLCENTER_RESTART = NO
NOMAD_DEPLOYMENT = NO
DATABASE_MUTATION = NONE
HOT_ROWS_DELETED = 0
ARCHIVE_PURGE = NO
```

## Recorder and runtime observations

The observed Recorder Nomad job was version `14`, using release
`live15-9cc5bd47ba89-3d60b8ac18d3` and the legacy interpreter
`C:\Program Files\LIVE15\ControlCenterRuntime\Scripts\python.exe`. The prepared immutable
revision `runtime-py3.13.15-00D37C7B04E1` existed but was not the active cause of the observed loop.

Recorder task events repeatedly showed roughly 200 seconds of process runtime followed by exit code
`1` and a 16--19 second restart delay: an approximately 217-second restart cycle. The same loop
remained after reverting to the legacy release/runtime. The immutable-runtime preparation was therefore
disproven as the root cause of this restart loop.

## Proven Pyth/Kalshi failure sequence

The audit observed `PythNetworkError` from both the Pyth stream and its REST fallback. The resulting
observed escalation was:

```text
Pyth transport outage
  -> bounded recovery exhausted
  -> PythWorkerUnhealthyError
  -> Recorder exit
  -> Nomad restart
  -> Kalshi resynchronizes
```

In the observed health state, Kalshi recovered all `10/10` synchronized books with sequence gaps `0`
and dropped events `0`, while the Pyth worker failure continued. This distinguishes the global Pyth
transport/critical-worker boundary from a Kalshi synchronization failure.

## ControlCenter host observations

- The main `live15-control-center` Nomad job was absent or otherwise not proven running.
- The legacy `LIVE15ControlCenter` WinSW service was stopped; its start path failed with
  `ModuleNotFoundError` for `live15_quant.control_center`.
- The desktop launcher targeted the legacy WinSW path and `127.0.0.1:8765`; no listener was available
  on `8765`.
- Instances answered on `8766` and `8767`, but their lifecycle owner and deployed release identity
  were not proven by this audit.

## Archive and storage observations

The SQLite database was approximately `61.3 GB`; its WAL was approximately `4.4 MB`; the D: volume
had approximately `225 GB` free. Archive processing was disabled and no active Parquet archive or
manifest was observed. This is a capacity/safety observation, not a restart-loop root cause and not
authorization to activate archive processing or purge any hot rows.

## Unresolved questions

- The correct Pyth critical-worker failure boundary remains the subject of
  `RECORDER-PYTH-CRITICALITY-RECOVERY`; this receipt does not select a mechanism.
- The actual lifecycle owner and deployed release identity of the `8766`/`8767` ControlCenter-like
  instances remain unproven.
- ControlCenter host ownership, desktop launch, listener, release identity, and host acceptance remain
  recovery-gated.
