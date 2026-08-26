# FLOW-005B — Causal Historical Sequence Readiness

Status: `DEVELOPMENT RESEARCH ONLY`; no model training or runtime wiring was performed.

The representation is built from the ignored HIST-003 SQLite store identified by
`historical-research-f2d529adfb95080971becdaf`.  A sequence is event-local, ends at its
decision timestamp, uses only completed observations whose source/receive timestamps are at or
before that decision, and requires an exact future observation for the target.  Future-nearest
joins, interpolation, forward/back-fill, and zero substitution are forbidden.  Normalization is
declared `train_fold_only`; the existing HIST-003 expanding walk-forward plan uses whole-event
groups and a 600-second purge/embargo.

## Measured evidence

| Item | Evidence |
|---|---:|
| HIST-003 markets | 59,056 |
| Official trades retained as provenance | 886,454 |
| Official 1-minute candles | 5,242 |
| Assets | BTC, ETH, SOL, XRP, DOGE, BNB, HYPE |
| Causal 1-minute sequences | 37,118 |
| Trade-event sequences | 0 (not contractually materialized) |
| Independent sequence UTC days | 1 |
| 30s / 60s / 120s / 180s / 300s candle targets | 0 / 11,685 / 10,317 / 8,948 / 6,168 |
| Excluded candidates | 67,162 |

The 1-minute archive cannot prove a 30-second target.  The irregular official trade stream is
kept as source evidence but is not silently aggregated into a sequence; a bounded trade-derived
representation must be specified in a later task.  Therefore the sequence gate is:

`SEQUENCE_PARTIAL_MORE_DATA_OR_REPRESENTATION_NEEDED`

Microstructure readiness is kept separate:

- `MICROSTRUCTURE_SNAPSHOT_NOT_MATERIALIZED` — a DepthFeed snapshot probe is not historical
  archive evidence.
- `MICROSTRUCTURE_DELTA_BLOCKED` — the bounded tick/delta capability probe returned HTTP 402;
  snapshots are never represented as deltas.

Commodity sequence research remains
`HISTORICAL_COMMODITY_SEQUENCE_UNAVAILABLE_IN_CURRENT_HIST003_ARTIFACT`.

The deterministic machine-readable record is `docs/sequence_readiness.json`.  Dataset v2,
its `UNREVEALED_FROZEN` holdout, Recorder, Paper, Production, and Hard Risk were not touched.
