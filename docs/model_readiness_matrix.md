# FLOW-005A — Model Readiness Matrix

| Model family | Role | Status | Allowed inputs | Blocker |
|---|---|---|---|---|
| `path_expert_foundation` | Path Expert contract | `READY` | H1 official history, H0 featureized inputs | complete sequence/detail coverage for full training |
| `terminal_baseline` | Terminal / decision expert | `READY` | H1 official history, H0 featureized inputs | offline development only |
| `microstructure_expert` | TLOB / DeepLOB / MLPLOB | `PARTIAL` | H0 Recorder L2, H2 snapshots/ticks where available | H2 ticks unavailable; commodity L2 unverified |
| `path_sequence_challengers` | TimeXer/PatchTST/iTransformer/TimeMixer/TimesNet/DLinear | `RESEARCH_ONLY` | approved H1/H0 sequences | no broad training in FLOW-005A |
| `rolling_research_orchestration` | Qlib reference | `RESEARCH_ONLY` | offline manifests | not a runtime dependency |
| `hierarchical_architecture_reference` | EarnHFT reference | `ARCHITECTURE_ONLY` | expert outputs/regime descriptors | router and execution layers deferred |
| `router_policy_foundation` | future router/policy interface | `ARCHITECTURE_ONLY` | expert outputs/regime descriptors | no runtime wiring or policy learning |
| `microstructure_commodity` | commodity microstructure | `BLOCKED` | H0 only if independently verified | no verified historical commodity L2 |

## FLOW-005B sequence evidence

| Evidence layer | Status | Measured evidence / blocker |
|---|---|---|
| Causal 1-minute path sequences | `SEQUENCE_PARTIAL_MORE_DATA_OR_REPRESENTATION_NEEDED` | 37,118 exact-target sequences; one independent sequence day; 30s cannot be proved by 1m candles |
| Trade-derived sub-minute sequences | `UNAVAILABLE_BY_CONTRACT` | 886,454 trades retained as provenance; no fixed aggregation or completeness contract was invented |
| Microstructure snapshots | `MICROSTRUCTURE_SNAPSHOT_NOT_MATERIALIZED` | DepthFeed probe is not historical archive evidence |
| Microstructure deltas | `MICROSTRUCTURE_DELTA_BLOCKED` | provider capability/plan probe returned HTTP 402; snapshots are not deltas |
| Commodity historical sequences | `BLOCKED` | `HISTORICAL_COMMODITY_SEQUENCE_UNAVAILABLE_IN_CURRENT_HIST003_ARTIFACT` |

The fold plan remains plan-only: expanding whole-event groups, 30 train days, 7 validation days,
7-day step, and a 600-second purge/embargo. No sequence model was trained.

All statuses are readiness decisions, not profitability or promotion decisions. Dataset v2
holdout remains `UNREVEALED_FROZEN` and is not an allowed source.
