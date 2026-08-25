# Kalshi SDK Reliability Shadow

This read-only validation path is deliberately separate from the official LIVE15 Recorder:

```text
Kalshi Production
  -> kalshi-sdk v12 WebSocket
  -> immutable SDK canonical events
  -> LIVE15 reliability adapter
  -> data/sdk_reliability_shadow.sqlite3
```

The SDK owns authentication, socket transport, subscription, typed payloads, reconnect and
resubscribe. LIVE15 owns only domain reliability: subscription-sequence gaps, book quarantine,
authoritative snapshot recovery, per-asset freshness, lifecycle/window validation and an atomic
shadow-recorder commit.

The official Recorder database is never opened by this component. The legacy LIVE15 WebSocket
transport remains the formal Recorder path and a future rollback/reference candidate. Old-vs-new
book parity is not a promotion criterion; low-frequency SDK REST orderbook reads validate canonical
YES/NO side and executable-price semantics without becoming a realtime source.

For the subscription-wide sequence used by the orderbook channel, one missing frame can belong to
any subscribed ticker. A gap therefore quarantines the full subscription, persists the gap, and
withholds authoritative quotes until fresh snapshots for the complete subscribed set arrive.

Validation command (no order or account mutation capability):

```powershell
live15-sdk-reliability-shadow --duration 1260 --rest-interval 60 `
  --validation-reconnect-after 300
```

Promotion remains a separate explicit change after all ten assets synchronize, at least one
15-minute rollover and one reconnect/resubscribe recover, REST side semantics pass per asset, and
the store has no unrecovered gap or consistency error.
