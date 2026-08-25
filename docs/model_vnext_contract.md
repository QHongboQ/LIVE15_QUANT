# Model vNext contract

Status: design/validation contract only. This document does not activate a model, change Paper thresholds, change hard risk, or enable Demo/Production writes.

## 1. Purpose

Model vNext exists to improve the current v2 static forward baseline without weakening LIVE15's evidence gates. The model must predict the future underlying path and terminal event probability, translate that forecast into after-cost executable edge, expose calibrated uncertainty, and support continuous position re-evaluation.

Kalshi implied probability remains a market benchmark, consensus signal, and executable-price input. It is not the prediction target and must not be copied into the model as a substitute for forecasting the underlying path.

## 2. Immutable boundaries

- Kalshi finalized settlement is the only training label truth for terminal YES/NO.
- Underlying feeds are predictive inputs only; they never manufacture or verify settlement labels.
- All features are strict as-of the decision timestamp. Unknown/missing stays typed missing; never fill with zero by default.
- Only synchronized, gap-free, fresh inputs may enter a live decision. Stale/gap/unknown states fail closed.
- Dataset v1 frozen final test is not reused for vNext architecture, threshold, calibration, feature, or hyperparameter selection.
- New feature groups require chronological ablation. No random row split and no feature stuffing.
- v2 Paper remains the static forward baseline/champion reference until a Challenger passes forward gates.
- Hard Risk is independent of the model and cannot be relaxed by model confidence.
- This contract has no venue client and grants no automatic Production write authority.

## 3. Prediction contract

### 3.1 Path outputs

For each decision timestamp, the first structured baseline should produce a distribution or calibrated point/quantile representation for future underlying moves at these horizons:

- 5s
- 15s
- 30s
- 60s
- 120s
- 180s
- 300s
- terminal/window end when enough time remains

The first implementation should favor simple structured models before sequence models. Sequence challengers remain evidence-gated by independent events, UTC days, regimes, synchronized coverage, and gap-free coverage.

### 3.2 Terminal probability

The terminal layer computes or directly estimates:

`p_terminal = P(final_underlying > target | information available now)`

for YES contracts, with the complementary probability for NO. The implementation must preserve target direction and market geometry explicitly; no midpoint is treated as truth.

Terminal probability must be evaluated with Brier score, LogLoss, calibration/ECE, asset robustness, time-to-expiry robustness, and regime robustness.

### 3.3 Market benchmark

At each decision, persist the comparable executable market state separately:

- best executable YES ask / NO ask for entry
- best executable YES bid / NO bid for exit
- spread and depth/liquidity state
- descriptive implied probability / midpoint only as benchmark features

Model and market probabilities must remain separately named in storage and diagnostics.

## 4. After-cost Net Edge contract

The decision layer must never trade on raw `p_model - ask` alone.

Define a conservative executable edge decomposition:

`gross_edge = expected_contract_value - executable_entry_price`

`net_edge = gross_edge - fees - expected_slippage - liquidity_penalty - uncertainty_penalty`

where:

- `expected_contract_value` is derived from calibrated terminal probability and contract payout geometry;
- `executable_entry_price` uses the relevant ask, not midpoint;
- `fees` use the venue fee schedule or a conservative bounded estimator available at decision time;
- `expected_slippage` is zero only when there is evidence the intended size is executable at the quoted level; otherwise it must be positive/conservative;
- `liquidity_penalty` increases for thin depth, wide spread, unstable book, or stale-near-threshold state;
- `uncertainty_penalty` increases when calibration confidence, ensemble agreement, regime confidence, or OOD confidence weakens.

All components must be individually persisted for Paper/Shadow audit. A single opaque `confidence` scalar is insufficient.

## 5. Uncertainty contract

vNext confidence must represent forecast uncertainty rather than distance from the ask.

First implementation may combine independently measurable components:

- calibration reliability by asset / time-to-expiry bucket;
- disagreement across eligible structured challengers;
- OOD distance or feature-support diagnostics;
- regime confidence;
- liquidity/book quality confidence;
- data-quality readiness.

The decision engine consumes these as typed fields. It may collapse them into a conservative penalty for execution, but raw components remain inspectable.

Unknown uncertainty inputs must reduce confidence or fail closed; they may not silently become maximum confidence.

## 6. Dynamic Exit contract

