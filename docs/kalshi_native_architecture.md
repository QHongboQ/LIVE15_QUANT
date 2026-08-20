# Kalshi-native lifecycle and settlement architecture

Verified against the official API documentation and public production REST responses on
2026-08-20. This implementation performs no account or order operation.

## Core source roles

1. `GET /markets` with one exact audited `series_ticker` discovers live and adjacent markets.
2. `GET /markets/{ticker}` and `GET /markets/{ticker}/orderbook` provide official venue quote
   and depth. The two REST responses are not atomic.
3. A market becomes an immutable training label only when REST reports `status=finalized`,
   `result=yes|no`, and a valid `settlement_ts`.
4. `/historical/markets` supplies archived markets through cursor pagination. Recent and archived
   results are merged by exact ticker and stored idempotently.
5. Coinbase is a predictive source only. Robinhood is an optional reference only. Neither can
   create or amend a Kalshi settlement label.

Official references:

- [Get Markets](https://docs.kalshi.com/api-reference/market/get-markets)
- [Market lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle)
- [Historical markets](https://docs.kalshi.com/api-reference/historical/get-historical-markets)
- [Historical market by ticker](https://docs.kalshi.com/api-reference/historical/get-historical-market)
- [Get Series](https://docs.kalshi.com/api-reference/market/get-series)
- [WebSocket quick start](https://docs.kalshi.com/getting_started/quick_start_websockets)
- [Orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates)

## Exact discovery

The asset-to-series map is a fixed enum-backed mapping for the ten approved series. Each accepted
candidate must have matching series/event/market ticker prefixes, a UTC quarter-hour aligned
15-minute `open_time`/`close_time`, a positive finite target from its own `yes_sub_title` (or its
own `floor_strike` fallback), and a unique ticker/window. Titles are never matched. A future market
whose official target is still `TBD` is rejected until Kalshi publishes that market's own target;
the current target is never copied forward.

## Lifecycle and immutable truth

Official statuses normalize as follows: initialized→UPCOMING, active→OPEN, inactive→PAUSED,
closed→CLOSED, determined/disputed/amended→SETTLEMENT_PENDING, and finalized+yes/no→
SETTLED_YES/SETTLED_NO. When polling
skips an intermediate exchange state, the deterministic replay state machine inserts CLOSED then
SETTLEMENT_PENDING before the official terminal result. It never infers a result from time or an
underlying price.

`kalshi_settlements` has one immutable row per ticker. An identical observation is idempotent. A
different later result, timestamp, target, or value is written to `kalshi_settlement_conflicts` and
raises loudly; it cannot overwrite truth.

## Event-driven live acceptance

The acceptance runner never waits for a configured calendar date or UTC opening time. On every
run it queries all ten exact series, selects the real OPEN market nearest to its official close
while preserving a minimum observation interval, and then follows only that asset. A successor
counts as a rollover only when its `window_start` exactly equals the baseline `window_end`.
Schedule gaps and exchange maintenance trigger bounded rediscovery; they are never represented as
synthetic markets or results. The runner verifies official OPEN→CLOSED→SETTLEMENT_PENDING→terminal
replay, successor quotes, immutable official settlement, SQLite integrity, and restart counts.

The default and absolute wall-clock limit is 30 minutes. Acceptance disables nested transport
retries, caps every GET timeout by the remaining global budget, and applies its own bounded capped
backoff. If the exchange does not expose a usable current market, adjacent
successor, successor quote, or finalized result before the deadline, the result is structured as
`expected_upstream_unavailable`. Malformed payloads, ticker/window mismatches, invalid lifecycle
transitions, settlement conflicts, and storage errors are correctness failures and propagate
immediately. An optional database path permits safe restart/repetition; the default is an isolated
temporary database.

`rollover_latency_seconds` is discovery latency: the local receive time of the first accepted
successor metadata observation minus that successor's official `window_start`. It includes polling
phase, REST time, and any delay before the exchange exposes a valid target. It is not settlement,
quote, order, fill, or execution latency. Settlement timing is recorded separately from the
official `settlement_ts` field.

## Backfill and leakage control

`KalshiBackfillService` commits market/result rows and a cursor after every page. Restart resumes
at that page boundary; API ordering is ignored and records replay by `(window_start,ticker,id)`.
Although the documentation lists `mve_filter=exclude`, the 2026-08-20 production historical
endpoint returned HTTP 400 when it was combined with `series_ticker`; the exact non-MVE 15-minute
series filter is already sufficient, so the historical request deliberately omits that parameter.
`join_training_label(ticker, decision_timestamp)` admits only metadata fetched and quotes received
at or before the decision timestamp. Settlement fields exist only in the returned `label` object,
never in the metadata or quote feature records. A decision at/after settlement is rejected.

## WebSocket decision

Kalshi documents `ticker`, `trade`, and `market_lifecycle_v2`, plus snapshot/delta orderbook
messages with `seq`. However every Production WebSocket handshake requires a Production API key,
including public market-data channels; orderbook also requires authenticated subscription. Demo
credentials are environment-separated and cannot authenticate Production. Milestone 6 therefore
keeps official REST in production runtime and documents a future read-only authenticated adapter
with snapshot→monotonic delta sequence, gap detection, reconnect/resubscribe, and snapshot recovery.
`kalshi_ws.py` defines that adapter Protocol, typed snapshot/delta envelopes, and a fail-closed
per-subscription sequence guard. It deliberately contains no socket connection, signer, credential,
or order method. No Production credential or write capability was added.
