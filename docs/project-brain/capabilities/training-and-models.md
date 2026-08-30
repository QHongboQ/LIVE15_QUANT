# Training and models

Revision: R2
Status: gated; no training authorized.

## What it is

Training, model lineage, evaluation, promotion, and holdout boundaries.

## Current truth

`NO TRAINING_GO` and `NO TRAINING_STARTED`. holdout-contamination remediation/replacement is required before `TRN-001`; no frozen-holdout payload may be reopened for scope measurement.

## Data and model invariant detail

- Only Kalshi finalized settlement with an official `yes`/`no` result is terminal label truth.
- Predictive feeds never manufacture settlement labels.
- Decision inputs obey strict as-of timestamps; missing, stale, unsynchronized, or gapped data
  fails closed. Do not forward-fill, interpolate, or use future rows.
- Dataset v1 final test is frozen. Never tune vNext on it.
- Use chronological/event-grouped validation, not random row splits. Do not add features without
  an ablation. The current v2 baseline remains the baseline until fresh forward Challenger
  evidence exists.

## Interfaces / dependencies

`docs/training_dataset.md`; `docs/model_vnext_contract.md`; `PROJECT_CHARTER.md`.

## Read next

Use `plan/current-roadmap.md` for ordering and `constraints/README.md` for high-risk routing.

## Update rule

Update only for an approved training, promotion, or holdout authority decision.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 authority baseline. |
| R2 | PROJECT-BRAIN-V2-MERGE-GATE-FINAL | Moved model/data invariants out of always entry. |
