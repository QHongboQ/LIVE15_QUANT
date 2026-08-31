# LIVE15 current state

## Source-of-truth rule

This file holds only stable workstream orientation. It is not a runtime receipt,
deployment log, or historical evidence store. Runtime facts come from the
current service/health evidence; research facts come from the Research Data
Authority and its evidence artifacts.

## Current phase

**Upstream-consolidation freeze.** GAP002 is closed/pass. Normal feature/model expansion is paused
while generic responsibilities are consolidated subtractively, one owner and one bounded
replacement at a time. Training remains blocked by its existing gates.

ControlCenter current truth is `docs/project-brain/capabilities/control-center.md`. Current task
closeouts and gates are in `PROJECT_PROGRESS.md`; older completed-foundation history remains in
Git/PR history and the bounded evidence selected by current authorities.

## Workstream orientation

| Area | State | Authoritative source |
| --- | --- | --- |
| Kalshi WS / DataGap reliability | **CLOSED / PASS** | `PROJECT_PROGRESS.md`; detailed FAIL/PASS receipts remain evidence only |
| Archive/purge throughput | **ON_DEMAND_MEASUREMENT** | The standalone ST-005 task is superseded; its bounded contract remains at `docs/project-brain/capabilities/records/recorder/throughput-proof.md` for later measured candidate decisions only |
| Nomad runtime ownership | **VERIFIED** | `docs/project-brain/dependencies/platform/runtime-ownership.md` |
| Generic infrastructure | **UPSTREAM_CONSOLIDATION** | `docs/project-brain/constraints/execution/runtime-upstream-boundary.md` |
| Research coverage | Typed H0/H1/H2 authority | `docs/research_data_authority.md` and `/api/research-data` |
| Dataset/model promotion | Requires fresh forward challenger evidence | `docs/model_vnext_contract.md`, model lineage |
| Hard Risk / Production writes | Human-authorized only | `PROJECT_CHARTER.md`, `AGENTS.md` |

## Current limits

`MERGED != DEPLOYED`; `DEPLOYED != VERIFIED`. `NO_TRAINING_GO` and `NO_TRAINING_STARTED` remain in
force. Upstream consolidation does not authorize Production mutation, holdout access, training,
Paper/Shadow activation, Hard Risk changes, or trading writes.

## Current execution route

The sole approved execution sequence is owned by
`docs/project-brain/plan/current-roadmap.md`. Capability detail is routed by
`docs/project-brain/capabilities/README.md`; execution constraints by
`docs/project-brain/constraints/README.md`.

## Update policy

Update this file only when a durable workstream state or source-of-truth rule
changes. Put measurements, PIDs, timestamps, and transient incidents in their
bounded evidence artifacts instead. Do not use this file to freeze or override
the separate runtime-closeout result.
