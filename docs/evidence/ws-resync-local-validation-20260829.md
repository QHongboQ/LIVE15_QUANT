# WS-RESYNC-001 local validation — 2026-08-29

Classification: `LOCAL_VALIDATION_PASS / RUNTIME_GATE_PENDING`.

## Scope

This evidence covers only the then-current-main (`4d088930`) local regression
suites for the typed Kalshi WebSocket recovery boundary. It does not authorize a Production
deployment, service restart, Recorder write, H2 promotion, or live runtime
recovery claim.

## Reproducible result

- source revision: `4d088930cc83634faf807188fba386f7a7a34bea`;
- command (from the isolated clean worktree, with `src` on `PYTHONPATH`):
  `D:\LIVE15_QUANT\.venv\Scripts\python.exe -m pytest tests/test_kalshi_ws.py tests/test_kalshi_ws_recorder.py tests/test_kalshi_sdk_ws_shadow.py -q`;
- completed: `2026-08-29T12:15:46.9609285Z`;
- result: **72 passed in 5.35s**;
- `git diff --check`: PASS.

The suites cover documented `get_snapshot` recovery, bounded retry/
resubscribe/reconnect escalation, atomic multi-market synchronization, typed
Recorder gap closure, and SDK-shadow boundaries. The protocol facts and
upstream links remain in `docs/reliability/ws-resync-001-protocol-audit.md`.

## Remaining gate

Local tests do not bind the implementation to a running protected-main
deployment and do not prove the bounded real H2 revalidation. A separate
runtime/deployment gate remains required before any live proof; until then,
unsynchronized or unproven runtime state remains fail-closed.
