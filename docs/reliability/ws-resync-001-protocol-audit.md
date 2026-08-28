# WS-RESYNC-001 upstream protocol audit

Retrieved 2026-08-28. This implementation uses only the documented Kalshi
WebSocket order-book recovery surface; it does not issue order, account, or
portfolio commands.

| Protocol fact | LIVE15 boundary |
| --- | --- |
| `orderbook_delta` sends a snapshot before incremental deltas. | A snapshot is the only baseline that can make a local book authoritative. |
| `update_subscription` accepts `get_snapshot` and returns snapshots without changing the delta subscription. | A sequence or parser failure quarantines the local subscription. The bounded ladder is initial snapshot request, one snapshot retry, one fresh subscription, then one transport reconnect; deltas stay non-authoritative until the whole expected snapshot set is complete. |
| `get_snapshot` uses `sids` and `market_tickers`. | Typed recovery commands emit one subscription-id list and exact ticker list. |
| With `use_yes_price: true`, NO snapshot/delta prices arrive on YES-leg scale. | The parser converts NO prices once at ingress back to LIVE15's canonical NO-leg representation. Existing execution/features therefore retain their complementary-price semantics and do not invert again. |

Primary evidence:

- https://docs.kalshi.com/websockets/orderbook-updates
- https://docs.kalshi.com/websockets/websocket-connection
- https://docs.kalshi.com/getting_started/order_direction
- https://docs.kalshi.com/changelog

The source rows remain immutable evidence. A derived book is unavailable on
any sequence, subscription, connection, ticker, market identity, or quantity
invariant failure, and can resume only after the replacement snapshot set has
been validated.
