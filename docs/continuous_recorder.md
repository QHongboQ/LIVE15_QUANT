# Continuous training-data recorder

`live15-record` is the independent raw source-of-truth process for all ten exact Kalshi
15-minute series. It needs no Robinhood data and exposes no order-write method. It discovers each
asset independently, records official REST market/orderbook observations, streams supported
Coinbase underlying products, follows every ended predecessor until Kalshi publishes finalized
truth, and atomically updates `data/health.json`.

## Operation on Windows

Run in the foreground for normal operation:

```powershell
.\.venv\Scripts\Activate.ps1
live15-record
```

Ctrl+C performs graceful task cancellation, closes HTTP sessions and SQLite, and writes a final
health heartbeat. For an unattended console or an already managed process host, use the bounded
restart wrapper:

```powershell
powershell -ExecutionPolicy RemoteSigned -File scripts\run_recorder_forever.ps1
```

The helper restarts only after a non-zero crash and exits after a clean stop. Restart delay grows
exponentially from 5 to 60 seconds, and more than five consecutive short crashes trips a circuit
breaker instead of hiding the fault or entering a rapid loop. A run lasting five minutes resets the
counter. It does not create a Windows service, startup item, or Scheduled Task. Those system
changes require separate approval.

`live15-status` prints the last atomic JSON heartbeat. `live15-coverage` creates/resumes one
snapshot-consistent feature build and prints finalized/trainable event counts, rows, per-asset
coverage, YES/NO balance, decision buckets, and missing/stale rates. Raw collection and feature
building use separate SQLite connections and databases. Periodic builds are disabled by default;
set `LIVE15_DATASET_BUILD_INTERVAL_SECONDS` only when desired. A build failure is logged and does
not stop raw collection.

## Lifecycle, settlement and recovery

Discovery uses the ten exact `KALSHI_15MIN_SERIES` identifiers and UTC contract windows. There is
no title matching, fixed ticker, fixed date, or fixed real-world wait. Each source operation has a
bounded timeout and the next poll is interruptible. Network failures are tracked per asset/source,
while malformed payloads, ticker/metadata mismatch, invalid lifecycle transitions, settlement
conflicts, and storage errors fail loudly.

At rollover the successor becomes the quote target. Ended markets remain in the append-only
lifecycle table and are selected in bounded keyset batches until official status is `finalized`.
The live `GET /markets/{ticker}` path is tried first; a documented 404 falls back to
`GET /historical/markets/{ticker}`. `determined`, `disputed`, and `amended` remain
`SETTLEMENT_PENDING`; only the official finalized `result` (`yes` or `no`) enters
`kalshi_settlements`. Existing truth is immutable and a conflicting result creates a diagnostic
then raises an error.

Startup reconstructs current OPEN markets, latest quote/tick receive cursors, all latest lifecycle
states, unresolved predecessors, and last finalized settlements from SQLite. Exact repeated facts
are idempotent; changed quote/orderbook state is retained. Follow-up batches and active market maps
are bounded in memory. SQLite remains in WAL mode with passive periodic checkpoints, integrity
checks in health, and append-only raw observations.

## REST and WebSocket boundary

The production recorder uses the documented credentialless Kalshi REST market endpoints. Kalshi's
official WebSocket sends ticker/trade/lifecycle and snapshot-first orderbook deltas, but the
connection handshake requires an API key even for public-data channels. The project therefore
keeps its typed snapshot/delta sequence guard as the future adapter boundary and does not load Demo
credentials into production collection or create Production credentials. REST remains the safe
fallback and no Demo/Production order method is called.

## Retention and growth planning

Raw truth has no automatic retention. Logs stay on structured stdout and health is a single
atomically replaced file. At the default two-second quote poll, the hard upper bound is 18,000
Kalshi poll observations/hour before semantic duplicate suppression; lifecycle is normally only a
few rows per 40 asset-windows/hour. Coinbase volume is activity-driven and is the dominant unknown.

Capacity should be measured from the local database after at least 24 hours. For planning, an
average 1.5-6 KiB persisted Kalshi quote/orderbook row implies 27-108 MiB/hour at the unsuppressed
upper bound. A combined 5-100 Coinbase ticker messages/second at roughly 0.3-0.8 KiB/row implies
about 5-275 MiB/hour. Thus provision approximately 32-383 MiB/hour, 0.8-9.0 GiB/day, or
5.3-62.8 GiB/week until a real 24-hour measurement narrows the range. SQLite page/WAL overhead and
market activity make this an intentionally conservative capacity range, not a measured promise.
Do not delete raw rows to meet this estimate; move or archive whole verified databases only under a
future explicit retention policy.

## Configuration

- `LIVE15_NATIVE_DISCOVERY_POLL_INTERVAL_SECONDS` (default 15)
- `LIVE15_OFFICIAL_QUOTE_POLL_INTERVAL_SECONDS` (default 2)
- `LIVE15_SETTLEMENT_FOLLOWUP_INTERVAL_SECONDS` (default 15)
- `LIVE15_SETTLEMENT_FOLLOWUP_BATCH_SIZE` (default 25)
- `LIVE15_RECORDER_OPERATION_TIMEOUT_SECONDS` (default 45)
- `LIVE15_RECORDER_MAX_BACKOFF_SECONDS` (default 60)
- `LIVE15_RECORDER_CHECKPOINT_INTERVAL_SECONDS` (default 300)
- `LIVE15_RECORDER_HEALTH_INTERVAL_SECONDS` (default 30)
- `LIVE15_RECORDER_HEALTH_PATH` (default `data/health.json`)
- `LIVE15_DATASET_BUILD_INTERVAL_SECONDS` (unset/disabled by default)

Coinbase covers BTC, ETH, XRP, SOL and DOGE only. Gold, Silver, WTI Oil, HYPE and BNB have no
fabricated underlying stream. Coinbase remains predictive input and is never settlement truth.
