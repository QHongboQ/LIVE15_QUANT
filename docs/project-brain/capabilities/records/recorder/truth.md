# Recorder truth and ownership

Revision: R1
Status: LIVE15-owned authority; no migration authorized.

## What it is

Recorder/RecorderStore captures authoritative raw truth, persistence, gap/quarantine, and settlement lineage.

## Current truth

Recorder ownership is unchanged by the verified ControlCenter Nomad cutover. Truth, quarantine,
settlement, as-of, and freshness rules remain local; that cutover does not authorize a Recorder migration.

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
