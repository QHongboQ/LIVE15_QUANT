# Kalshi SDK WebSocket shadow parity

This component evaluates `kalshi-sdk==12.0.0` market-data reliability without
changing LIVE15's official Recorder input. The production endpoint is
`wss://external-api-ws.kalshi.com/trade-api/ws/v2`.

## Ownership boundary

- The SDK owns authentication, connection, typed frames, reconnect,
  resubscribe, and transport sequence detection.
- The LIVE15 adapter owns canonical event mapping, gap persistence, whole-book
  quarantine, per-asset synchronization/freshness, lifecycle projection, and
  parity comparison.
- The existing Recorder remains the only writer of raw data, features, and the
  live book projection. Shadow telemetry is stored separately in
  `data/kalshi_sdk_ws_shadow.sqlite3` and is Git-ignored.

## Canonical contract

Each canonical event carries asset, exact ticker, event type, sequence (when
provided), exchange timestamp (when provided), adapter receive timestamp,
subscription and connection identity, provenance, and a typed payload.
Order-book snapshots contain YES/NO bid price and quantity levels. Deltas carry
side, price, signed quantity change, and market identity. Lifecycle events
preserve provider event/result/exchange-index values. Gap and reconnect events
are also persisted as canonical reliability events.

The Recorder/feature contract requires exact market identity, one contiguous
order-book subscription sequence, atomic snapshot/delta application, both
exchange and local receive time where available, executable bid/ask depth,
lifecycle identity, and a fail-closed synchronization state. A gap invalidates
all books in that subscription. Nothing becomes synchronized again until the
SDK resubscription supplies a complete authoritative snapshot set.

## Parity rules

The shadow compares against `data/kalshi-live-ws-books.json`, never the Recorder
database. It uses exact ticker and exact price/depth equality within a strict
one-second receive-time alignment window. An unavailable old projection is
classified as `OLD_WS_UNAVAILABLE`; it does not count as an SDK failure.

Promotion requires a separate decision after sustained observation. Merely
running this component does not activate `KalshiWebSocketGateway` for Recorder
writes (`recorder_transport_activated` remains `False`).
