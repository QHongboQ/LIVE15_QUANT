# FLOW-005B1 — Sequence and Microstructure Evidence

Status: **DEVELOPMENT RESEARCH ONLY**. No model training, promotion, Dataset v2 mutation,
holdout access, Recorder change, Paper activation, or Production write occurred.

## Path evidence

The source is the ignored HIST-003 SQLite store behind
`historical-research-f2d529adfb95080971becdaf`. The builder uses event-local causal buckets at
5s, 15s, and 30s resolutions, a 120s lookback, and a predeclared 15s future-target tolerance.
Only trades with timestamps at or before the bucket decision enter features. Missing buckets are
excluded with typed reasons; no fill, interpolation, synthetic trade, or future-nearest join is
used.

| Item | Measured result |
|---|---:|
| Official trade rows | 886,454 |
| Markets with trades | 350 |
| Independent UTC days | 1 (`2026-06-25`) |
| Independent events | 350 |
| Assets | 7 crypto assets |
| 5s / 15s / 30s sequences | 13,632 / 14,597 / 8,943 |
| 30s / 60s / 120s / 180s / 300s targets | represented with explicit availability per row |
| Available sequence folds | 0 |
| Path readiness | `SEQUENCE_PARTIAL_MORE_DATA_OR_REPRESENTATION_NEEDED` |

Per-asset sequence counts (all grids combined) are BNB 3,072; BTC 11,461; DOGE 3,446;
ETH 7,678; HYPE 4,056; SOL 3,753; XRP 3,706. The only sequence day is
`2026-06-25` with 37,172 rows, so there is no per-day consistency evidence yet.

Per-grid target availability is recorded in `docs/flow005b1_evidence_report.json` and the ignored
`trade_sequence_manifest.json`. The 886k row count is not treated as independent evidence: all
detail trades are concentrated in one UTC day, so the sequence gate remains closed for bounded
model training.

## Microstructure evidence

The authorized free-plan attempt was bounded to a seven-day window and at most three selected
asset snapshot calls. Metadata discovery returned 50 rows, but the first bounded snapshot request
returned HTTP 429; no repeated retry was made. The known raw tick limitation remains
`H2_DELTA_UNAVAILABLE_PLAN_LIMIT` from the prior HTTP 402 probe. No snapshots were materialized,
so the resulting statuses are:

- `MICROSTRUCTURE_SNAPSHOT_BLOCKED`
- `H2_DELTA_UNAVAILABLE_PLAN_LIMIT`
- `TLOB_BLOCKED`
- `BLOCKED_NO_SNAPSHOTS` for snapshot-compatible baselines

Trades were never treated as L2. H2 provenance remains separate from H0 Recorder evidence.

## Reproducibility and artifacts

- Historical dataset: `historical-research-f2d529adfb95080971becdaf`
- Sequence manifest: `flow005b1-sequence-38e032fa554d45cd4225aea4`
- Code SHA: `748c20946fb395eae7c17036b68be50adcc0755c`
- Ignored artifacts: `data/research/flow005b1/trade_sequence_rows.jsonl`,
  `trade_sequence_manifest.json`, `depthfeed_snapshot_manifest.json`, and the combined report.

The next eligible work is more independent H1 trade detail coverage (or a separately approved
historical source) before any Path sequence tournament. Microstructure training is not unlocked.
