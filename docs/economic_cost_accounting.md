# ECON-COST-001 — Full economic cost accounting contract

Status: **USER-APPROVED PROJECT POLICY / IMPLEMENTATION PENDING**

This contract defines the economic-cost boundary for LIVE15 model/factor evaluation, Router/EV decisions, Champion/Challenger comparison, and any future Paper/Shadow/Production profitability claim. It does not authorize training, model promotion, deployment, execution, Hard Risk changes, or Production writes.

## Core rule

LIVE15 must optimize and validate economic value **after all material real-world costs**, not merely direction accuracy, raw price movement, or pre-fee P&L.

Predictive labels and calibrated probabilities remain statistical targets. Costs must not be fabricated into settlement labels or probability targets. Instead, every factor/model/router/threshold/promotion decision that claims tradable edge must be evaluated through a versioned full-cost economic layer.

## Required cost stack

At minimum, the economic layer must account for every applicable component below:

1. **Executable entry and exit prices** — actual bid/ask-side economics rather than descriptive midpoint assumptions.
2. **Bid/ask spread** — including the cost of crossing the book where applicable.
3. **Slippage and market impact** — including size-dependent execution degradation.
4. **Partial-fill, queue, and fill uncertainty** — including the economic effect of unfilled or partially filled orders and adverse selection where measurable.
5. **Venue and contract fees** — Kalshi trade fees, series-specific fee overrides, fee schedule changes, rounding effects, and rebates when actually applicable.
6. **Settlement/transfer/banking costs** — any real settlement, withdrawal, transfer, banking, or payment cost that applies to realized strategy economics.
7. **Capital and funding costs** — locked-capital/opportunity cost, financing cost, or other capital-use cost when material to the strategy horizon.
8. **Taxes** — an explicit, configurable, versioned tax policy or conservative tax reserve must be included in after-tax economics when tax treatment is applicable.
9. **Data, infrastructure, API, and compute costs** — provider subscriptions, paid historical data, compute/GPU/CPU, storage, network, and other allocatable operating costs must be included when judging whether the overall strategy is actually profitable.
10. **Other material costs** — any future exchange, regulatory, conversion/FX, account, vendor, or operational cost that can materially change net profitability.

A cost component may be zero only when supported by the applicable venue/account/provider policy for the relevant period; it must not be silently omitted.

## Tax treatment

Taxes are not a fixed exchange fee and must not be invented as a universal per-trade percentage. Actual treatment can depend on jurisdiction, taxpayer status, instrument treatment, realized gains/losses, netting, timing, and then-current law.

Therefore LIVE15 must:

- keep tax assumptions outside predictive labels/features unless they are valid decision-policy inputs;
- store a named, versioned tax-policy assumption with each economic evaluation;
- support pre-tax and after-tax results separately;
- use an explicit conservative reserve/scenario when exact tax liability is not yet known;
- never claim after-tax profitability if the tax policy is missing, stale, or materially uncertain;
- update the tax policy when authoritative account/legal/tax treatment changes rather than hard-coding an unverifiable personal rate.

This project policy is an accounting/evaluation requirement, not tax advice.

## Economic objective

For a candidate action, the decision layer should conceptually compare:

`net_economic_value = gross_expected_value - execution_costs - venue_fees - other_transaction_costs - tax_cost_or_reserve - allocatable_operating_costs`

The implementation may decompose this further by entry, exit, hold-to-settlement, and portfolio/account context. Every component must retain provenance, units, effective time, and assumption version.

A model with higher accuracy but lower net economic value is not superior. A factor/model/threshold may be promoted only when its incremental value remains positive and robust under the approved full-cost policy and declared cost-stress scenarios.

## Training and model-selection boundary

Model architecture selection remains governed by the already approved layered model roadmap. ECON-COST-001 does **not** replace or reopen those model choices.

For training/evaluation:

- pure predictive loss may still optimize probability/path/microstructure targets;
- hyperparameter, factor, threshold, Router, Champion/Challenger, and promotion decisions must use full-cost economic evaluation where tradability is claimed;
- all compared candidates must use the same cost-policy version and executable-price assumptions;
- cost assumptions must be fit/configured from authorized information only and must not introduce future leakage;
- backtests and forward evaluations must report both gross and net results, with cost decomposition;
- cost stress must include worse spread/slippage/fill/fee assumptions than the central estimate;
- a claimed edge that disappears under reasonable cost stress is not robust edge.

## Fail-closed behavior

If a material cost is unknown, stale, unavailable, or cannot be bounded credibly, LIVE15 must either:

1. use a documented conservative upper-bound/reserve, or
2. mark the economic result unavailable and prevent promotion/trade authorization that depends on it.

Unknown material costs must never default silently to zero.

## Lineage and audit requirements

Every future training/evaluation/promotion artifact that makes an economic claim must record, directly or by immutable reference:

- cost-policy version;
- executable-price convention;
- fee schedule/version/effective time;
- slippage/impact/fill model version;
- tax policy or reserve version;
- operating-cost allocation policy where used;
- gross P&L/EV;
- each material cost component;
- net pre-tax P&L/EV;
- net after-tax P&L/EV when tax policy is available;
- cost-stress results;
- data/code/model lineage used to produce the calculation.

## Implementation gate

A future bounded implementation task should add executable typed cost accounting and tests before formal training/promotion can rely on after-cost profitability. Until then, existing fee code remains useful execution/paper infrastructure but is not sufficient by itself to satisfy this full economic-cost contract.
