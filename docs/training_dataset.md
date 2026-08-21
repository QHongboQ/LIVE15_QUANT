# Training dataset and feature-store contract

Milestone 7 derives supervised-learning rows from the recorder without modifying raw tables. The
raw SQLite database remains the source of truth; `data/features.sqlite3` is a separate, rebuildable
SQLite WAL feature store. Decimal values are JSON/SQL strings and all timestamps are UTC-aware.

## As-of and label boundary

Each row is keyed by exact Kalshi `ticker + window + decision_timestamp`. Metadata must have been
fetched by the decision, and may only be OPEN or PAUSED. Kalshi quotes require both local receive
time and any available source timestamp to be no later than the decision. Coinbase ticks use the
same rule for receive and exchange timestamps. Freshness fails closed when either the local receive
age or an available source/exchange age exceeds its configured limit; `quote_age_seconds` itself is
specifically the observable local receive age. Post-window observations are excluded.

The feature engine accepts no label or settlement object. Only the dataset builder joins the
engine output to the immutable Kalshi `finalized/result/settlement_ts` record, by exact ticker and
window. Coinbase is never used to manufacture or verify a final label. Missing values stay null and
carry one of `truly_missing`, `stale`, `not_enough_lookback`, `source_unavailable`, or
`market_side_unavailable`; zero is never an implicit imputation.

## Sampling and features

`SamplingPolicy` accepts any unique offsets inside the 15-minute window. The default environment
configuration requests 14m, 12m, 10m, 8m, 5m, 3m, 2m, 1m, and 30s remaining, but the feature engine
contains no fixed grid or real-world date/time dependency.

Feature schema `1.0.0` contains 42 explainable features across contract geometry, underlying
returns, realized volatility/range, official contract bid/ask/last, explicit Yes/No bid-book depth,
book imbalance/change, and descriptive spread-aware market-implied quantities. The authoritative
name, unit, formula, lookback, missing policy, and timestamp semantics for every feature live in
`live15_quant.feature_registry.FEATURE_REGISTRY`. Midpoint is descriptive only and is never an
execution price or asserted true probability.

Coinbase predictive inputs exist for BTC, ETH, XRP, SOL, and DOGE. Gold, Silver, WTI Oil, HYPE, and
BNB use the independently identified Pyth Hermes primary observations. Missing historical periods
remain `source_unavailable`; neither provider is settlement truth.

## Reproducibility and restart

Dataset version `1.2.0` and feature schema version `1.0.0` are stored with every row. A deterministic
build ID hashes configuration plus a path-free recorder snapshot: raw schema version and, for each
source table, row count, maximum row ID, and ordered content-hash digest. All reads are capped at the
captured row-ID boundaries, so a recorder that keeps appending cannot silently alter an in-progress
build. Each `ticker + decision_timestamp` is unique within a build; equal rows resume idempotently
and conflicting rows fail loudly.

The feature store includes a machine-readable manifest, registry, provenance row IDs, per-feature
timestamps/missing reasons, build status, and diagnostics. SQLite was chosen for restartable atomic
writes and deterministic indexed replay. Replay verifies each stored training row's content hash
before deserialization, so syntactically valid SQLite value corruption also fails loudly. It is
training-friendly through pandas/Polars/DuckDB and can be exported to Parquet later; Parquet is not
used as an append-time source of truth.

## Evaluation splits

`chronological_split` and `walk_forward_splits` operate on whole ticker/event groups. Expanding and
rolling walk-forward policies are typed and configurable. Multiple decision rows from one 15-minute
event can never cross train/validation/test boundaries. Random row splitting is intentionally not
provided as a formal evaluation path.

`NormalizationPolicy` supports global and per-asset profiles. It is deliberately not run during the
dataset build: callers must fit it only on the training event groups of each fold, then reuse that
profile for validation/test or live inference. Missing inputs stay missing. This preserves both
pooled-model and asset-specific model research without leaking future-fold statistics.

## Gap quarantine

Raw schema v9 records append-only OPEN and RECOVERED source-gap facts independently from
observations. A dataset build deterministically projects each fact pair to one effective interval and pins
the `data_gaps` max row ID in the same immutable manifest as quotes, ticks, metadata and settlement
truth. Before calculating features, each decision checks the exact Kalshi quote freshness interval
and the primary-underlying 300-second lookback plus configured age allowance. Only overlapping
decision rows are rejected; later rows after recovery remain eligible. Restart and broad runtime
stall gaps retain distinct machine-readable reasons. Missing or stale required inputs and missing
market sides are also rejected without filling zeroes.

No gap is repaired with forward fill, backward fill, interpolation, synthetic prices, or a later
backfill that was unavailable at decision time. Kalshi finalized settlement remains label-only.
The same typed readiness boundary returns only `PASS` or `DATA_UNAVAILABLE` for future live
inference when a required source is stale, disconnected, inside a gap, lacks lookback, or has no
synchronized orderbook.
