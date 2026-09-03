# LIVE15 current state

## Source-of-truth rule

This file holds only stable workstream orientation. It is not a runtime receipt,
deployment log, or historical evidence store. Runtime facts come from the
current service/health evidence; research facts come from the Research Data
Authority and its evidence artifacts.

## Current phase

**Upstream-consolidation freeze with storage/archive reprioritization.** GAP002 is closed/pass.
Runtime/Lifecycle consolidation and the Web Application Shell replacement are **COMPLETE / VERIFIED**:
Nomad + Windows SCM owns lifecycle, the React Admin + Material UI terminal is the sole ControlCenter
Web owner, RuntimeSupervisor and the legacy handwritten shell are retired, cold boot passed, and no
dual owner remains. A subsequently confirmed storage/archive capacity problem changed the next
execution priority before Vector adoption began. The current NEXT responsibility is the bounded
**COMMERCIAL ARCHIVE/PACKAGE UPSTREAM ASSEMBLY**; Vector telemetry remains a deferred later candidate.
Training remains blocked by its existing gates.

ControlCenter current truth is `docs/project-brain/capabilities/control-center.md`. Current task
closeouts and gates are in `PROJECT_PROGRESS.md`; older completed-foundation history remains in
Git/PR history and the bounded evidence selected by current authorities.

## Workstream orientation

| Area | State | Authoritative source |
| --- | --- | --- |
| Kalshi WS / DataGap reliability | **CLOSED / PASS** | `PROJECT_PROGRESS.md`; detailed FAIL/PASS receipts remain evidence only |
| Archive/package lifecycle | **PLANNED / NEXT** | `docs/project-brain/plan/current-roadmap.md`; `docs/roadmap/COMMERCIAL_ARCHIVE_UPSTREAM_ASSEMBLY_001.md` |
| Archive/purge throughput | **CAPACITY PROBLEM / PRESERVE SAFETY GATES** | `docs/project-brain/capabilities/records/recorder/throughput-proof.md`; `docs/roadmap/COMMERCIAL_ARCHIVE_UPSTREAM_ASSEMBLY_001.md` |
| Runtime/Lifecycle consolidation | **COMPLETE / VERIFIED** | `docs/project-brain/dependencies/platform/runtime-ownership.md` |
| Nomad + Windows SCM lifecycle replacement | **ADOPTED / PRODUCTION VERIFIED** | `docs/project-brain/dependencies/platform/runtime-ownership.md` |
| RuntimeSupervisor | **RETIRED** | Runtime authority; PR #129 |
| Web Application Shell | **COMPLETE / VERIFIED** | `docs/project-brain/capabilities/control-center.md`; React Admin + Material UI terminal |
| Vector telemetry | **DEFERRED / LATER CANDIDATE** | `docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md`; bounded POC evidence retained |
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
