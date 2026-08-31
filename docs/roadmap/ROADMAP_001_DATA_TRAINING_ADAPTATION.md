# ROADMAP 001 — Global data, training authority, H2 materialization, and adaptive learning

Status: **DESIGN_REFERENCE / NON-CURRENT / NOT IMPLEMENTED BY THIS DOCUMENT**

This volume records the approved data/training direction for LIVE15. It is intentionally detailed. It does not itself authorize training, holdout access, model promotion, Production deployment, service restart, retention destruction, or Production writes.

## 1. Global-data-first training authority

Formal current research, factor search, retraining, Challenger generation, calibration, and model selection must use the authorized global research universe rather than Dataset v1/v2 partitions.

Canonical source hierarchy:

- **H0** — LIVE15 Recorder plus replay-verified/authorized cold archive. This is the preferred current-regime reality source for drift, Challenger validation, and promotion checks.
- **H1** — official Kalshi historical markets/trades/candles/settlement evidence. It is authoritative for those historical semantics but must not be represented as full historical L2.
- **H2** — DepthFeed historical L2 only for ranges and capabilities whose provider identity, timestamps, book semantics, and overlap with H0 have been validated.

The current-training chain should converge on:

`Research Data Authority -> ResearchUniverseSnapshot -> CanonicalEvidenceSnapshot -> immutable Training Snapshot -> trainer`

H0/H1/H2 must never be blindly concatenated. Equivalent observations follow deterministic source precedence; exact duplicates are deduplicated; semantic conflicts are quarantined rather than arbitrarily selected. Every usable row retains provider/source identity, provenance tier, timestamp semantics, quality class, gap/quarantine state, and experiment/holdout boundary.

## 2. Dataset v1/v2 policy

Dataset v1 and Dataset v2 remain immutable audit/reproduction artifacts. They are **not** current research-history stores and must not be normal inputs to current training.

Required long-term behavior:

- normal current trainer APIs do not accept Dataset v1/v2 as ordinary history inputs;
- factor search, Auto-RD, continuous retraining, Challenger generation, current calibration, and current model selection cannot silently read them;
- any legacy command that uses them must require an explicit `--reproduction-only` or equivalent typed boundary;
- Dataset v1 revealed final-test information cannot be reused to select vNext features, horizons, thresholds, assets, architecture, calibration, or regimes;
- Dataset v2 frozen holdout remains `UNREVEALED_FROZEN`; its payload, labels, features, predictions, and performance remain inaccessible to development research;
- external/global evidence sharing the frozen holdout event/time identity must be excluded from development selection.

Do not physically delete the immutable artifacts merely to prevent accidental use. The stronger fix is architectural: current-training code should have no ordinary path that can consume them.

## 3. Immutable Training Snapshots

The global research universe is allowed to grow continuously as Recorder/archive/H1/H2 evidence arrives, but a running training job must never train against a moving source.

Each real training run freezes an immutable Training Snapshot containing at least:

- canonical evidence identity/hash;
- source tiers/ranges included;
- experiment cutoff;
- event/time coverage;
- explicit exclusions for holdout identities, gaps, quarantine, conflicts, stale/unavailable evidence;
- feature/label schema versions;
- training code Git SHA;
- weighting policy and parameters;
- split/purge/embargo policy;
- random seeds/hyperparameters where applicable.

A later retraining run creates a new snapshot and a new Challenger; it does not mutate the previous snapshot or artifact.

## 4. DepthFeed H2 must become a training source, not merely an extractor

The target H2 path is:

`DepthFeed API -> typed HistoricalL2Snapshot/HistoricalL2Tick -> H0 overlap validation -> canonical H2 evidence -> deterministic L2 materializer -> leakage-safe microstructure/sequence representation -> model-family training_preflight`

A successful API response alone is not H2 training readiness.

### 4.1 Typed representation

Historical L2 examples must preserve at minimum:

- provider and provenance tier;
- ticker and event/window identity;
- source/receive/as-of timestamp semantics;
- sequence identity where real sequence semantics exist;
- YES/NO sides and book levels;
- deterministic level ordering;
- gap state and quality class;
- source artifact/content hash;
- experiment cutoff and holdout exclusion state.

Settlement/result fields are forbidden from predictive inputs.

### 4.2 Snapshot materialization

Where real snapshots are available, deterministic materialization may derive bounded microstructure features such as:

- best executable-side book levels where semantics permit;
- spread;
- depth per side/level;
- imbalance;
- multi-level depth aggregates;
- price-distance-weighted depth;
- concentration/slope where mathematically and semantically valid.

Missing levels must remain missing; no synthetic depth may be invented.

### 4.3 Sequence materialization

Event-local sequences must be chronological, bounded to one event/window, and end at the decision/as-of time. No future-nearest join, forward fill, backward fill, interpolation, later backfill, or cross-event sequence is allowed.

Snapshot-only capability and true tick/delta capability remain separate. Snapshots must never be relabeled as replayable deltas. If real ticks/deltas are unavailable, the correct state is an explicit delta/sequence limitation, not synthetic readiness.

### 4.4 H0 overlap validation

Before H2 becomes training-authorized, bounded overlapping H0 evidence must validate, where applicable:

- market/ticker identity;
- timestamp/availability semantics;
- YES/NO side semantics;
- price/size representation;
- level ordering;
- duplicate behavior;
- conflicts;
- snapshot meaning;
- sequence/delta meaning.

H0 retains precedence for equivalent observations. Conflicts fail closed/quarantine.

### 4.5 Provider limitations

HTTP 402/429 or other plan/rate capability boundaries must not be hidden or retried indefinitely. Saved real payload fixtures can support parser/materializer regression tests; synthetic fixtures can test mechanics only. Synthetic data never counts as research/training evidence.

Readiness must distinguish:

- `CODE_PIPELINE_READY`
- `REAL_H2_DATA_READY`
- snapshot readiness
- delta/tick readiness
- sequence readiness
- model-family readiness.

MLPLOB, DeepLOB, and TLOB must each receive their own `READY/PARTIAL/BLOCKED` preflight result based on actual required capability.

## 5. Anti-leakage and anti-overfit invariants

All current/future training paths retain the existing hard safeguards:

- source and receive timestamps are as-of decision time;
- no settlement/result/outcome field enters predictive features;
- missing/stale/gapped data fails closed;
- no forward/back fill, interpolation, future-nearest join, or hidden zero imputation;
- whole-event chronological splits, never random row splits as formal evaluation;
- event groups cannot cross train/validation/test;
- purge/embargo remains contract-derived (currently 600 seconds unless the declared contract changes);
- normalization and calibration fit only on allowed training folds;
- holdout is never used for hyperparameter/model/factor selection;
- factor work requires ablation, FDR/redundancy controls, multi-day/multi-asset/regime evidence;
- promotion requires fresh forward OOS evidence, not only backtest/development metrics;
- after-cost behavior, calibration, drawdown, profit factor, trade count, and consistency matter more than gross return alone.

## 6. Fast inference loop vs slow learning loop

LIVE15 should not retrain model weights on every incoming tick. Market inputs may update predictions every second/event, but supervised labels and trustworthy learning evidence arrive more slowly.

### Fast loop

`new data -> features -> current Champion experts -> Router/uncertainty -> EV decision -> Hard Risk`

This loop may run event-driven/second-scale and must not mutate model weights.

### Slow learning loop

`new settled/authorized evidence -> RDA/CES -> drift monitor -> immutable Training Snapshot -> Challenger training -> chronological validation -> anti-overfit/after-cost gates -> Shadow/Paper forward -> Champion comparison -> promote/reject`

Retraining may be scheduled, triggered by sufficient new events, or requested by validated drift. Drift is a request to research/retrain; it never directly replaces the Champion.

## 7. Sample weighting

Do not simply delete old history because it is old, and do not treat all historical evidence as equally representative of current conditions.

Approved weighting direction:

`sample_weight = recency_weight * regime_similarity_weight * data_quality_weight`

### Recency

Recent evidence receives higher weight; older evidence decays gradually rather than disappearing solely by age.

### Regime similarity

Older evidence from a market regime similar to the current regime may deserve more weight than newer but structurally dissimilar evidence. Regime dimensions may include trend/range, volatility, reversal risk, liquidity/spread, and other validated descriptors.

### Data quality

Gap-free, synchronized, high-quality authoritative evidence receives higher effective trust than partial/degraded evidence. Quality weighting must not convert invalid/unavailable evidence into valid evidence; hard invalidity remains excluded.

Rare but important long-term regimes (crash, volatility spike, liquidity collapse, unusual reversal behavior) should be preserved at lower weight rather than erased.

## 8. Drift-safe retraining

`ADAPT-001` should implement explicit drift monitoring for at least:

- feature/distribution drift;
- prediction/calibration drift (e.g. probability bins no longer calibrate);
- after-cost performance drift;
- drawdown/hit-rate/log-loss/Brier deterioration where appropriate;
- regime-frequency/coverage shifts.

Drift thresholds must be frozen/tested, not improvised after observing outcomes.

## 9. Champion/Challenger and rollback

Every candidate is an immutable Challenger with full lineage. Promotion requires evidence, not merely a successful fit.

Required capabilities:

- unique model/release ID and artifact hash;
- dataset/Training Snapshot identity;
- training code SHA;
- metrics and cost assumptions;
- current promotion state;
- stable Champion comparison;
- rollback to prior valid Champion;
- failed/partial artifacts never masquerade as valid models.

A future optional architecture may maintain a **Stable Champion** (longer-memory, lower turnover) and an **Adaptive Challenger/Champion** (more current-regime emphasis), with Router-controlled weighting only after sufficient forward validation. This must not become an excuse for unvalidated online weight mutation.

## 10. Historical design task identifiers

- `DATA-GLOBAL-001` — make global RDA/CES/Training Snapshot the only formal current-training authority.
- `H2-TRAIN-001` — complete DepthFeed L2 validation and training materialization.
- `ADAPT-001` — drift, recency/regime/quality weighting, triggered/scheduled retraining, Champion/Challenger, rollback.

This section preserves design provenance; it is not a current planned-task list. Current status is
owned by `PROJECT_PROGRESS.md`, current ordering by
`docs/project-brain/plan/current-roadmap.md`, and training remains separately gated.
