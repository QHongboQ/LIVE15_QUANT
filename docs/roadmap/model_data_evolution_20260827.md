# LIVE15 approved model/data evolution roadmap — 2026-08-27

Status: **APPROVED DIRECTION / NOT IMPLEMENTED**.

This document records user-approved planning decisions discussed on 2026-08-27. It is a roadmap reference, not training authorization, deployment evidence, model promotion, or Production permission. Every item still requires a bounded task, tests, evidence, Checker/CI, and human gates where applicable.

## Global-data-first training

Formal current research and retraining should consume the authorized global research universe, not Dataset v1/v2 partitions:

`H0 Recorder + verified cold archive` + `H1 official Kalshi history` + `validated H2 DepthFeed L2` → `Research Data Authority` → `ResearchUniverseSnapshot` → `CanonicalEvidenceSnapshot` → immutable per-run Training Snapshot → trainer.

Dataset v1/v2 remain immutable audit/reproduction artifacts only. Normal current-training, AutoML, factor search, Challenger generation, recalibration, and adaptive retraining must not use them except through an explicit reproduction-only path. Do not delete them merely to hide bugs; structurally remove them from current-training authority.

Every actual training run freezes an auditable snapshot/hash so a moving Recorder/archive/history source cannot silently change an in-progress build.

## H2 / DepthFeed training materialization

DepthFeed is not complete merely because the API can return historical L2 objects. The required chain is:

DepthFeed API → typed `HistoricalL2Snapshot`/`HistoricalL2Tick` → bounded H0 overlap validation → canonical H2 evidence → deterministic L2 materializer → event-local microstructure/sequence examples → model-family training preflight.

Snapshot and true delta/tick capabilities must remain separate. Missing delta capability must never be fabricated from snapshots. H0 retains precedence for equivalent observations; conflicts quarantine rather than silently choosing H2.

## Multi-expert model architecture

The target system is layered rather than one monolithic 15-minute classifier:

1. **Terminal Expert** — `P(final YES)`.
2. **Path Expert** — short/medium path at 30s/1m/3m/5m.
3. **Microstructure Expert** — second-scale Kalshi book movement.
4. **Regime/Router Expert** — trend/range/volatility/reversal-risk context and expert weighting.
5. **Factor Alpha Expert** — guarded machine-generated factors.
6. **Uncertainty/Disagreement Expert** — calibration/confidence/model disagreement.
7. **EV Decision Engine** — converts expert state plus executable book/costs/position into one action.
8. **Hard Risk** — independent veto before execution.

Candidate order should remain evidence-driven. Structured logistic/XGBoost are sanity/baseline models. Causal TCN is the preferred first deep sequence challenger once the sequence gate passes. THUML/Time-Series-Library models (for example TimeXer, PatchTST, iTransformer, TimeMixer, TimesNet, DLinear) form a challenger pool rather than a Production dependency. For L2, compare MLPLOB → DeepLOB → TLOB on identical LIVE15 evidence. EarnHFT is a hierarchical-router architecture reference, not an execution dependency.

## Autonomous factor/model R&D

Build `AUTO-RD-001` as a guarded research factory inspired by AlphaGPT and RD-Agent(Q), while keeping LIVE15 data, leakage, promotion, and risk authority independent.

Candidate loop:

Generate factor/model → LeakageChecker → chronological whole-event walk-forward → purge/embargo → FDR/redundancy for factors → after-cost metrics → multi-day/asset/regime stability → fresh Shadow/Paper forward → promote/reject.

External projects are references, not Production controllers. New candidates may be generated continuously offline, but no candidate may replace the Champion from backtest/development evidence alone.

## Continuous adaptation and drift-safe retraining

Separate two time scales:

- **Fast inference loop:** refresh features/predictions/decisions every second or event as valid data arrives; do not mutate model weights tick-by-tick.
- **Slow learning loop:** after valid labels/evidence arrive, scheduled or drift-triggered jobs build immutable Challengers.

Training weights should combine recency, regime similarity, and data quality rather than simply deleting old history. Older rare regimes may keep lower but nonzero influence.

Add feature drift, calibration/prediction drift, and after-cost performance drift. Drift may request retraining; it must not directly replace the Champion. Support Champion/Challenger comparison, rollback, and later evaluate Stable Champion + Adaptive Challenger routing.

## First-live decision surface

The theoretical v3 action set contains ten actions, but the first Paper-forward / tiny-live surface should remain conservative:

- `BUY_YES`
- `BUY_NO`
- `HOLD`
- `TAKE_PROFIT`
- `CUT_LOSS`
- `CLOSE`
- `HOLD_TO_SETTLEMENT`
- `DATA_UNAVAILABLE`

Keep `ADD` and `REDUCE` disabled until separately validated forward evidence proves that position scaling improves after-cost performance without unacceptable risk. Hard Risk retains veto authority over every proposed action.

## Planned task IDs

- `DATA-GLOBAL-001` — global H0/H1/H2 current-training authority and legacy Dataset isolation.
- `H2-TRAIN-001` — DepthFeed L2 training materialization and family-specific readiness.
- `AUTO-RD-001` — autonomous factor/model Challenger factory.
- `ADAPT-001` — drift-safe adaptive retraining, weighting, Champion/Challenger, rollback.
- `MODEL-ENSEMBLE-001` — multi-expert model/router evolution.
- `DEC-ACT-001` — conservative first-live eight-action surface.

These roadmap items do not supersede the current gated sequence (`DEP-001` → `ST-005` → `TRN-001`) and do not authorize training, deployment, holdout access, or Production writes.