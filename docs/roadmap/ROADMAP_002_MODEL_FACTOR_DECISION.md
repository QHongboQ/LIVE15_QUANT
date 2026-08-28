# ROADMAP 002 — Multi-expert models, autonomous factors, Router, and decision surface

Status: **USER-APPROVED DIRECTION / NOT IMPLEMENTED BY THIS DOCUMENT**

This volume records the approved model/factor/decision architecture. It does not itself authorize training, model promotion, Paper/Shadow activation, Production deployment, execution, Hard Risk changes, or Production writes.

## 1. Multi-timescale problem framing

LIVE15 forecasts a 15-minute settlement outcome, but the market can change materially every second. The model system must therefore separate terminal-horizon probability from second/minute-scale tradability and microstructure rather than forcing one model to solve every horizon.

Approved layered expert system:

1. **Terminal Expert** — estimates `P(final YES)` using information available at decision time.
2. **Fast Microstructure Expert** — models second-scale Kalshi order-book/contract-price behavior.
3. **Path Expert** — predicts underlying path over short horizons such as 30s, 1m, 3m, and 5m.
4. **Regime / Router Expert** — identifies trend/range/volatility/reversal/liquidity context and controls which expert evidence deserves more weight.
5. **Factor Alpha Expert** — supplies validated automatically discovered factors/signals.
6. **Uncertainty / Disagreement Expert** — measures confidence, expert disagreement, calibration quality, missing capability, and whether the system should abstain.

Expert outputs feed a Router/uncertainty layer, then an explicit after-cost EV Decision Engine, then independent Hard Risk, then execution. Models do not directly submit orders.

## 2. Model selection philosophy

The objective is not to choose a fashionable single “best model.” LIVE15 should maintain simple sanity/baseline models and progressively stronger Challengers on identical chronological evidence. Promotion depends on LIVE15 after-cost forward evidence, not architecture popularity or external leaderboard ranking.

## 3. Terminal Expert

The terminal layer answers the settlement question:

`P(final underlying/contract outcome satisfies YES | information available at decision time)`

Existing certified/structured terminal candidates remain the baseline lineage until fresh Challenger evidence beats them. Terminal probability is distinct from market-implied probability, executable bid/ask, and short-horizon path movement.

The terminal expert must remain calibrated and must not consume settlement-derived features.

## 4. Path Expert model order

Approved first-order sequence:

1. structured logistic/linear sanity baselines where relevant;
2. **structured multi-horizon XGBoost** as the first practical nonlinear path baseline;
3. **causal TCN** as the preferred first deep sequence Challenger once sequence readiness passes;
4. Time-Series-Library/THUML candidate pool only after evidence and representation justify broader sequence experiments.

Useful THUML/Time-Series-Library candidate references include TimeXer, PatchTST, iTransformer, TimeMixer, TimesNet, and DLinear. The library is a research/model-source reference, not an automatic Production dependency and not evidence that a Transformer must win.

Current/previous `NO_ROBUST_PATH_EDGE_YET` development outcomes remain evidence that simple path edge has not yet been proven; they are not permission to skip baseline comparisons or to tune against revealed/frozen tests.

## 5. Microstructure Expert model order

Historical/live L2 capability must be validated first through `H2-TRAIN-001` and/or H0 native order-book evidence.

Approved comparison order on identical LIVE15 training/evaluation evidence:

1. **MLPLOB** — simple sanity baseline;
2. **DeepLOB** — mature LOB baseline/reference family;
3. **TLOB** — Transformer-style LOB Challenger.

Do not assume TLOB wins because it is newer/more complex. All three must use identical leakage-safe event-local representations, costs, splits, and forward evidence appropriate to their capability.

Snapshot-only and true delta/tick sequence evidence must remain distinct. A model that requires real sequence semantics cannot be “unblocked” by relabeling snapshots.

## 6. Regime / Router design

Initial Router behavior should be transparent and conservative: calibrated rules/scores rather than immediate reinforcement learning.

Examples of intended routing logic after validation:

- trending regime: higher Path Expert relevance;
- very short time-to-expiry: higher Terminal Expert relevance;
- high-volatility/rapid book change: higher validated Microstructure Expert relevance;
- choppy/ranging or high disagreement: higher abstention threshold;
- elevated reversal risk: discourage entries or prefer exit/hold decisions as supported by EV.

EarnHFT is an architecture reference for hierarchical low-level/high-level routing: second-scale low-level behavior plus slower regime/high-level policy. LIVE15 must not copy its execution/risk semantics blindly.

Development order for Router policy:

1. explicit rules/calibrated scores;
2. only after sufficient forward evidence, consider contextual bandit methods;
3. RL Router only after stronger evidence and separate approval.

The Router may weight expert evidence; it does not bypass Hard Risk or create execution authority.

## 7. Uncertainty and disagreement

Do not reduce experts to a simple majority vote.

Example conflict:

- Terminal: strong YES edge;
- Path: short-term upward;
- Microstructure: strong downward pressure;
- Regime: elevated reversal risk.

The system should calculate/represent disagreement and uncertainty. High disagreement, poor calibration, missing capability, stale data, or unknown regime should increase the probability of `HOLD`/`DATA_UNAVAILABLE` rather than forcing an averaged trade.

Potential uncertainty inputs include:

- model predictive confidence/calibration;
- cross-expert directional disagreement;
- data-quality/freshness status;
- regime confidence;
- model-family readiness and artifact status;
- out-of-distribution/drift indicators.

This layer is a safety/decision input, not a mechanism for concealing a failed expert.

## 8. Autonomous factor and model R&D

`AUTO-RD-001` should build a guarded autonomous research loop inspired by **AlphaGPT** and **RD-Agent(Q)** concepts, while keeping all execution inside LIVE15’s own typed, leakage-safe research boundaries.

### AlphaGPT direction

Use as a conceptual reference for an automatic factor factory:

`generate factor -> evaluate -> score -> retain/reject -> use feedback to generate next candidates`

Do not copy assumptions designed for daily/cross-sectional equities into LIVE15 without adaptation. LIVE15 factors are decision-time, event-local, high-frequency/15-minute-market features.

The existing Factor Factory DSL/VM remains a good safety boundary because candidate expressions can be limited to registered primitives/operators/lookbacks and evaluated without arbitrary code execution.

### RD-Agent(Q) direction

Use as a conceptual reference for a multi-agent research process capable of:

- factor iteration;
- model iteration;
- factor + model joint research;
- automated experiment bookkeeping;
- feedback-driven next-experiment selection.

External agents/projects are research references. They must not obtain direct Recorder write, holdout, Hard Risk, execution, or Production control.

## 9. Guarded factor evolution loop

Approved factor lifecycle:

`Generate -> type/schema validation -> LeakageChecker -> chronological whole-event walk-forward -> ablation -> BH/FDR -> redundancy check -> multi-day/multi-asset/regime stability -> after-cost contribution -> Shadow/Paper forward -> promote/reject`

Additional requirements:

- fixed search budget before metrics are observed;
- no hidden expansion of candidate search after seeing results;
- factors must add incremental evidence beyond primitive baselines;
- unstable/redundant factors are rejected, not archived as “alpha” merely because one fold looks good;
- frozen holdout is never used to steer factor generation;
- generated factors may iterate continuously offline, but Production uses only explicitly promoted artifacts.

## 10. Autonomous model research loop

Model evolution should parallel factor evolution:

`candidate specification -> canonical training_preflight -> immutable Training Snapshot -> train -> chronological validation -> calibration/after-cost/drawdown/consistency -> forward Challenger -> Champion comparison -> promote/reject`

Auto-RD may propose models/hyperparameters, but it cannot relax data authority, split, purge/embargo, holdout, cost, promotion, or Hard Risk rules.

Large search should not begin until data/evidence breadth justifies it; more compute cannot fix insufficient independent days or invalid sequence semantics.

## 11. Stable and Adaptive model concept

A future optional architecture may maintain:

