# MVN-001 — Model vNext data, target, and leakage contract

Status: contract frozen. MVN-001 does not train, calibrate, promote, or connect a model to
Paper/Shadow. The executable guards live in `live15_quant.model_vnext_contract`; this document is
the canonical human-readable policy for later MVN-002 work.

## Decision-time information set

Every example is identified by `event_id`, Kalshi `ticker`, window start/end, decision timestamp,
target level, contract side, and an explicit lookback. All timestamps are timezone-aware UTC
instants. A feature observation is admissible only when both its `received_timestamp` and, when
present, its provider `source_timestamp` are no later than the decision timestamp. A later
backfill, synthetic value, interpolation, forward fill, nearest-future join, or post-window
substitution is rejected. Missing, stale, unsynchronized, or gapped inputs remain unavailable.

`time_remaining_seconds` is the exact window-end minus decision-time duration. The model sees the
contract geometry and predictive feeds available at that instant; it does not see a settlement
object.

## Path targets

The first structured path contract permits horizons of 5, 15, 30, 60, 120, 180, and 300 seconds,
plus a `window_end` target where the event still has a valid future observation. For an observed
base value `x_t` and an explicitly observed future value `x_(t+h)`:

`path_return_h = x_(t+h) / x_t - 1`

The future lookup must be within the declared two-second tolerance of `decision_timestamp + h`;
the event window must not be crossed. Ties choose the earlier observation. No target is fabricated
when the observation is missing. Path direction is separate from terminal YES/NO probability.

The conceptual terminal output remains:

`P(final_underlying > target | information available at decision time)`

Model probability, market-implied probability, executable bid/ask, and descriptive midpoint are
separate quantities.

## Terminal label boundary

Only finalized Kalshi settlement with an official `yes` or `no` result is terminal label truth.
Coinbase, Pyth, future providers, path targets, and market prices are predictive inputs only; they
cannot manufacture, correct, verify, or replace a settlement label. Settlement-derived fields are
prohibited from the feature boundary.

## Leakage checklist (P0)

| Rule | Machine gate |
| --- | --- |
| Look-ahead | source and receive timestamps are as-of the decision |
| Label | no settlement/result/outcome fields in features |
| Backfill | later-recovered observations are rejected |
| Join | future nearest rows are not selected outside tolerance |
| Rolling window | windows cannot cross the decision or use synthetic values |
| Event split | one event/ticker group appears in one partition only |
| Normalization | statistics are fitted on train rows only |
| Model selection | validation/development only; final test is not tuned |
| Calibration | fit only on the declared training/validation fold |
| Hyperparameters | no revealed final-test inspection |
| Cross-asset temporal | synchronized features obey their own receive clocks |
| Archive/replay | replay preserves original availability timestamps and provenance |

`LeakageChecker` is the independent MVN-001 review profile. Future data/model tasks should invoke
it at their public boundary and record the result.

## Splits and purge/embargo

Formal evaluation is chronological and grouped by whole event/ticker/window identities. Random row
splits are not a valid path. Walk-forward folds may be expanding or rolling, but no group may span
train, validation, or test. The required purge/embargo is derived, never guessed:

`max_feature_lookback_seconds + max_target_horizon_seconds`

The initial maximums are 300 seconds each, so the initial boundary guard is 600 seconds. A later
dataset may increase this only by recomputing the formula from its declared contract.

## Frozen Dataset v1 and evidence gate

Dataset v1 is `live15-dataset-v1-f81d7d1feebcbbaecff9`, build hash
`f81d7d1feebcbbaecff93086c2e1a577aeb72cc98b7bfabd22d826e05a4cce95`: 1,091 independent events,
7,984 rows, 42 features, and three independent UTC days (2026-08-20 through 2026-08-22). Its
final test is revealed and frozen (161 events / 1,254 rows); it is not available for vNext feature,
horizon, architecture, threshold, calibration, asset, or regime selection. Historical final-test
numbers are revealed history, not fresh vNext OOS evidence.

Fresh evidence must come from future Shadow, Paper, or Demo forward observations. Current sequence
readiness remains `INSUFFICIENT_SEQUENCE_EVIDENCE`; the WS row count alone does not authorize TCN,
DeepLOB, PatchTST, or another deep sequence challenger.

## Ablations and model order

Feature groups enter one at a time on identical chronological folds: (P1-A) short-horizon path,
(P1-B) Kalshi microstructure, (P1-C) path/risk diagnostics, then (P1-D) cross-market leading
signals. Retain a group only on incremental after-cost evidence across independent days, events,
assets, regimes, time-to-expiry buckets, and cost stress.

The implementation order is structured logistic baseline, structured XGBoost path baseline, then a
causal sequence challenger only after the evidence gate. MVN-001 does not train MVN-002, tune
thresholds, implement uncertainty, or wire dynamic exits/execution.

## Lineage and review

Every future artifact must retain the Dataset ID/build hash, feature and label schema versions,
training code SHA, fold policy, normalization/calibration provenance, cost assumptions, and final-
test state. Paper remains local-only and paused; production writes remain disabled.
