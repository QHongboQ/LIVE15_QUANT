# LIVE15 current state

## Source-of-truth rule

This file holds only stable workstream orientation. It is not a runtime receipt,
deployment log, or historical evidence store. Runtime facts come from the
current service/health evidence; research facts come from the Research Data
Authority and its evidence artifacts.

## Current phase

**Storage/archive Production-acceptance preparation.** GAP002 is closed/pass. Runtime/Lifecycle
consolidation and the Web Application Shell replacement are **COMPLETE / VERIFIED**. The storage
capacity problem has already moved beyond candidate selection: the commercial bakeoff selected
**Parquet + ZSTD** for the verified cold archive path, the HOT->COLD closed loop and named multi-root
layout are merged, and the first Production acceptance attempt stopped safely before mutation.

PR #167 resolved the Runtime/deploy blocker that interrupted the archive acceptance path, but
`MERGED != DEPLOYED`: the host is still on the retained legacy Production runtime until a new immutable
runtime revision is prepared, verified, and separately rolled out through Nomad. The immediate NEXT is
that runtime preparation/Recorder rollout verification; after that, WTI is retired completely as its
own narrow task before a fresh Parquet Production Phase 1 acceptance. Vector telemetry remains deferred.
Training remains blocked by its existing gates.

ControlCenter current truth is `docs/project-brain/capabilities/control-center.md`. Current task
closeouts and gates are in `PROJECT_PROGRESS.md`; older completed-foundation history remains in
Git/PR history and the bounded evidence selected by current authorities.

## Workstream orientation

| Area | State | Authoritative source |
| --- | --- | --- |
| Kalshi WS / DataGap reliability | **CLOSED / PASS** | `PROJECT_PROGRESS.md`; detailed FAIL/PASS receipts remain evidence only |
| Archive/package format selection | **COMPLETE / PARQUET+ZSTD SELECTED** | `docs/project-brain/plan/current-roadmap.md`; PRs #157/#158 |
| Parquet HOT->COLD closed loop | **MERGED / NOT YET PRODUCTION-ACCEPTED** | PR #158; `PROJECT_PROGRESS.md` |
| Named multi-root archive layout | **MERGED** | PR #160; `PROJECT_PROGRESS.md` |
| Production Parquet acceptance | **PENDING FRESH PHASE1-002** | `docs/project-brain/plan/current-roadmap.md`; PR #159 is historical stop evidence only |
| Runtime deployment simplification | **MERGED / HOST ROLLOUT PENDING** | `docs/project-brain/dependencies/platform/runtime-ownership.md`; PR #167 |
| WTI retirement | **PLANNED / AFTER RUNTIME ROLLOUT** | `docs/project-brain/plan/current-roadmap.md` |
| Archive/purge throughput | **CAPACITY PROBLEM / PRESERVE SAFETY GATES** | `docs/project-brain/capabilities/records/recorder/throughput-proof.md` |
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
force. Runtime preparation does not authorize a Recorder deployment; Recorder deployment does not
authorize archive activation; Production acceptance does not authorize purge. Holdout access,
training, Paper/Shadow activation, Hard Risk changes, and trading writes remain outside this lane.

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