An open position is re-evaluated whenever a valid new decision state is available. The decision engine compares executable alternatives instead of mechanically holding to settlement.

Required actions:

- `HOLD`
- `REDUCE`
- `TAKE_PROFIT`
- `CUT_LOSS`
- `CLOSE`
- `HOLD_TO_SETTLEMENT`

For an open YES position, compare at minimum:

- `EV_close_now`: executable YES bid net of exit cost;
- `EV_continue`: expected value of holding through the next review horizon, net of expected future execution/risk cost;
- `EV_settlement`: calibrated terminal probability net of relevant holding/risk penalties.

NO positions use the symmetric contract.

`TAKE_PROFIT` and `CUT_LOSS` are classifications of an EV-dominant close, not fixed percent-price rules. `HOLD_TO_SETTLEMENT` requires terminal EV to dominate executable close-now EV with acceptable reversal/tail risk.

No dynamic exit action bypasses Hard Risk or execution reconciliation.

## 7. Feature rollout order

Feature groups are introduced one at a time on identical chronological folds.

### P1-A: short-horizon path

- 5/15/30/60/120/180/300s returns
- realized volatility and range
- acceleration / deceleration
- reversal indicators

### P1-B: Kalshi microstructure

- OFI
- multi-level OBI
- microprice
- spread velocity
- depth velocity
- add/cancel intensity
- CVD / signed trade-flow features where timing is trustworthy

### P1-C: path/risk labels and diagnostics

- MFE / MAE
- multi-horizon move magnitude
- P(up/down)
- reversal risk

### P1-D: cross-market leading signals

- perp/futures versus spot
- basis and basis velocity
- lead/lag
- cross-asset residuals

P2 inputs such as funding, OI, liquidation bursts, options IV/skew, and news/event intelligence do not block the first vNext baseline.

## 8. Validation gate

Every candidate must use:

- chronological walk-forward;
- event grouping so rows from one 15-minute event never cross splits;
- purge/embargo around boundaries where required by overlapping lookbacks/targets;
- a frozen holdout not repeatedly inspected;
- identical fold definitions for feature-group ablations;
- cost stress including at least +1c execution stress or an equivalent conservative scenario;
- asset, time-of-day/time-to-expiry, and regime breakdowns;
- tail-loss and drawdown diagnostics, not total PnL alone.

A new feature group is retained only when it improves the predeclared scorecard without unacceptable robustness degradation. If it cannot prove incremental value, remove it.

## 9. Challenger scorecard

Minimum tracked metrics:

Probability quality:

- Brier
- LogLoss
- ECE / reliability

Trading quality after cost:

- net PnL
- profit factor
- MaxDD
- tail loss / worst-event loss
- trade count and turnover
- win/loss asymmetry

Robustness:

- per asset
- per regime
- per time-to-expiry bucket
- per independent UTC day
- cost stress

Dynamic exit comparison:

- static hold-to-settlement vs dynamic policy on the same forward opportunities
- exit reason frequencies
- incremental after-cost PnL
- DD/tail-loss change

## 10. Promotion rule

The promotion path is:

`candidate -> offline chronological gate -> shadow -> Paper forward Challenger -> promotion gate -> champion`

Promotion requires fresh forward evidence. Development backtests alone cannot declare a champion.

Rollback must be immediate to the prior immutable model/version if forward decay, calibration failure, data-quality anomalies, or risk gates trigger.

## 11. First implementation milestone

The first vNext implementation should deliberately stay small:

1. build a runtime/data/model baseline snapshot before changing model code;
2. freeze the exact training/validation event split contract for the next development dataset;
3. implement structured short-horizon path targets and a simple XGBoost/logistic baseline;
4. implement explicit terminal probability output;
5. implement the persisted after-cost edge decomposition;
6. add uncertainty fields with conservative defaults;
7. implement a pure typed dynamic-exit evaluator with no venue capability;
8. run chronological ablations and produce a machine-readable scorecard;
9. only then wire an accepted Challenger into Paper/Shadow alongside v2.

## 12. Explicit non-goals for this milestone

- no SDK/Recorder rewrite;
- no legacy WebSocket deletion;
- no automatic Production orders;
- no threshold tuning against Dataset v1 final test;
- no TCN/PatchTST/DeepLOB-style model merely because WS row count is large;
- no large feature dump without group-wise ablation;
- no automatic online self-training or self-promotion;
- no risk-cap changes.
