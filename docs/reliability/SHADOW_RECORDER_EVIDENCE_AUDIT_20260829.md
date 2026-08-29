# SHADOW-REC-001 evidence revalidation — 2026-08-29

## Scope and authority

This is a bounded, read-only audit of existing SDK shadow receipts in the
protected checkout. It does not restart, stop, clean up, attach to, or otherwise
modify a shadow process or Production service. The SDK shadow is not the
authoritative Recorder and has no write authority.

## Receipt observations

| Receipt | Reported state | Freshness/identity observation |
| --- | --- | --- |
| `runtime/kalshi-sdk-ws-shadow-status.json` | `status=RUNNING`, `process_alive=true`, `pid=17356`, `connected_status=CONNECTED`, `parity_status=MEASURING`, `synchronized_count=0`, `recent_gap_count=53736`, `recent_mismatch_count=379971`, `official_recorder_writes=false` | Last heartbeat `2026-08-26 02:43:53`; bounded native process query found PID `17356` absent |
| `runtime/sdk-reliability-shadow-status.json` | `status=RUNNING`, `process_alive=true`, `pid=11352`, `connected_status=connected`, `synchronized_count=10`, `official_recorder_writes=false` | Last heartbeat `2026-08-24 20:01:53`; bounded native process query found PID `11352` absent |
| `runtime/sdk-production-host-smoke-result.json` | `status=PASSED`, `synchronized_count=10`, `unrecovered_gap_count=0`, `duration_seconds=125.05` | Historical smoke receipt; it does not prove a current process or current shadow health |

## Result

`EVIDENCE_REVALIDATION_REQUIRED`.

The `RUNNING` and `process_alive` fields in the two shadow receipts are not
current-state proof because neither reported PID exists in the bounded native
process query and both heartbeats are stale. The first receipt additionally
records no synchronized assets and substantial gap/mismatch counts. Do not use
these receipts to authorize a Recorder replacement, infer current health, or
operate Production. Fresh revalidation, if authorized later, must bind a live
PID generation, executable identity, start time, current heartbeat, and bounded
parity/gap evidence in a separate non-Production task.

No process, service, file, credential, holdout, or Production state was changed by
this audit. Hosted CI remains `CI_DEFERRED_QUOTA`.
