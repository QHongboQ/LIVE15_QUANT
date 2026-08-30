# Research Data Authority

Revision: R2
Status: LIVE15-owned authority.

## What it is

The RDA defines authorized H0/H1/H2 research coverage and separation from immutable training snapshots.

## Current truth

H0 is Recorder/verified archive, H1 official historical evidence, and H2 validated credentialed L2.
ResearchUniverseSnapshot is not a Dataset v1/v2 partition. Research coverage comes from the typed
RDA: decision-time feature freshness, development-history recency, and post-spec forward OOS
freshness remain separate; a 15-minute horizon is not a two-day history limit.

## Interfaces / dependencies

`docs/research_data_authority.md`; `docs/training_dataset.md`; `docs/kalshi_native_architecture.md`.

## Read next

Use `../model-governance/README.md` for model validation, promotion, and holdout gates.

## Update rule

Update only for an RDA authority or source-registry decision.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 authority baseline. |
| R2 | PROJECT-BRAIN-V2-MERGE-GATE-FINAL | Moved RDA coverage/freshness detail out of always entry. |
