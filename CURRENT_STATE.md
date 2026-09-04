# LIVE15 current state

## Source-of-truth rule

This file holds stable workstream orientation only. Runtime facts come from current service/health
evidence; research facts come from the Research Data Authority and bounded evidence.

## Current phase

**Upstream-consolidation freeze with storage/archive Production-acceptance preparation.** GAP002 is
closed/pass. Runtime/Lifecycle consolidation and the Web Application Shell are **COMPLETE / VERIFIED**.
The current responsibility remains **COMMERCIAL ARCHIVE/PACKAGE UPSTREAM ASSEMBLY**.

Storage selection has advanced: PR #157 selected **Parquet + ZSTD**; PR #158 merged the verified
HOT->COLD closed loop; PR #160 merged named multi-root storage. PR #159 is historical safe-stop
evidence only and deleted no HOT rows. PR #167 resolved the Runtime/deploy blocker in code, but
`MERGED != DEPLOYED`: the host still uses the retained legacy runtime.

The archive mainline's immediate gate is immutable Runtime preparation/verification followed by a
separately authorized Nomad Recorder rollout. Then WTI is retired 10->9 as its own task before fresh
Parquet Phase1-002 with Recorder running and STOP BEFORE PURGE. Vector remains deferred; training
remains blocked.

## Workstream orientation

| Area | State | Authoritative source |
| --- | --- | --- |
| Kalshi WS / DataGap reliability | **CLOSED / PASS** | `PROJECT_PROGRESS.md` |
| Commercial archive/package | **PLANNED / NEXT** | `docs/project-brain/plan/current-roadmap.md` |
| Cold format | **PARQUET+ZSTD SELECTED / MERGED** | PRs #157/#158 |
| Named multi-root archive layout | **MERGED** | PR #160 |
| Production Parquet acceptance | **PENDING FRESH PHASE1-002** | current roadmap; PR #159 historical only |
| Runtime deployment simplification | **MERGED / HOST ROLLOUT PENDING** | runtime authority; PR #167 |
| WTI retirement | **PLANNED / BEFORE PHASE1-002** | current roadmap |
| Archive/purge throughput | **CAPACITY PROBLEM / SAFETY GATES PRESERVED** | Recorder throughput authority |
| Runtime/Lifecycle consolidation | **COMPLETE / VERIFIED** | runtime authority |
| Web Application Shell | **COMPLETE / VERIFIED** | ControlCenter authority |
| Vector telemetry | **DEFERRED / LATER** | upstream replacement matrix |
| Dataset/model promotion | **BLOCKED BY EXISTING GATES** | model authorities |
| Hard Risk / Production writes | **HUMAN-AUTHORIZED ONLY** | `PROJECT_CHARTER.md`, `AGENTS.md` |

## Current limits

`MERGED != DEPLOYED`; `DEPLOYED != VERIFIED`. Runtime preparation does not authorize Recorder rollout;
Recorder rollout does not authorize archive activation; acceptance does not authorize purge. Training,
holdout access, Hard Risk changes, and trading writes remain outside this lane.

## Current execution route

The sole approved sequence is `docs/project-brain/plan/current-roadmap.md`.

## Update policy

Update only when durable workstream state or source-of-truth routing changes. Keep transient receipts,
PIDs, timestamps, and measurements in bounded evidence.
