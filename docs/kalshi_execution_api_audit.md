# Kalshi execution API audit (2026-08-20)

Milestone 5 includes a **Demo-only, authenticated, GET-only connectivity audit**. It does not
submit, amend, decrease, or cancel orders. Production authenticated APIs remain documentation
only and are not configured or callable.

| Topic | Official result |
| --- | --- |
| Production REST | `https://external-api.kalshi.com/trade-api/v2` |
| Demo REST | `https://external-api.demo.kalshi.co/trade-api/v2`; credentials are separate from production |
| Production WebSocket | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` |
| Demo WebSocket | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` |
| Authentication | Kalshi API Key ID plus locally held RSA-PSS SHA-256 private key; signed timestamp/method/path headers |
| Signature | `timestamp_ms + uppercase_method + /trade-api/v2/path`; query parameters are excluded; headers are `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE` |
| Read-only portfolio | `GET /portfolio/balance`, `/portfolio/positions`, `/portfolio/orders`, `/portfolio/fills` |
| Market discovery | `GET /markets`; public market data remains independent from account state |
| Order API schema | `POST /portfolio/events/orders` (V2 fixed-point request) documents limit orders and `immediate_or_cancel`, `fill_or_kill`, and resting orders |
| Cancel API schema | `DELETE /portfolio/events/orders/{order_id}` (V2) |
| Rate limits | Separate read/write token buckets; Basic tier documentation lists 200 read and 100 write tokens/second, with endpoint weights |
| Account requirements | A Kalshi account, identity verification/KYC, accepted agreements, funding for production, and user-created API credentials |

The Demo audit has a fixed Demo hostname and a five-path GET allowlist. It deliberately has no
generic public request method and exposes no write operation. It reads the private key only from
an absolute `.key`/`.pem` path outside the repository, holds it in process memory for signing,
and never returns or logs the key ID, key path, private-key bytes, signature, or headers.

Demo and production keys are not interchangeable. A future execution adapter must be separately
approved, must retain an environment boundary, and must remain behind the immutable hard-risk
layer. No production credential is requested by this implementation.

## Demo runtime verification (2026-08-20)

An authenticated audit against the recommended Demo REST host completed successfully after
credential-file ACL hardening. `GET /portfolio/balance`, `GET /markets`,
`GET /portfolio/positions`, `GET /portfolio/orders`, and `GET /portfolio/fills` all succeeded;
the bounded market query returned ten open markets. The audit did not log the balance value or
any account/credential material. No POST, PUT, PATCH, or DELETE request was available or sent.

This confirms Demo authentication and the portfolio-read surface, not order execution. Official
create/cancel schemas remain documentation-only until a later explicit approval for Demo order
testing; Production credentials and Production writes remain out of scope.

Official evidence:

- [API environments and endpoints](https://docs.kalshi.com/getting_started/api_environments)
- [Authenticated requests and RSA-PSS signing](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests)
- [API key authentication](https://docs.kalshi.com/getting_started/api_keys)
- [WebSocket connection and channels](https://docs.kalshi.com/getting_started/quick_start_websockets)
- [Balance](https://docs.kalshi.com/api-reference/portfolio/get-balance), [positions](https://docs.kalshi.com/api-reference/portfolio/get-positions), [orders](https://docs.kalshi.com/api-reference/orders/get-orders), and [fills](https://docs.kalshi.com/api-reference/portfolio/get-fills)
- [Create order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2) and [cancel order V2](https://docs.kalshi.com/api-reference/orders/cancel-order-v2)
- [Rate limits](https://docs.kalshi.com/getting_started/rate_limits)

Fee model evidence:

- [Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
- [Fixed-point fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)
- [Series fee changes API](https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes)

The local simulator uses the published general taker formula `0.07 × contracts × price ×
(1-price)`, applies the documented fixed-point rounding/cent-alignment accumulator, and labels
the result as an assumption. The public fee-change endpoint returned no override for any of the
ten target 15-minute series on the audit date. This is not a promise that future fees stay the
same; runtime fee metadata must be revalidated before any later demo/production integration.
