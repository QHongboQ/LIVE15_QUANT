# LIVE15 guarded regression index

This is a compact index. Executable tests and evidence documents remain the
source of detail.

| Guarded regression | Durable fact / status | Guard / evidence | Related decision |
| --- | --- | --- | --- |
| Pyth false worker-progress / stale masking | Failed or duplicate input must not advance meaningful worker progress. **GUARDED**; the proven masking behavior is fixed. | `tests/test_native_recorder.py`, `tests/test_runtime_ownership.py` | `docs/adr/0003-runtime-ownership.md` |
| Pyth recurring generic `PythNetworkError` | Symptom: Recorder cannot complete clean runtime-health proof while the Pyth worker is unhealthy. Root cause: **UNRESOLVED_ACTIVE**. | Current runtime-closeout evidence; diagnosis follows `docs/agents/debug-routing.md` | `CURRENT_STATE.md` |
| RuntimeSupervisor service ACL delegation | Symptom: target Codex ACE service-control delegation was reported but not persisted. Root cause: **UNRESOLVED_ACTIVE**. | Runtime-closeout evidence; no false-positive completion claim | `docs/adr/0003-runtime-ownership.md` |
| Replay-baseline gap | Historical delta prefixes without a usable baseline isolate or wait; they are never replay-verified by invention. **GUARDED**. | `tests/test_ws_archive.py`, `tests/test_ws_retention.py` | `docs/adr/0002-research-data-authority.md` |
| Archive quarantine | Quarantined rows remain preserved and non-purgeable; FAILED and WAITING remain fail-closed. **GUARDED**. | `tests/test_ws_retention.py` | `docs/continuous_recorder.md` |
| Stale UI/runtime truth | A stale receipt must not override current service health; UI exposes stale telemetry rather than a false failure. **GUARDED**. | `tests/test_runtime_ownership.py`, Control Center runtime tests | `docs/adr/0003-runtime-ownership.md` |
| Dataset-history confusion | Dataset partitions do not define current research coverage or authorize holdout access. **GUARDED**. | `tests/test_research_data_authority.py` | `docs/adr/0002-research-data-authority.md` |

If no regression test is possible, record the reason beside the smallest
reproducible evidence artifact; do not substitute prose for an executable test.
