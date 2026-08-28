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

## FLOW-005B / FLOW-005B1 sequence evidence

| Evidence layer | Status | Measured evidence / blocker |
|---|---|---|
| Causal 1-minute path sequences | `SEQUENCE_PARTIAL_MORE_DATA_OR_REPRESENTATION_NEEDED` | 37,118 exact-target sequences; one independent sequence day; 30s cannot be proved by 1m candles |
| Trade-derived sub-minute sequences | `SEQUENCE_PARTIAL_MORE_DATA_OR_REPRESENTATION_NEEDED` | Causal 5s/15s/30s manifests: 13,632 / 14,597 / 8,943 sequences; 350 events but one independent UTC day and zero available walk-forward folds |
| Microstructure snapshots | `MICROSTRUCTURE_SNAPSHOT_BLOCKED` | Bounded seven-day attempt hit HTTP 429 after metadata discovery; no repeated retry and no snapshots materialized |
| Microstructure deltas | `MICROSTRUCTURE_DELTA_BLOCKED` | provider capability/plan probe returned HTTP 402; snapshots are not deltas |
| Commodity historical sequences | `BLOCKED` | `HISTORICAL_COMMODITY_SEQUENCE_UNAVAILABLE_IN_CURRENT_HIST003_ARTIFACT` |

The fold plan remains plan-only: expanding whole-event groups, 30 train days, 7 validation days,
7-day step, and a 600-second purge/embargo. The actual sequence detail has zero available folds,
so no sequence model was trained and Path training remains locked. H2 raw ticks remain
`H2_DELTA_UNAVAILABLE_PLAN_LIMIT`; TLOB remains `TLOB_BLOCKED`.

All statuses are readiness decisions, not profitability or promotion decisions. Dataset v2
holdout remains `UNREVEALED_FROZEN` and is not an allowed source.

## H2-TRAIN-001 materialization boundary

`H2-TRAIN-001` is `PARTIAL`. The offline typed snapshot materializer, event-local snapshot
sequence builder, H0/H2 conflict-quarantine comparison, and family-specific MLPLOB/DeepLOB/TLOB
preflight capability fields are implemented. This is `CODE_PIPELINE_READY`, not real H2 data
readiness: no real snapshot has passed H0 overlap validation, and delta/tick remains
`H2_DELTA_SEQUENCE_UNAVAILABLE`. Snapshot, delta, snapshot-sequence, delta-sequence, and
microstructure-training-ready days remain separate in `CanonicalEvidenceSnapshot`.

## DATA-READINESS-001 canonical gate

Future readiness must start from `CanonicalEvidenceSnapshot`; the legacy scalar evidence helper is
compatibility-only and must not be used as global truth. The gate preserves separate H0/H1/H2 path,
snapshot, and delta days, rejects first-N/API-order temporal sampling, and blocks unexplained
sample-to-source coverage collapse as `EVIDENCE_RECONCILIATION_REQUIRED`. H0 remains the preferred
fresh current-regime validation source even when H1/H2 improve historical research metrics.

No model training is authorized by this gate. A model-family request must pass
`training_preflight(...)` with an experiment cutoff and holdout isolation before any future
tournament or AUTO-ML run.

## EVID-RECON-001 update

The prior one-day H1 sequence evidence was traced to first-N-per-asset temporal concentration,
not to a lack of historical days. A deterministic 7-day stratified sample now supplies 10,653
causal sequences across 144 event identities and four chronological folds (`purge_embargo_seconds`
`600`). Accordingly, structured path evidence is now `SEQUENCE_READY_FOR_BOUNDED_MODEL_TRAINING`
for offline bounded work only. H0's 6-day live-native layer remains supplementary because its
trade-derived path rows are not materialized. H2 remains `MICROSTRUCTURE_SNAPSHOT_BLOCKED` with
`H2_DELTA_UNAVAILABLE_PLAN_LIMIT`; no sequence or microstructure model was trained.
