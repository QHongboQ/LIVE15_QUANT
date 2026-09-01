# Kalshi Production WebSocket read-only audit

Audited against Kalshi's official documentation on 2026-08-21. This project does not call an
order, cancel, portfolio, balance, position, or account endpoint from this adapter.

## Official capability

- Production: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
- Demo: `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`
- Every WebSocket handshake requires API-key authentication, including sessions used only for
  public market-data channels.
- Handshake headers are `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, and
  `KALSHI-ACCESS-TIMESTAMP`.
- The signature message is `timestamp_ms + "GET" + "/trade-api/ws/v2"` and uses the same
  RSA-PSS/SHA-256 scheme as the documented REST authentication flow.
- Python `websockets` ping/pong frames provide keepalive. The adapter also uses a bounded read
  timeout and bounded exponential reconnect backoff.
- A dedicated bounded receive pump timestamps frames immediately after `recv()` and drains the
  transport independently from SQLite persistence. This prevents consumer backlog from being
  mislabeled as exchange/network latency; queue high-watermark remains observable and no frame is
  silently dropped.

Official references:

- [WebSocket quick start](https://docs.kalshi.com/getting_started/quick_start_websockets)
- [WebSocket connection and commands](https://docs.kalshi.com/websockets/websocket-connection)
- [Orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates)
- [Market ticker](https://docs.kalshi.com/websockets/market-ticker)
- [API keys](https://docs.kalshi.com/getting_started/api_keys)

## Orderbook protocol

`orderbook_delta` requires one or more exact `market_ticker` values. The server sends an
`orderbook_snapshot` first and then `orderbook_delta` messages. Both carry a server subscription
ID (`sid`) and sequence (`seq`). Sequence is checked at the subscription level because one
subscription can contain multiple markets. Sequenced `ok` responses from `update_subscription`
are also advanced and persisted; otherwise a valid control frame would look like a missing data
frame during replay.

The snapshot contains optional `yes_dollars_fp` and `no_dollars_fp` arrays of
`[price_dollars, contract_count_fp]`. A missing side is an empty book, not zero depth at an
invented price. A delta contains exact `market_ticker`, `market_id`, `side`, `price_dollars`, and
signed `delta_fp`; the current schema may also include `ts` and `ts_ms`. Values are parsed directly
to `Decimal`.

The local state machine:

1. Refuses deltas until an initial snapshot is accepted.
2. Applies only `seq == previous_seq + 1`.
3. Marks every book on the affected subscription `UNSYNCHRONIZED` for duplicate, backward, or
   skipped sequence.
4. Blocks the entire affected subscription from the typed live-feature/paper boundary until every
   requested market has received a new official snapshot.
5. Sends one documented `update_subscription` with `action=get_snapshot` for all affected exact
   tickers.
6. Accepts the returned official snapshot as the new baseline and records resync duration.
7. Fails loudly on ticker/market identity conflicts or a delta that would create negative depth.
8. Deletes a price level only when the exact accumulated quantity becomes zero.

The `ticker` channel is also parsed. It supplies last price, Yes bid/ask, sizes, volume, and source
time whenever a ticker field changes. Its documented payload does not carry `seq`, so it is not
used to claim atomic orderbook completeness and missing No fields are not derived.

## Dynamic rollover

Rollover is lifecycle-driven, with no fixed UTC wait:

1. Existing Kalshi-native discovery provides the exact successor ticker.
2. `add_markets` adds the successor while retaining the predecessor.
3. The successor is unavailable to consumers until its official snapshot is synchronized.
4. Only after that snapshot does `delete_markets` remove the predecessor.
5. Existing REST lifecycle and settlement follow-up remain authoritative and independent.

`get_snapshot` does not change the subscription. Subscription commands use exact ticker identity;
there is no title/fuzzy matching.

## Persistence and replay

Recorder schema v8 added two role-isolated tables; schema v10 added nullable enqueue timing to the
raw event table without fabricating values for historical rows. Schema v10 to v11 adds the bounded
derived `kalshi_ws_current_books` synchronized-current-book projection; raw immutable WebSocket
history remains unchanged:

- `kalshi_ws_orderbook_events`: every raw snapshot/delta and sequenced subscription acknowledgement
  in local arrival order, including
  connection ID, `sid`, `seq`, exact identity, source/socket/enqueue/parse/persist clocks, sync
  status, receive-to-enqueue latency, and true receive-to-persist latency derived from the local
  monotonic clock.
- `kalshi_ws_book_checkpoints`: sparse synchronized books written after official snapshot/resync,
  not after every delta.
- `kalshi_ws_current_books`: the bounded synchronized-current-book projection consumed by the
  current terminal; it is derived state and does not replace the raw event history.

The raw event sequence is sufficient for deterministic reconstruction. Exact duplicate
connection/sid/seq events are idempotent; conflicting facts for the same identity fail loudly.
Sparse checkpoints bound redundant storage while raw market changes remain append-only. Schema
v7 to v8 is a single rollback-safe transaction that only creates new tables and indexes.

The existing `kalshi_prediction_quotes` REST history is unchanged and remains `kalshi_rest`
history. New records use `kalshi_ws` provenance. DatasetBuilder and the current feature registry do
not read the WS tables in this stage.

## REST fallback and execution boundary

The synchronized WebSocket book is now the live orderbook primary. REST remains an independently
recorded fallback and cross-check. A REST market response plus a
separate REST orderbook response is never relabeled as an atomic WS state. If WS is disconnected or
unsynchronized, the WS candidate is blocked; independently collected REST observations can still
be recorded under their original provenance.

The paper layer is unchanged. `SynchronizedKalshiBookProvider` returns synchronized WS books or
raises; current paper behavior is not silently switched. It contains no execution method.

## Credential boundary and Production smoke

Production and Demo credentials are not interchangeable. The user created a Production key as
read-only in Kalshi's UI; the API did not expose a permission-introspection response, so code does
not claim to have independently verified that UI permission. The key ID and RSA private key are in
separate, ACL-restricted user credential files outside this repository. Their contents and paths
are excluded from Git, logs, SQLite, API responses and test output.

A bounded 10-market Production read-only smoke completed successfully on 2026-08-21:

- authentication succeeded; 10/10 dynamically discovered current markets produced snapshots;
- 16,087 orderbook deltas and 59 ticker updates were observed in the final short sample;
- duplicate/backward/gap/reconnect/transport-error counts were all zero;
- all 10 books reconstructed identically through schema v8 replay; integrity and foreign keys
  passed;
- the bounded receive queue high-watermark was 512 frames, with no silent drop;
- monotonic receive-to-persist latency was 130.5 ms median and 259.6 ms p95;
- observed cross-clock source-to-receive age was 1.37 s median and 2.92 s p95, but Windows time
  dispersion was about 2.17 s and HTTP `Date` has only second precision, so this is not presented as
  pure network latency.

The adapter rejects repository-local credential paths, fixes the host to the documented Production
endpoint, redacts filesystem/parser failures, and exposes no order/account method. The continuous
recorder now owns this same bounded stream: dynamic rollover adds a successor, waits for its
synchronized snapshot, sends predecessor deletion, and removes the predecessor locally only after
the sequenced acknowledgement. REST lifecycle and official finalized settlement remain independent.
