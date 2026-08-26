# EVID-RECON-001 — Layered evidence reconciliation

Status: `DEVELOPMENT RESEARCH ONLY`; no model was trained and no runtime or Dataset v2 state was changed.

## Result

The previous one-day H1 sequence result was not evidence that LIVE15 lacked multiple days. It was
caused by `HIST003_DETAIL_CAP_FIRST_N_PER_ASSET_TEMPORAL_CONCENTRATION`: the old bounded detail
path took the first N markets per asset in API/storage order, and all 350 selected markets traded
on `2026-06-25`. This task replaces that selection for reconciliation with deterministic, evenly
spaced UTC-day × asset × event sampling.

| Layer | Measured evidence | Readiness |
|---|---:|---|
| H0 live-native Recorder | 6 UTC days, 4,350 settlement identities, 1,460,580 quote rows, 3,212,987 underlying rows, 25,509 L2 checkpoints | supplementary real evidence; H0 trade/path sequence not materialized |
| H1 official Kalshi | 147 markets across 7 selected UTC days, 201,424 trades, 2,187 candles, 0 conflicts | `SEQUENCE_READY_FOR_BOUNDED_MODEL_TRAINING` |
| H2 DepthFeed | 50 discovered markets; 0 usable snapshots; tick/delta plan limit | `MICROSTRUCTURE_SNAPSHOT_BLOCKED` / `H2_DELTA_UNAVAILABLE_PLAN_LIMIT` |

H1 produced 10,653 causal sequences (5s/15s/30s grids), 144 independent event identities, and four
expanding chronological folds with a 600-second purge/embargo. The seven H1 source days are the
path-ready evidence; H0's six days remain a separate live-native layer and are not silently blended
into H1 trade-derived sequences. No final-test, holdout, settlement-as-feature, future-nearest join,
interpolation, or fill was introduced.

The H2 probe was bounded: a provider timestamp contract error was recorded, then a later request
was rate-limited with HTTP 429. The probe did not coerce naive timestamps or retry without bound.

Raw databases, manifests, checkpoints, and sequence rows remain under ignored
`data/research/evid_recon001/`; only this lightweight report and the reconciliation implementation
are tracked.