- **Stable Champion** — longer-memory, conservative, proven across regimes;
- **Adaptive Challenger/Champion** — higher weighting on recent/current-regime evidence.

A Router can combine or select them only after forward validation proves value. This is preferable to allowing every recent tick to mutate the live model.

Adaptive behavior must remain reversible, lineage-tracked, and subject to drift/promotion gates from `ROADMAP_001`.

## 12. Explicit EV Decision Engine

The final decision is not a simple expert vote. The Decision Engine combines:

- terminal probability;
- short-horizon path;
- microstructure confirmation;
- regime/reversal risk;
- uncertainty/disagreement;
- executable YES/NO bid/ask;
- fees/costs;
- current position state.

It compares relevant values such as entry edge, close-now value, hold-to-settlement value, and future approved incremental-position value. Hard Risk can veto any decision.

## 13. Conservative first-live action surface

The internal v3 contract may expose more theoretical actions, but the approved first Paper-forward/tiny-live action surface should initially be limited to eight:

1. `BUY_YES`
2. `BUY_NO`
3. `HOLD`
4. `TAKE_PROFIT`
5. `CUT_LOSS`
6. `CLOSE`
7. `HOLD_TO_SETTLEMENT`
8. `DATA_UNAVAILABLE`

`ADD` and `REDUCE` remain disabled initially.

Rationale: first-live behavior should prove entry/hold/exit semantics before allowing dynamic position scaling. Adding to a wrong position amplifies model error and requires separate forward evidence, risk controls, and approval.

## 14. Decision semantics

### Flat position

A new entry requires after-cost terminal edge and appropriate short-horizon confirmation. If edge is below the floor, expert confirmation is weak/conflicted, or uncertainty is high, return `HOLD` rather than forcing a trade.

### Existing position

Continue to compare executable close value versus expected hold value. Near expiry, `HOLD_TO_SETTLEMENT` is appropriate only when terminal EV dominates close-now EV by the configured margin and reversal risk is acceptably low.

Exit reason remains typed:

- `TAKE_PROFIT` when the system chooses an exit that realizes favorable value under deteriorating/reversal conditions;
- `CUT_LOSS` when continued holding is no longer justified and the exit realizes a loss;
- `CLOSE` for neutral/non-P&L-specific exit reasons.

Do not replace EV logic with simplistic fixed percentage stop/take-profit rules unless separately validated as Hard Risk overlays.

### Data unavailable

If required expert inputs, book semantics, freshness, synchronization, regime, or provenance are not trustworthy, the correct decision is `DATA_UNAVAILABLE`/abstain. No guessing or silent fallback to an unrelated provider.

## 15. Hard Risk remains independent

Even a valid `BUY_YES`/`BUY_NO` recommendation cannot become an order if Hard Risk rejects it.

Hard Risk remains responsible for separate constraints such as:

- maximum position/exposure;
- daily/period loss boundaries;
- stale/invalid data veto;
- reconciliation uncertainty;
- execution/system health;
- kill-switch behavior;
- any future approved account/risk policy.

Models, Router, Auto-RD, and EV Decision may not modify Hard Risk to improve backtest results.

## 16. Promotion philosophy

The desired end-state is not “train one model once.” It is a controlled research factory:

`fresh authorized data -> new factors/models -> immutable Challengers -> strict chronological/after-cost validation -> Shadow/Paper forward -> Champion comparison -> promotion or rejection`

New architectures are useful only when they survive the same evidence standards and beat the current Champion in fresh forward conditions.

## 17. Planned tasks

- `AUTO-RD-001` — autonomous factor/model research factory.
- `MODEL-ENSEMBLE-001` — multi-expert model architecture and routing evolution.
- `DEC-ACT-001` — enforce the conservative first-live eight-action surface and keep ADD/REDUCE gated.
- existing `MOD-UNC-001 / MOD-004 / MOD-005`, `DEC-001 / SIG-*`, `RISK-001 / EXE-* / SEC-001` remain downstream components of this direction.

All remain planned until their own task, evidence, tests, Checker/CI, and required human approvals complete.
