# Model Architecture v3 research and selection

Status: architecture and data-contract stage only. No v3 model has been trained,
promoted, or connected to Paper/Shadow. Dataset v1 final test remains revealed
and is forbidden for v3 architecture, horizon, threshold, calibration, or asset
selection.

The broader Model vNext hard gate is frozen in [`docs/model_vnext_contract.md`](model_vnext_contract.md).
It is a contract-only milestone; this document's v3 research remains evidence-gated and does not
authorize training or promotion.

## Selected layered design

| Layer | First implementation | Role |
| --- | --- | --- |
| Terminal probability | Frozen Model Zoo v2 candidates | `P(final YES)`; retains the current certified lineage. |
| Trend/path | Structured multi-horizon XGBoost baseline, then causal TCN challenger | Predict 30s, 1m, 3m, and 5m underlying path outputs. |
| Kalshi microstructure | DeepLOB-style sequence feature contract, then causal TCN challenger | Predict 10s, 30s, and 60s contract-price movement, not settlement. |
| Regime | Rule-assisted structured regime baseline | Trend, range, volatility, and reversal-risk confidence. |
| Decision | Explicit expected-value engine | Compare executable close now, hold, and incremental add; it does not execute. |

`Model Architecture v3` therefore does not ask a terminal YES model to predict
short-horizon tradability. A future v3 terminal expert enters Paper/Shadow
through `V3ForwardPredictionAdapter`, which implements the existing
`ForwardPredictionProvider` contract. Ledger, fills, accounting, settlement,
and idempotency remain unchanged.

## Upstream research manifest

| Candidate | Pinned upstream | License | Original task | LIVE15 decision |
| --- | --- | --- | --- | --- |
| DeepLOB paper | [paper](https://arxiv.org/abs/1808.03668), [source](https://github.com/zcakhaa/DeepLOB-Deep-Convolutional-Neural-Networks-for-Limit-Order-Books) `ff14d7c2fd38bdfc143389786993d0f0236d4eb8` | Repository license was not present at the pinned source; unverified | Equity LOB mid-price classification | Research the CNN/inception/LSTM ideas only; do not vendor or copy upstream code. |
| PatchTST | [paper](https://arxiv.org/abs/2211.14730), [official source](https://github.com/yuqinie98/PatchTST) `204c21efe0b39603ad6e2ca640ef5896646ab1a9` | Apache-2.0 | Long-horizon multivariate forecasting | Deferred challenger: current independent sequence evidence is not sufficient for a patch Transformer. |
| TCN | [paper](https://arxiv.org/abs/1803.01271), [source](https://github.com/locuslab/TCN) `2f8c2b817050206397458dfd1f5a25ce8a32fe65` | MIT | Causal sequence modeling | Selected future low-latency sequence backbone; use an independently implemented causal adapter, not a copied repository. |
| XGBoost | [paper](https://doi.org/10.1145/2939672.2939785), [official source](https://github.com/dmlc/xgboost) `379b29f0836b9dbc313b993d8e5743bd452d4117` | Apache-2.0 | Structured boosted prediction | Selected first multi-horizon path/regime baseline; already pinned in the project. |

The pinned revisions and license statuses are duplicated in the machine-readable
`architecture_manifest()` function. No upstream code, weights, or data were
downloaded into this repository.

The same manifest also records each candidate's model-size description,
training-data requirement, and latency status. These are deliberately recorded
as either measured evidence or `not benchmarked`; this phase does not turn
architecture intuition into an unverified live-latency claim.

## Sequence Dataset v1 contract

Sequence Dataset v1 is a future, immutable offline artifact. It consumes an
archive/snapshot identity, never the active recorder database. Each example is
one event/ticker and has:

- only synchronized Kalshi WS frames with `received_timestamp <= decision_timestamp`;
- a fixed historical lookback ending at the decision;
- future 10s/30s/60s contract-midpoint targets from the same ticker and before
  window end;
- explicit target tolerance, no forward fill, no synthetic frame, and no REST
  substitution;
- event-grouped chronological train/validation/test partitions.

The evidence gate requires enough independent events, examples, distinct days,
synchronized coverage, and gap-free coverage. A high WS row count cannot pass it
on its own. Until it passes, the only valid result is
`INSUFFICIENT_SEQUENCE_EVIDENCE`.

## Ablations fixed before training

Every trainable v3 candidate must compare its original-style baseline with the
LIVE15 adaptation on identical grouped chronological folds:

1. raw path/LOB inputs only;
2. + target distance;
3. + time remaining;
4. + Kalshi implied probability;
5. + synchronized microstructure;
6. + regime features.

Promotion is based on after-cost development performance, calibration, drawdown,
profit factor, trade count, and cross-asset consistency—not gross return. Final
test results from Dataset v1 are never reused as a v3 OOS claim. Fresh OOS must
come from future Paper/Shadow or Demo forward evidence.

## Dynamic decision policy

The deterministic decision engine is deliberately conservative:

- Flat: Buy only when after-cost terminal edge and 30-second path plus book
  confirmation agree; otherwise hold.
- Open: Compare executable bid minus exit cost with expected terminal payout.
  An EV-dominated exit is classified as `TAKE_PROFIT`, `CUT_LOSS`, or neutral
  `CLOSE` from its executable result, not mechanically from price movement.
- Near expiry: `HOLD_TO_SETTLEMENT` needs terminal EV to dominate close-now EV
  with low reversal risk.
- `ADD`, `REDUCE`, and `CUT_LOSS` require explicit incremental EV/risk logic and
  still pass through the existing immutable hard-risk layer when later wired to
  Paper/Shadow.

This module is a typed decision proposal only. It has no venue client, no order
path, no Demo/Production account capability, and no automatic forward activation.
