# Realtime current-state / parallel-persistence audit

Status: AUDIT_ONLY / NO_IMPLEMENTATION

Base: `2dce889e6833ce102ae25b9ce541d774ca1713c8`

## Scope and invariants

This audit traces the active Recorder-to-ControlCenter path without changing it. Browser state is
never an authority. SQLite raw events and archive remain durable evidence; Recorder continues to
own validation, synchronization, gap, provenance, and settlement semantics.

## Active source paths

| Source | Receive and canonicalization | Durable write | Current projection | ControlCenter/browser path | Owner classification |
| --- | --- | --- | --- | --- | --- |
| Kalshi official SDK WS | `production_recorder_host.py:_accept_typed_orderbook` copies an SDK callback into `canonical_from_sdk`, then `SdkRecorderMarketDataProvider` queues validated events. | `RecorderMarketDataConsumer` batches up to 128 events or one second; `RecorderStoreDomainWriter` calls `write_kalshi_ws_persistence_event_batch_atomic`. Raw WS facts and `kalshi_ws_current_books` update in one transaction. | `on_committed` reaches `KalshiNativeRecorder._on_sdk_ws_committed` only after durable commit; it updates `_kalshi_ws_books` and synchronized health. A coalesced JSON file is emitted every 200 ms. | `/ws/terminal` polls `realtime_asset_cursor`, then `realtime_asset`; both open read-only SQLite and read `kalshi_ws_current_books`. | KEEP durable batch and atomic bounded projection; REPLACE_CANDIDATE only after a single cross-process current-state owner is designed. |
| Coinbase WS | `KalshiNativeRecorder._record_coinbase` receives public ticks. | `RecorderStore.append_coinbase` is called before health/gap advancement. | No shared in-memory market projection exists. | Cursor reads latest `coinbase_ticks.id`; detail projection reads latest tick from SQLite. | SIMPLIFY candidate only after canonical event/current-state parity is proven. |
| Pyth stream / bounded REST recovery | `KalshiNativeRecorder._accept_pyth_batch` validates each observation. | `RecorderStore.append_underlying` is called before health/gap advancement. | No shared in-memory market projection exists. | Cursor and detail queries read `underlying_observations` from SQLite. | SIMPLIFY candidate only after canonical event/current-state parity is proven. |
| Binance BNB / Hyperliquid HYPE secondary streams | `KalshiNativeRecorder._record_secondary` normalizes one venue-native tick. | `RecorderStore.append_secondary_underlying` writes synchronously. | The recorder then calls `latest_secondary_underlying` again solely to read persistence latency. | Cursor and detail queries read `secondary_underlying_observations` from SQLite. | SIMPLIFY candidate; the post-write latency reread is a concrete removable detour if a write result can safely carry that timing. |

## Confirmed hot-path observations

1. `ControlCenterService.terminal_cursor` runs from `control_center.py` every 100 ms for each
   subscribed channel. `markets` uses `DashboardReadStore.realtime_cursor`; an asset detail uses
   `realtime_asset_cursor`.
2. On every changed cursor, `terminal_event` calls `markets()` or `realtime_asset()`. Each opens a
   read-only SQLite connection and re-materializes the browser payload. The markets channel
   materializes all active assets, not only the changed asset.
3. The active SDK Kalshi callback deliberately invokes `_on_sdk_ws_committed` only after the SQLite
   batch transaction succeeds. A persistence delay therefore delays both Recorder's in-memory book
   and browser-visible state.
4. `KalshiNativeRecorder._publish_kalshi_ws_live_books` already writes an immutable cross-process
   snapshot (`kalshi-live-ws-books.json`), but the active ControlCenter reads neither that file nor
   an equivalent shared-memory surface. It also covers Kalshi books only, not Coinbase, Pyth, or
   the secondary venues.
5. Raw history and bounded `kalshi_ws_current_books` are correctly atomic and fail closed on
   desynchronization. They must remain the restart/recovery source of truth.

## Current ownership classification

| Owner | Classification | Reason |
| --- | --- | --- |
| Recorder validation, gap state, provenance, raw writes, archive | KEEP | Domain truth and recovery authority. |
| `kalshi_ws_current_books` atomic SQLite projection | KEEP | Bounded durable recovery projection; invalidates atomically when unsynchronized. |
| ControlCenter SQLite cursor and projection reads | REPLACE | This is the confirmed disk round-trip before browser delivery. |
| Recorder `_kalshi_ws_books` after durable commit | SIMPLIFY | Useful local projection, but not a cross-process browser owner and currently delayed behind persistence. |
| `kalshi-live-ws-books.json` | REMOVE_OR_REPLACE | It is a second current-book publication path used by shadow consumers, but not by ControlCenter; it cannot become browser authority without a single-owner decision. |
| Browser chart state | KEEP | Presentation-only consumer; never authority. |

## Decision

`IMPLEMENTATION_SAFE = NO` on this audit branch.

The target concept can reduce ControlCenter disk reads only if a separate design first establishes:

1. one canonical event boundary shared by Kalshi, Coinbase, Pyth, Binance, and Hyperliquid;
2. one in-process current-state owner with an explicit cross-process publication/recovery contract;
3. a bounded persistence queue whose event identity, ordering, capacity, backlog age, high-watermark,
   drop count, and failure behavior are observable;
4. durable raw-event parity independent of browser connections; and
5. restart recovery from SQLite without retaining a second live owner.

Without those decisions, wiring ControlCenter to the existing Kalshi JSON file would create a
Kalshi-only second authority, skip the other source paths, and weaken synchronization/recovery
semantics. No implementation branch is authorized by this audit.

## Follow-up entry criteria

Before any implementation branch, capture a bounded benchmark of the existing 100 ms cursor/read
cost per channel and define deterministic tests for one event, persistence parity, reconnect,
restart recovery, queue bounds, and browser-independent persistence. The implementation must delete
or bypass the corresponding SQLite-before-browser path rather than add a parallel permanent path.
