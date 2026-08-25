# Kalshi SDK v12 selective migration

Status: the SDK-native Recorder route is the authoritative Production market-data path. The
legacy WebSocket implementation remains `LEGACY_ROLLBACK_ONLY` for one version cycle.

Sources audited:

- Kalshi API environments: <https://docs.kalshi.com/getting_started/api_environments>
- Kalshi Create Order V2: <https://docs.kalshi.com/api-reference/orders/create-order-v2>
- Kalshi WebSocket API: <https://docs.kalshi.com/websockets>
- [`TexasCoding/kalshi-python-sdk`](https://github.com/TexasCoding/kalshi-python-sdk), consumed
  solely as the pinned dependency `kalshi-sdk==12.0.0` (no SDK source is copied or forked)
- `kalshi-sdk==12.0.0` installed package and its OpenAPI/AsyncAPI-generated resources

## Ownership boundary

`kalshi-sdk` is the external Kalshi infrastructure dependency. It owns authenticated REST and
WebSocket transport, typed subscriptions, SID routing, reconnect/resubscribe, and V2 request
models. LIVE15 does not monkey-patch or reimplement those mechanisms.

`live15_quant.kalshi_gateway` is a thin, project-owned boundary around that dependency. It
converts SDK models into immutable LIVE15 DTOs, applies explicit Production configuration,
records metrics, and exposes only the project-specific contract required downstream. The
Reliability Adapter, 15-minute universe/window identity, persistence DTO, settlement truth,
model, risk, Materializer, Paper, and Control Center remain LIVE15 domain code.

The official path is:

```
Kalshi Production -> kalshi-sdk -> LIVE15 KalshiGateway -> Reliability Adapter
                  -> Recorder Consumer -> RecorderStore -> Materializer / Paper
```

This separation is intentional: upgrading the external SDK is a dependency change, while
LIVE15-specific behavior is maintained in the Gateway and downstream domain modules.

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
| WS auth/transport | `providers/kalshi_ws.py` | 4 | 5 | USE_SDK_DIRECTLY | Gateway | SDK owns typed transport, SID routing, reconnect and resubscribe. The legacy client is rollback-only. |
| WS subscribe/resubscribe | `providers/kalshi_ws.py`, `kalshi_ws.py` | 5 | 5 | USE_SDK_DIRECTLY | Gateway | The authoritative Recorder uses the SDK's SID-routed typed subscription iterator; LIVE15 does not recreate subscription state. |
| WS sequence-gap handling | `kalshi_ws.py` | 5 | 4 | KEEP_LIVE15_DOMAIN | Reliability Adapter | SDK delivers typed frames; LIVE15 persists gaps and blocks affected books for audit/replay. |
| Atomic orderbook state | `kalshi_ws.py` | 5 | 5 | KEEP_OWN | No | Existing state machine is integrated with recorder storage, checkpoints and fail-closed provenance. |
| WS heartbeat/freshness | provider + Recorder health | 5 | 4 | KEEP_OWN | No | LIVE15 health semantics cover per-asset freshness and downstream decision gates. |
| Recorder storage | `storage.py`, `native_recorder.py` | 5 | 1 | KEEP_OWN | No | Out of SDK scope. |
| Lifecycle/business semantics | `kalshi_lifecycle.py` | 5 | 2 | KEEP_OWN | No | Exact 15m mapping, rollover and settlement labeling are LIVE15 domain logic. |

## Gateway boundary

- `client.py`: exact environment allowlist, credential paths and SDK construction.
- `market_data.py`: exchange, markets, orderbook and trades wrappers.
- `portfolio.py`: balance/orders/fills/positions plus the proven `orders.get` 404 list fallback.
- `execution.py`: current V2 request models and mutations behind `write_enabled=False` by default.
- `websocket.py`: SDK WebSocket construction and typed subscription boundary only.
- `canonical_ws.py`, `reliability.py`, `recorder_provider.py`, and `recorder_consumer.py`:
  project-specific immutable adaptation, gap/quarantine policy and persistence delivery; none
  reimplements SDK authentication, sockets, SID routing, or reconnect.

No model, feature, dataset, risk or decision code imports the SDK. The legacy provider remains
available only as a controlled rollback path; deletion is deferred until the retained rollback
window and operational review have completed.
