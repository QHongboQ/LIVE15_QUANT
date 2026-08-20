# Robinhood Trading MCP capability audit

Audit date: 2026-08-20. Endpoint: `https://agent.robinhood.com/mcp/trading`.

This is a schema and read-only capability audit. No order review, submission,
cancellation, exercise, watchlist mutation, account-state read, or money movement
was performed. No OAuth material, account number, cookie, session, or browser
credential is stored in this repository.

## Connection and resources

Codex is configured out-of-repository as a Streamable HTTP server with OAuth and
write-tool approval. The authenticated server exposes 54 tools, seven static
resources, and no resource templates.

Relevant resources report:

- `trading://asset-types`: `event_contracts` means Prediction Markets.
- `trading://feature-availability`: event contracts, futures, crypto trading, and
  options are listed as app-only rather than MCP trading capabilities. The live
  tool schema nevertheless currently exposes equity and option order tools; the
  actual schema, not this general resource, is used for the detailed inventory.
- `trading://order-types`: market and limit.
- `trading://api-errors`: authentication, rate-limit, kill-switch,
  duplicate-order, not-found, and invalid-request errors.

## Event-contract verdict

**Robinhood MCP Event Contract capability = currently unsupported.**

The actual `search` schema accepts only `instrument`, `currency_pair`, and
`market_index`; its description says events and futures may be added when their
corresponding tools land. Read-only searches using `asset_type=event_contracts`
for BTC, ETH, SOL, and DOGE 15-minute markets each returned:

```text
unsupported asset_type "event_contracts"; supported: "instrument", "currency_pair", "market_index"
```

No tool name or schema provides event discovery, event/contract metadata, an
event quote, Yes/No bid or ask, an event position, an event order, cancellation,
or event-order status. Two generic accounting surfaces mention the asset class:

- `get_portfolio(account_number)` returns aggregate `event_contracts_value`.
- `get_pnl_trade_history(account_number, ...)` can include already realized
  prediction-market trades in a generic trade-history shape.

Neither surface identifies a live contract or makes an event contract queryable
or executable.

## Requested capability matrix

| Capability | Actual MCP status |
| --- | --- |
| Prediction Markets / Event Contracts | Named by resources and accounting fields only |
| Live 15-minute contracts | Unsupported |
| Market/event discovery | Unsupported |
| Event contract quote / timestamp | Unsupported |
| Yes / No | Unsupported |
| Yes bid/ask; No bid/ask | Unsupported |
| Event/contract ID | Unsupported |
| Event positions | Unsupported; aggregate portfolio value only |
| Event buying power | Unsupported; generic account buying power exists |
| Event open orders / status | Unsupported |
| Buy Yes / Buy No | Unsupported |
| Add event position | Unsupported |
| Close/reduce event position | Unsupported |
| Cancel event order | Unsupported |

## Other products and key schemas

The current inventory supports:

- equities: discovery, quotes, Level 2 book, historicals, fundamentals,
  tradability, positions, tax lots, orders, review, submit, and cancel;
- options: chains, instruments, quotes, historicals, positions, orders, review,
  submit, cancel, and exercise lifecycle;
- market indexes: discovery, current values, and historicals;
- crypto: pair discovery, watchlist references, portfolio/PnL accounting fields,
  but no crypto quote, position, order, or cancellation tool in this inventory;
- account-level reads: `get_accounts`, `get_portfolio`, realized PnL, and generic
  trade history.

Important parameter and result boundaries:

- `search(query, asset_type?, limit?)` returns only equity instruments, crypto
  currency pairs, or market indexes.
- `get_accounts()` returns account metadata and `agentic_allowed`; write tools
  reject accounts that are not authorized for this agent.
- `get_portfolio(account_number)` returns cash, buying power, total value, and
  per-asset-class values, including only aggregate event-contract value.
- `get_equity_positions(account_number)` and
  `get_option_positions(account_number, ...)` do not return event positions.
- `get_equity_orders(...)` and `get_option_orders(...)` return order IDs, states,
  quantities, fills, prices, fees, timestamps, and rejection details.
- `place_equity_order` / `place_option_order` are real-money writes and were not
  called. `cancel_equity_order` / `cancel_option_order` are writes and were not
  called. There are no event equivalents.

## LIVE15_QUANT decision

Do not build a `RobinhoodMCPProvider` yet: there is no event-contract read schema
to implement. Keep the MCP configured as a future official adapter and re-audit
its advertised tools before any implementation.

Current production data roles remain separate:

1. Robinhood public SSR: 15-minute discovery and metadata.
2. Kalshi official public REST: independently verified venue Bid/Ask, last trade,
   and orderbook; never labeled Robinhood executable.
3. Coinbase public feed: predictive underlying market data only.
4. Settlement providers: actual benchmark truth when officially obtainable.

`ExecutionProvider` is a protocol boundary only. `HardRiskLayer` is a separate
protocol with immutable, explicitly supplied limits and mandatory stale-quote,
source-health, and fill-certainty signals. No provider implementation or risk
limit values exist in this milestone.
