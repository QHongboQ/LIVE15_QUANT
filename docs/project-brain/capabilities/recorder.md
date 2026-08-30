# Recorder

Revision: R1
Status: LIVE15-owned authority; no migration authorized.

## What it is

Recorder/RecorderStore captures authoritative raw truth, persistence, gap/quarantine, and settlement lineage.

## Current truth

Recorder ownership is unchanged by the verified ControlCenter Nomad cutover. `ST-005` still requires valid measured proof; truth, quarantine, settlement, as-of and freshness rules remain local.

## Interfaces / dependencies

`docs/continuous_recorder.md`; `docs/research_data_authority.md`; `capabilities/reliability.md`.

## Read next

For runtime ownership use `dependencies/runtime-ownership.md`; for execution gates use `constraints/README.md`.

## Update rule

Update only for a Recorder authority, truth, or approved ownership decision.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 authority baseline. |
