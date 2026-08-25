# Kalshi SDK v12 selective migration

Status: gateway foundation active for new authenticated integrations; Recorder transport unchanged.

Sources audited:

- Kalshi API environments: <https://docs.kalshi.com/getting_started/api_environments>
- Kalshi Create Order V2: <https://docs.kalshi.com/api-reference/orders/create-order-v2>
- Kalshi WebSocket API: <https://docs.kalshi.com/websockets>
- `kalshi-sdk==12.0.0` installed package and its OpenAPI/AsyncAPI-generated resources

The SDK v12 built-in host constants still select the supported shared hosts. LIVE15 uses Kalshi's
recommended dedicated `external-api` hosts. `kalshi_gateway.client` therefore validates exact
LIVE15 endpoint constants before constructing `KalshiConfig`; SDK environment variables and SDK
defaults are never consulted.

## Inventory and decisions

Scores are 1 (weak) to 5 (strong). A high LIVE15 score generally reflects domain-specific data
integrity rather than generic API breadth.

| Function | Existing LIVE15 | OWN | SDK | Decision | Changed | Rationale |
|---|---|---:|---:|---|---|---|
| REST auth / RSA-PSS | `providers/kalshi_demo.py`, `providers/kalshi_demo_execution.py`, `providers/kalshi_ws.py` | 4 | 5 | REPLACE_WITH_SDK | Gateway | SDK centralizes signing, key loading and typed errors; old clients remain for rollback/parity. |
| Public market REST | `providers/kalshi.py` | 5 | 4 | KEEP_OWN | No | Existing provider is unauthenticated, validates redirect provenance, exact 15m mapping and target identity. SDK orderbook currently requires auth. |
| Market/event/series reads | `providers/kalshi.py`, `kalshi_lifecycle.py` | 4 | 5 | KEEP_BOTH | Gateway | SDK supplies current typed schemas/pagination; LIVE15 keeps exact asset/window/target business validation. |
| Exchange status | `providers/kalshi_demo_execution.py` | 3 | 5 | WRAP_SDK | Gateway | Generic current schema belongs to SDK; LIVE15 keeps trade gating semantics. |
| REST orderbook | `providers/kalshi.py` | 5 | 4 | KEEP_BOTH | Gateway | SDK is useful for authenticated cross-checks; REST still cannot replace atomic WS truth. |
| Trades/ticker REST | limited own reads | 2 | 5 | WRAP_SDK | Gateway | SDK provides typed paginated trades. |
| Pagination | `kalshi_lifecycle.py`, Demo clients | 4 | 5 | REPLACE_WITH_SDK | Gateway | SDK has bounded `list_all`/`max_pages`; old material remains for artifact compatibility. |
| Balance | Demo execution client | 4 | 5 | REPLACE_WITH_SDK | Gateway | Typed official account truth with `exchange_index`. |
| Orders | Demo execution client | 4 | 5 | REPLACE_WITH_SDK | Gateway | Typed pagination and current direction fields; list-based 404 compatibility retained. |
| Fills | Demo execution client | 4 | 5 | REPLACE_WITH_SDK | Gateway | Current typed portfolio endpoint and pagination. |
| Positions | Demo execution client | 4 | 5 | REPLACE_WITH_SDK | Gateway | Current typed portfolio endpoint and shard routing. |
| Settlement reads | `kalshi_lifecycle.py`, Demo execution client | 5 | 5 | KEEP_BOTH | Gateway read available | SDK transports truth; LIVE15 retains finalized-label and immutable-ledger semantics. |
| Create V2 | Demo/clean executors | 3 | 5 | REPLACE_WITH_SDK | Gateway | SDK request model forbids extra/legacy fields and uses the current route. Write remains disabled by default. |
| Cancel/amend/decrease V2 | partial own cancel only | 2 | 5 | REPLACE_WITH_SDK | Gateway | SDK has current request models and `exchange_index` routing. |
| Write retry safety | own no-retry/reconciliation | 5 | 5 | KEEP_BOTH | Gateway | SDK does not retry POST/PUT/DELETE; LIVE15 keeps intent identity, risk and reconciliation policy. |
| HTTP error mapping/backoff | multiple custom mappings | 4 | 5 | WRAP_SDK | Gateway | SDK typed errors and bounded read retry reduce duplicated transport code. LIVE15 keeps sanitized audit projections. |
| WS auth/transport | `providers/kalshi_ws.py` | 4 | 5 | KEEP_BOTH | SDK factory only | SDK offers current typed transport/reconnect. Recorder does not switch until raw-frame parity is proven. |
| WS subscribe/resubscribe | `providers/kalshi_ws.py`, `kalshi_ws.py` | 5 | 5 | KEEP_BOTH | No runtime change | Both are capable; LIVE15 also owns rollover and bounded per-market recovery. |
| WS sequence-gap handling | `kalshi_ws.py` | 5 | 4 | KEEP_OWN | No | SDK silently drops the gap frame and waits for a new snapshot; LIVE15 persists typed gaps and blocks affected books for audit/replay. |
| Atomic orderbook state | `kalshi_ws.py` | 5 | 5 | KEEP_OWN | No | Existing state machine is integrated with recorder storage, checkpoints and fail-closed provenance. |
| WS heartbeat/freshness | provider + Recorder health | 5 | 4 | KEEP_OWN | No | LIVE15 health semantics cover per-asset freshness and downstream decision gates. |
| Recorder storage | `storage.py`, `native_recorder.py` | 5 | 1 | KEEP_OWN | No | Out of SDK scope. |
| Lifecycle/business semantics | `kalshi_lifecycle.py` | 5 | 2 | KEEP_OWN | No | Exact 15m mapping, rollover and settlement labeling are LIVE15 domain logic. |

## Gateway boundary

- `client.py`: exact environment allowlist, credential paths and SDK construction.
- `market_data.py`: exchange, markets, orderbook and trades wrappers.
- `portfolio.py`: balance/orders/fills/positions plus the proven `orders.get` 404 list fallback.
- `execution.py`: current V2 request models and mutations behind `write_enabled=False` by default.
- `websocket.py`: SDK WebSocket construction only; `recorder_transport_activated=False` is explicit.

No model, feature, dataset, risk or decision code imports the SDK. Existing providers are retained as
the rollback path. Legacy deletion is deferred until offline parity, live read-only parity and
Recorder raw-frame/gap provenance tests all pass.
