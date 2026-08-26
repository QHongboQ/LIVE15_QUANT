# FLOW-005B-PREP — Completion Evidence

This milestone is a development/research preparation change.  It pins upstream provenance and
builds a causal sequence-readiness representation; it does not train or promote a model.

| Area | Result |
|---|---|
| Protected main base | `ca7f7765649e9ae3215867d8f88f458749ebabe0` |
| Historical dataset | `historical-research-f2d529adfb95080971becdaf` |
| Raw evidence | 59,056 markets; 886,454 trades; 5,242 1m candles; 7 crypto assets |
| Causal sequence samples | 37,118 1m candle sequences; 0 fabricated trade-event sequences |
| Independent sequence days | 1 |
| Sequence gate | `SEQUENCE_PARTIAL_MORE_DATA_OR_REPRESENTATION_NEEDED` |
| Microstructure snapshot | `MICROSTRUCTURE_SNAPSHOT_NOT_MATERIALIZED` |
| Microstructure delta | `MICROSTRUCTURE_DELTA_BLOCKED` (provider capability/plan HTTP 402) |
| Commodity sequence path | `HISTORICAL_COMMODITY_SEQUENCE_UNAVAILABLE_IN_CURRENT_HIST003_ARTIFACT` |
| Fold plan | 8-fold expanding plan; 30 train / 7 validation days; 600s purge/embargo; plan only |
| Model training | Not performed |
| Dataset v2 / holdout | Untouched / `UNREVEALED_FROZEN` |
| Recorder/Paper/Production/Hard Risk | Unchanged |
| Checker | `PASS` |

The exact model-family pins and license evidence are in `docs/model_upstream_pins.json`.  EarnHFT
has no license file at its pinned revision and is therefore documentation-only pending review.
The sequence schema, exclusion reasons, per-asset counts, and readiness decisions are in
`docs/sequence_readiness.json` and `docs/sequence_readiness.md`.
