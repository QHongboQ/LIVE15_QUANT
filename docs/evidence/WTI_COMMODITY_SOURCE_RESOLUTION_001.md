# WTI commodity source resolution

**Task:** `WTI-COMMODITY-SOURCE-RESOLUTION-001`
**Status:** RESEARCH COMPLETE — implementation intentionally deferred
**Observed:** 2026-09-02 UTC
**Production mutation:** none

## Decision

Do **not** replace the inactive configured `Commodities.USOILSPOT` feed with a
dated WTI future.  That would be semantically wrong for LIVE15's active
`KXWTI15M` contracts.

The current Kalshi WTI market rule names the close of the one-minute Pyth
**PYTHOIL** candlestick for both the start and end comparison.  The current
official market metadata was read only from
[`KXWTI15M-26SEP020500-00`](https://api.elections.kalshi.com/trade-api/v2/markets/KXWTI15M-26SEP020500-00):

> If the close price of the 1-minute candlestick for WTI Oil ... is at least
> the close price of the 1-minute Pyth PYTHOIL candlestick ...

The source rule also specifies two-decimal settlement rounding and uses the
most recently published source data if the specified instant is absent.  The
rule's source of record is Pyth, not CME settlement, a generic WTI spot quote,
or a rolling futures proxy.

Pyth currently lists
[`Commodities.Index.PYTHOIL/USD`](https://app.pyth.com/explore/Commodities.Index.PYTHOIL%2FUSD)
as a **stable**, 24/7 commodity **index**, with Pyth Pro/Lazer id `3063`, a
50 ms minimum channel, and Hermes id
`67784f72e95ac01337edb7d7bd5bbd1c03669101b7068a620df228ed4e52ef14`.
This is the required predictive series for this Kalshi family.

## Root cause

LIVE15 currently configures this retired source in `providers/pyth.py`:

| Configured symbol | Configured Hermes id | Current Pyth state | Instrument |
| --- | --- | --- | --- |
| `Commodities.USOILSPOT` | `925ca92ff005ae943c158e3563f59698ce7e75c5a8c8dd43303a0a154887b3e6` | inactive | WTI light-sweet crude CFD |

The observed live ControlCenter market detail confirms that the Recorder still
projects this exact stale identity as `source_unavailable`.  The inactive
series is also materially different from the current `KXWTI15M` target scale;
therefore it must not be made healthy by changing only presentation or
freshness policy.

## Futures are not a substitute

Pyth's current WTI futures chain is genuine and usable for a product whose
specification calls for an individual future.  It publishes dated monthly
symbols such as `Commodities.WTIV6/USD` (22 September 2026) and
`Commodities.WTIX6/USD` (20 October 2026); their chain id is `WTI`.
Pyth documents that those feeds roll by their explicit expiration metadata.
See [Pyth Pro futures terminology](https://docs.pyth.network/price-feeds/pro/futures-terminology).

That rollover rule is **not** LIVE15's current rule: a dated contract is a
future, while the Kalshi rule names the `PYTHOIL` index.  Selecting a
front-month future, a next-month future, or a continuous/spot proxy would
create a silent settlement mismatch.  Consequently:

- no WTI futures feed id is approved as a fallback;
- no maturity or roll convention is configured;
- the current exact WTI degradation remains truthful until the validated
  `PYTHOIL` path is separately implemented and tested.

## Provider and sibling conclusion

| Asset | Current semantic series | Current state | Recommendation |
| --- | --- | --- | --- |
| Gold | `Metal.XAU/USD` spot | stable | retain existing Pyth Core/Hermes mapping |
| Silver | `Metal.XAG/USD` spot | stable | retain existing Pyth Core/Hermes mapping |
| WTI | `Commodities.Index.PYTHOIL/USD` index | stable | dedicated, validated Pyth Pro/Core entitlement and `PYTHOIL` adapter work |

Pyth is the shared data vendor, but Gold/Silver and WTI are not interchangeable
static feeds.  A commodity implementation may share credential and transport
plumbing only if it preserves each asset's exact instrument identity, metadata
state, source timestamp, session semantics, and failure boundary.  It must not
change Gold or Silver while repairing WTI.

Pyth's current documentation says Hermes price delivery requires an API key and
that Pyth Pro price calls require a server-side bearer key; it also documents
the public symbols endpoint as the source for current feed metadata.  See
[Pyth Core price feeds](https://docs.pyth.network/price-feeds/core/price-feeds),
[Hermes delivery](https://docs.pyth.network/price-feeds/core/how-pyth-works/hermes),
and [Pyth Pro REST](https://docs.pyth.network/price-feeds/pro/api/rest).
This audit did not reveal or test any credential material or entitlement.

## Required follow-up contract

A separate `agent/wti-provider-fix-001` may be opened only after the following
read-only preflight succeeds against the exact Production-approved Pyth
credential class:

1. Resolve `PYTHOIL` by current symbol metadata; reject an absent, inactive, or
   non-index result.
2. Verify the credential is entitled to feed `3063` (or to the metadata's
   authoritative successor), without logging key material.
3. Receive current data with Pyth's own source timestamp and preserve its
   one-minute-candle semantics for the terminal's predictive input.
4. Keep the retired `USOILSPOT` id explicitly unavailable; never treat a
   retired response or generic oil quote as success.
5. Persist and health-check exact selected identity, source timestamp,
   receive timestamp, metadata state, and stale/entitlement/transport reason.
6. If Pyth retires or supersedes `PYTHOIL`, fail closed until a new Kalshi rule
   confirms the new named settlement series.
7. Prove Gold and Silver behavior is unchanged, and prove no false fallback or
   futures-roll selection occurs.

No implementation is included here because the current Recorder Pyth client is
configured for static Core/Hermes feed ids, while the required `PYTHOIL`
instrument must be entitlement-tested and the one-minute settlement semantics
must be made explicit.  This is a semantic/provider change, not a safe
configuration-only repair.

## Evidence boundaries

- Read-only local ControlCenter `GET /api/markets/WTI%20Oil` confirmed the
  active `KXWTI15M` contract, the unavailable retired identity, and no
  underlying fallback.
- Read-only official Kalshi market metadata confirmed the current settlement
  rule.
- Read-only Pyth Pro public symbol metadata confirmed the current `PYTHOIL`
  identity and the inactive legacy/futures metadata.
- No Recorder, database, credentials, Production configuration, or deployment
  was modified.
