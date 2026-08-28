# Control Center UI data lineage

This is the reviewed read-only contract for the localhost Control Center. UI code renders
typed responses from the listed endpoint; it does not define runtime truth. A failed refresh
clears that projection and renders an unavailable/unknown state until a new response arrives.

| UI element | API endpoint | Backend source | Authoritative source | Refresh | Status |
| --- | --- | --- | --- | --- | --- |
| Recorder state, heartbeat, written records, fatal fields | `/api/health` | `ControlCenterService.health()` | Recorder heartbeat JSON and process/lifecycle projection | 2.5 s | LIVE |
| WS synchronized asset count and sequence gaps | `/api/health` | `ControlCenterService.health()` | Recorder WS health fields (`kalshi_ws_synchronized_count`, `kalshi_ws_seq_gaps`) | 2.5 s | LIVE |
| Current markets, ticker, quote, lifecycle | `/api/markets` | `ControlCenterService.markets()` / `ControlCenterStore` | Recorder store + current provider projections | 2.5 s | LIVE |
| Market detail and orderbook | `/api/markets/{asset}` | `ControlCenterService.market()` / `ControlCenterStore` | Recorder store and synchronized quote evidence | 2.5 s | LIVE |
| Dashboard warning/error/fatal totals | `/api/events/summary` | `ControlCenterService.event_summary()` | Indexed recorder-event aggregate over its explicit 24-hour window; the bounded event sample is never treated as a total | 15 s | LIVE |
| Account balance, portfolio, positions | `/api/account` | `ProductionAccountService.read()` | Kalshi Production read API | 10 s | LIVE |
| Orders/fills | `/api/account/orders`, `/api/account/fills` | `ProductionAccountService.read()` | Kalshi Production read API | 10 s | LIVE |
| Archive verified/failed/waiting/quarantine/backlog | `/api/archive` | `ControlCenterService.archive()` | Recorder heartbeat `ws_archive` projection | 10 s | LIVE |
| Adaptive archive mode/cadence | `/api/archive` | `ControlCenterService.archive()` | Recorder archive worker metrics | 10 s | LIVE |
| Storage DB/WAL/free-space/reuse | `/api/storage` | `ControlCenterService.storage()` | Filesystem and SQLite runtime metrics | 30 s | LIVE |
| Data pipeline projection | `/api/data` | `ControlCenterService.data()` | Recorder store and persisted materializer evidence | 30 s | LIVE |
| Training truth/current projection | `/api/training` | `ControlCenterService.training()` | Finalized settlement store and immutable dataset/materializer records | 30 s | LIVE |
| Runtime/service identity | `/api/system` | `ControlCenterService.system()` | Local runtime status and service configuration | 30 s | LIVE |
| Operations/retries/recent events | `/api/operations`, `/api/events` | `ControlCenterService.operations()` / `events()` | Recorder heartbeat and bounded event store | 10–15 s | LIVE |

## Fail-closed refresh contract

`web/app.js` uses `cache: "no-store"` and bounded request timeouts. When an HTTP, JSON, or
timeout failure occurs, the failed projection is cleared rather than retained as current truth.
The page shows an unavailable/unknown state and a visible refresh notice. Other independently
successful projections may remain visible because their endpoint has its own fresh response.

## Explicit non-truth UI

Orders, History, Watchlist, Analytics, Signals, and Models are currently read-only shells or
bounded evidence views. They must not be interpreted as live trading controls, generated signals,
or model promotion state. Missing account values are rendered `N/A`; missing market/storage/
archive values are rendered `—`/`N/A`, never fabricated zeroes.
