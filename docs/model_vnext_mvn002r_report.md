# MVN-002R — Dataset v2 structured path re-evaluation

Status: `DEVELOPMENT ONLY`  
Outcome: `NO_ROBUST_PATH_EDGE_YET`

## Frozen experiment identity

- Dataset: `live15-dataset-v2-4bb4934bf328b6b024ff`
- Build hash: `4bb4934bf328b6b024ff4183df134c481d962a041dc6ae760a3816d3c5228113`
- Cutoff: `2026-08-25T19:35:14.898895+00:00`
- Rows used: train `18,507`; validation `3,801`; independent events `3,489`; UTC days `6`.
- Holdout: `UNREVEALED_FROZEN`; holdout rows/labels loaded or scored: `false`.
- Purge/embargo: `600s`; LeakageChecker: `PASS`.
- Fixed seed/config: `20260826`; XGBoost depth `3`, `40` rounds, no sweep.

The runner skips `test` records before JSON decoding. It reads only the immutable Dataset v2
JSONL artifacts and does not regenerate or mutate them.

## Targets and models

All declared horizons remain in the report. Valid train/validation targets were available only at
30s (`1,077`), 60s (`4,798`), 120s (`12,575`), 180s (`5,324`), and 300s (`5,249`). The 5s,
15s, and `window_end` targets remain explicitly unavailable (`future observation unavailable`)
and were not filled or interpolated. The 65 fixed candidates are A0 naive plus linear, logistic,
and XGBoost heads for A1–A4 across each available horizon.

## Validation summary

Best pooled validation candidate by MAE (not a promotion decision):

| Horizon | Candidate | MAE | Directional accuracy | LogLoss | Brier | ECE |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 30s | Logistic A2 | 0.0003241 | 0.5369 | 0.6802 | 0.2436 | 0.0331 |
| 60s | Logistic A4 | 0.0006070 | 0.5525 | 0.6910 | 0.2489 | 0.0464 |
| 120s | Logistic A3 | 0.0010183 | 0.5166 | 0.6912 | 0.2490 | 0.0121 |
| 180s | Linear A3 | 0.0011904 | 0.5090 | 0.9404 | 0.3019 | 0.2054 |
| 300s | Naive A0 | 0.0015606 | 0.5310 | 0.8259 | 0.3054 | 0.2374 |

The complete per-fold, per-UTC-day, per-asset metrics and artifact identities are in
[`model_vnext_mvn002r_report.json`](model_vnext_mvn002r_report.json).

## Dataset v1 comparison and gates

The prior Dataset v1 XGBoost/A2 30s directional accuracy was `0.6529`; Dataset v2 is `0.4713`
under this fixed train/validation re-evaluation. The prior apparent 30s result therefore did not
replicate and is classified `WEAKENED_OR_REPLICATED`, not as fresh OOS evidence. XGBoost does not
show a stable advantage over naive/linear across horizons, folds, days, and assets.

The predeclared robust-edge criteria were: consistent positive advantage across chronological folds,
UTC days, and assets; advantage over naive and linear without isolated-fold selection; and no
probability-quality deterioration. Those criteria were not met. Six independent days are still
development evidence only. `MVN-003` remains `INSUFFICIENT_MICROSTRUCTURE_EVIDENCE`; sequence
models remain gated by `INSUFFICIENT_SEQUENCE_EVIDENCE`. No Paper/Shadow/Production wiring or
promotion was performed.
