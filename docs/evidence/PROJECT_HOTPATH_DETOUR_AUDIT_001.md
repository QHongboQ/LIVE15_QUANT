# Project hot-path detour audit

**Task:** `PROJECT-HOTPATH-DETOUR-AUDIT-001`
**Baseline:** `origin/main` `2dce889e6833ce102ae25b9ce541d774ca1713c8`
**Method:** source/control-flow audit only; no runtime, database, or Production mutation

## Authority boundary

Kalshi's synchronized SDK WebSocket book is the current prediction-market
authority.  Raw events are durable history and `kalshi_ws_current_books` is the
bounded current projection.  The terminal is localhost/read-only and only
subscribes active channels.  This audit does not recommend replacing the
official Kalshi WebSocket with a third-party source or adding a second current
state owner.

## Active flows

| Source fact | Acquisition / parse | Durable path | Projection / consumer |
| --- | --- | --- | --- |
| Kalshi order book | SDK callback → typed order-book validation | raw `kalshi_ws_orderbook_events` plus bounded current-books row atomically | ControlCenter cursor → `markets()` or exact `market()` → browser WS |
| Coinbase underlying | public WebSocket → typed tick | `coinbase_ticks` | latest replay query → detail/feature projection |
| Pyth underlying | authenticated streaming / bounded recovery → typed observation | `underlying_observations` | latest replay query → detail/feature projection |
| Binance / Hyperliquid secondary | public WebSocket → typed secondary observation | `secondary_underlying_observations` | BNB/HYPE detail only |
| Historical chart | current ticker/window query | existing durable rows only | bounded/downsampled HTTP history; normal terminal ticks do not request history |

## Ranked findings

### P0 — legacy REST quote projection can omit `no_ask`

**Finding:** a historical `kalshi_prediction_quotes` row without `no_ask`
causes ControlCenter's read projection/feature reconstruction to index a
missing SQLite field.  This produces a real `/api/markets` / detail availability
failure, rather than a truthful unavailable quote.

**Disposition:** independent corrective PR
[`#145`](https://github.com/QHongboQ/LIVE15_QUANT/pull/145),
`agent/control-center-market-projection-compat-fix-001`.
It makes the projection schema-aware and derives a binary-equivalent no ask
only from the same row's yes bid when the historical field is absent.  It does
not alter the synchronized WS authority, stored price facts, or schema.

### P1 — ControlCenter terminal cursor is still SQLite-backed

Every subscribed terminal channel polls a cursor at 100 ms.  The markets
cursor opens the read-only store and obtains the newest id from five tables;
when it changes, the event path materializes all ten market summaries.  Exact
market channels scope cursor reads more tightly, but still reopen SQLite.

This is an intentional current architecture, not an unindexed scan: the cursor
queries use primary-key descending `id` access, and terminal subscriptions are
visibility/channel scoped.  It remains a measurable disk round-trip before the
browser.  The separate `REALTIME_CURRENT_STATE_PARALLEL_PERSISTENCE_AUDIT_001`
already establishes that an in-memory current-state change needs a formal
cross-source/restart/failure design first.  **No implementation branch is safe
from this audit.**

### P1 — archive throughput is below raw ingress

The storage audit independently measured archive throughput below incoming raw
WS events.  This is a capacity incident, not a hot-path correctness patch.  It
must remain isolated from terminal/realtime changes and requires an authorized
operator capacity decision.  No code branch is created here.

### P2 — secondary BNB/HYPE path writes then rereads for latency

The secondary underlying path persists an observation and immediately rereads
the latest row to compute the latency projection.  It is confined to BNB/HYPE
diagnostics, is not the primary price authority, and does not drive all-market
terminal updates.  Folding it into an unapproved generic current-state cache
would create a second owner.  **Defer** until the formal current-state design
has a canonical event boundary.

### P2 — current-session lookup sorts a bounded ten-row projection

`_current_ws_book()` selects the newest session before fetching an exact
ticker.  The projection is hard-bounded to the active market set, so this is
not a production-scale table scan.  **Ignore**; an index or cache would add
more ownership than value.

### Ignore — normal realtime history fetch

The terminal's incremental chart path uses local WebSocket updates.  Bounded
history is fetched for snapshot/reconciliation only; the normal tick path does
not issue HTTP history requests.  **No regression found.**

### Ignore — inactive heavy pages

Portfolio, Research, and Admin are visibility-aware and inactive tabs do not
continue their heavier requests.  **No N+1 or background history detour found
in the active terminal contract.**

## Follow-up order

1. Merge/review the self-contained P0 compatibility fix only after its CI
   gates pass.
2. Treat storage capacity as a human-authorized incident; do not compensate by
   weakening retention truth.
3. If latency evidence later warrants it, draft the current-state design before
   implementing any in-memory/browser path.  It must preserve durable parity,
   restart recovery, bounded queue failure semantics, and source ownership.

No generic framework, cache manager, duplicate provider, or production change
is proposed by this audit.
