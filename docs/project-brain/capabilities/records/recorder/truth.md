# Recorder truth and ownership

Revision: R2
Status: LIVE15 truth authority; Nomad runtime ownership verified.

## What it is

Recorder/RecorderStore captures authoritative raw truth, persistence, gap/quarantine, and settlement lineage.

## Current truth

Recorder process lifecycle is Nomad-owned under `Nomad:live15-recorder`; truth, quarantine,
settlement, as-of, freshness, persistence, and gap semantics remain LIVE15-owned. Nomad may own
allocation/restart/deployment behavior but cannot create, infer, repair, or override Recorder truth.

## Interfaces / dependencies

`docs/continuous_recorder.md`; `docs/research_data_authority.md`; `../reliability.md`.

## Read next

For runtime ownership read `../../../dependencies/platform/runtime-ownership.md`; for ST-005 proof read
`throughput-proof.md`.

## Update rule

Update only for a Recorder authority, truth, or approved ownership decision.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 Recorder truth baseline, moved without semantic change. |
| R2 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 | Reconciled verified Nomad process ownership while preserving LIVE15 truth ownership. |
