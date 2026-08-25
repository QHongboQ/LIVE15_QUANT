# FACTOR-001R — Bounded Symbolic Factor Evaluation

Status: **DEVELOPMENT EVIDENCE ONLY**; conclusion: **NO_ROBUST_SYMBOLIC_FACTOR_SIGNAL**.

Experiment `5df0767f2961e02caad4a551` uses Dataset v2 `live15-dataset-v2-4bb4934bf328b6b024ff` (build `4bb4934bf328b6b024ff4183df134c481d962a041dc6ae760a3816d3c5228113`), frozen train/validation rows only. Holdout state is `UNREVEALED_FROZEN` and holdout access is `False`.

The candidate budget was frozen before metric evaluation at 96: F0=16, F1=32, F2=24, F3=12, F4=12. No candidate-budget expansion or search-until-success rerun was performed.

## Acceptance and multiple testing

Selection uses validation metrics, BH-FDR at alpha=0.1, minimum coverage=0.5, at least 3 independent days, at least 2 assets, sign consistency >=0.60, and a predeclared +0.01 absolute Rank IC advantage over the best primitive. These gates are development gates, not production criteria.

## Best validation result by horizon

| Horizon | Factor | Family | Spearman IC | Coverage | Status |
|---:|---|---|---:|---:|---|
| 30s | `34a54346d7ef` | F3 | -0.2022556577502961 | 0.643 | development-only |
| 60s | `09cb9bfd3ce8` | F2 | -0.0967190956348372 | 1.000 | development-only |
| 120s | `09cb9bfd3ce8` | F2 | -0.1246679774202472 | 0.784 | development-only |
| 180s | `44560459f59c` | F3 | -0.22608081634615515 | 0.531 | development-only |
| 300s | `37c33814a9b1` | F3 | -0.19531507151475644 | 0.526 | development-only |

The compact tracked JSON preserves lineage, formulas/IDs, aggregate metrics, rejection counts, and ranking summaries. Full Factor Zoo records, per-horizon metrics, per-day/per-asset stability, FDR values, and rankings are emitted to the ignored regenerable detail artifact when --full-output-json is supplied. No factor is wired into a model or runtime.

Sequence readiness remains `INSUFFICIENT_SEQUENCE_EVIDENCE`; FACTOR-002 reward learning and any trading optimization remain deferred.
