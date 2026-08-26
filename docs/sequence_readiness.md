# FLOW-005B — Causal Historical Sequence Readiness

Status: `DEVELOPMENT RESEARCH ONLY`; no model training or runtime wiring was performed.

The representation is built from the ignored HIST-003 SQLite store identified by
`historical-research-f2d529adfb95080971becdaf`.  A sequence is event-local, ends at its
decision timestamp, uses only completed observations whose source/receive timestamps are at or
before that decision, and requires an exact future observation for the target.  Future-nearest
joins, interpolation, forward/back-fill, and zero substitution are forbidden.  Normalization is
declared `train_fold_only`; the existing HIST-003 expanding walk-forward plan uses whole-event
groups and a 600-second purge/embargo.

## FLOW-005B1 measured evidence

| Item | Evidence |
|---|---:|
| HIST-003 markets | 59,056 |
| Official trades retained as provenance | 886,454 |
| Official 1-minute candles | 5,242 |
| Assets | BTC, ETH, SOL, XRP, DOGE, BNB, HYPE |
| Causal 1-minute sequences | 37,118 (prior proof) |
| Causal 5s / 15s / 30s sequences | 13,632 / 14,597 / 8,943 |
| Official trade rows used | 886,454 across 350 markets |
| Independent sequence UTC days | 1 (`2026-06-25`) |
| Available walk-forward folds | 0 |
| Target tolerance | 15 seconds, future trade at/after target only |
| Sequence exclusions | 9,418 insufficient-history; 31,292 missing source bucket |

The 1-minute archive cannot prove a 30-second target. FLOW-005B1 now provides a bounded causal
trade-derived representation, but its detail rows are all concentrated on one independent UTC day.
Therefore the sequence gate remains:

`SEQUENCE_PARTIAL_MORE_DATA_OR_REPRESENTATION_NEEDED`

Microstructure readiness is kept separate. The bounded seven-day DepthFeed attempt returned HTTP
429 on the first snapshot query after metadata discovery; the known tick probe remains HTTP 402:

- `MICROSTRUCTURE_SNAPSHOT_BLOCKED` — no snapshot rows were materialized after the bounded 429.
- `MICROSTRUCTURE_DELTA_BLOCKED` — the bounded tick/delta capability probe returned HTTP 402;
  snapshots are never represented as deltas.

Commodity sequence research remains
`HISTORICAL_COMMODITY_SEQUENCE_UNAVAILABLE_IN_CURRENT_HIST003_ARTIFACT`.

The deterministic machine-readable records are `docs/flow005b1_evidence_report.json` and the
ignored `data/research/flow005b1/trade_sequence_manifest.json`. Dataset v2,
its `UNREVEALED_FROZEN` holdout, Recorder, Paper, Production, and Hard Risk were not touched.
