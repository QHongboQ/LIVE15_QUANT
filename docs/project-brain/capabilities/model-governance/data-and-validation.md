# Model data and validation

Revision: R1
Status: invariant authority.

## What it is

Defines label truth, decision-time input validity, dataset boundary, and validation rules.

## Current truth

Only Kalshi finalized settlement with official `yes`/`no` is terminal label truth; predictive feeds
never manufacture labels. Inputs obey strict as-of timestamps: missing, stale, unsynchronized, or
gapped data fails closed—never forward-fill, interpolate, or use future rows. Dataset v1 final test
is frozen; use chronological/event-grouped validation and feature ablations. The v2 baseline remains
until fresh forward Challenger evidence exists.

## Interfaces / dependencies

`docs/training_dataset.md`; `docs/model_vnext_contract.md`; `docs/model_artifact_lineage.md`.

## Read next

For training/holdout gates read `training-and-promotion.md`; for research coverage read
`../records/research-data.md`.

## Update rule

Update only for an approved data, validation, or model-baseline authority decision.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-V2-RECURSIVE-HIERARCHY-001 | Moved data/validation invariants from broad model leaf. |
