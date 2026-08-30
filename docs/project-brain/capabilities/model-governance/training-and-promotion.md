# Training and promotion gates

Revision: R2
Status: gated; no training authorized.

## What it is

Defines training, promotion, model-lineage, and frozen-holdout authority boundaries.

## Current truth

`NO TRAINING_GO` and `NO TRAINING_STARTED`. `LONG_RUN_TRAINING_FINAL_GO_NO_GO` has not run.
Holdout-contamination remediation/replacement is required before `TRN-001`; do not reopen frozen
holdout payload to measure scope. The prior broad artifact search exposed rows but informed no
WS/GAP/H2 implementation, thresholds, parameters, or code changes. **PRODUCTION WRITES 0.**

H2 capability remains granular: snapshot, snapshot-sequence, delta/tick, and each microstructure
family are independently gated; DepthFeed HTTP 402 plan restrictions must remain visible.
`H2-TRAIN-003` is not an independent lane: its prior BLOCKED result exposed the WS/DataGap
authority problem, so any H2 revalidation is acceptance work inside `WS-RESYNC-001 + GAP-002`.

## Interfaces / dependencies

`docs/training_dataset.md`; `docs/model_vnext_contract.md`; `PROJECT_CHARTER.md`.

## Read next

Use `../../plan/current-roadmap.md` for ordering and `../../constraints/README.md` for high-risk routing.

## Update rule

Update only for an approved training, promotion, or holdout authority decision.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 training/model authority baseline. |
| R2 | PROJECT-BRAIN-V2-MERGE-GATE-FINAL | Moved data invariants out of always entry. |
