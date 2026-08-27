# LIVE15 UI runtime link audit 001

The audit covered read-only runtime/link verification and narrow UI truth repairs. Recorder,
database, archive state, storage maintenance, and Production writes were not touched.

## Runtime evidence

| Boundary | Evidence |
| --- | --- |
| Project checkout | Inspected read-only; unrelated working-tree changes were preserved. |
| Services | `LIVE15ControlCenter` and `LIVE15Recorder` were `Running/Automatic`. |
| Control Center | WinSW service wrapper; direct project `.venv\Scripts\python.exe -m live15_quant.control_center` in the installed XML. |
| HTTP | `/`, `/api/health`, `/api/system`, `/api/markets`, `/api/account`, `/api/archive`, `/api/storage` returned HTTP 200 |
| Account | Unavailable credentials are represented as typed `N/A`; the UI does not fabricate a balance. |
| Browser | Overview, Archive, and System rendered without console errors. |

## Findings and repairs

1. **Refresh staleness (high):** `web/app.js` retained the previous payload after a failed
   request and explicitly told the user that last-valid values were retained. Failed projections
   now clear and render unavailable/unknown until a fresh response arrives; the notice no longer
   presents retained data as current truth.
2. **Dashboard route (medium):** `#/dashboard` was present in navigation and renderer dispatch,
   but omitted from route parsing, so it silently rendered Overview. The route is now recognized.
3. **Stale runtime receipt (medium):** Control Center projected historical
   `runtime-supervisor-status.json` rows as `RUNNING` even when their heartbeat was stale. The
   service now marks stale rows `STALE`, removes the PID from the live projection, and reports
   `process_alive=false` until a fresh receipt exists.
4. **Synchronization semantics (low):** System view now labels the actual
   `kalshi_ws_synchronized_count` as “WS synchronized assets” and exposes `kalshi_ws_seq_gaps`
   without changing backend metric semantics.

## Desktop launcher and maintenance audit

The desktop shortcut targets the project-local `.local-tools\Start LIVE15.cmd` and opens
`http://127.0.0.1:8765/#/overview`. The corrected launcher uses only `sc.exe` for
`LIVE15ControlCenter`, validates `/api/system`, and opens the browser only after identity and
localhost binding are confirmed. It no longer falls back to the superseded runtime supervisor or
legacy managed Control Center process; missing service or failed start is reported visibly.

The reproduced failure was in the previous local launcher: it resolved `.venv` relative to its
`.local-tools` working directory, so it exited before service startup and browser launch even
though the project environment was present at the checkout root. The replacement resolves the
service through Windows SCM and keeps the browser step behind bounded HTTP readiness checks.

No matching LIVE15 Scheduled Task was present in the read-only inventory. WinSW services are the
active automatic maintenance boundary. The stale supervisor receipt demonstrates why old
supervisor automation did not prevent the UI truth issue: it was not an authoritative current
service-health source and had no UI/API data-lineage validation.

## Safety result

Recorder runtime was not changed, no database/archive/maintenance operation ran, and Production
writes remained zero. The `tools/ui_runtime_truth_self_test.py` utility is detect-only;
it emits `PASS`, `WARN`, or `FAIL` JSON and never invokes SCM or mutating endpoints.
