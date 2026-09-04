# LIVE15 current state

## Source-of-truth rule

This file holds stable workstream orientation only. Runtime facts come from current service/health evidence; research facts come from the Research Data Authority and bounded evidence.

## Current phase

**STABILIZATION / RECOVERY FREEZE.** GAP002 remains closed/pass, but normal feature, archive, WTI,
runtime-rollout, and model progression is on HOLD while two independent Production blockers are
recovered. The sole current responsibility is **RECORDER-PYTH-CRITICALITY-RECOVERY**.

The [audit receipt](docs/evidence/LIVE15_FULL_SYSTEM_ROOT_CAUSE_AUDIT_001.md) proved that a complete Pyth transport outage can exhaust bounded recovery,
terminate the whole Recorder, and trigger a recurring Nomad restart even while Kalshi re-synchronizes.
It also proved that current ControlCenter Nomad ownership and the desktop Web launch path are not
reconciled with host reality. Immutable Runtime preparation succeeded but was disproven as the root
cause of the Recorder loop.

## Workstream orientation

| Area | State | Authoritative source |
| --- | --- | --- |
| Kalshi WS / DataGap reliability | **CLOSED / PASS** | `PROJECT_PROGRESS.md` |
| Stabilization / recovery | **CURRENT / RECOVERY FREEZE** | `docs/project-brain/plan/current-roadmap.md` |
| Recorder / Pyth criticality | **BLOCKER / SOLE NEXT** | Recorder authority; current roadmap |
| ControlCenter / Web ownership | **BLOCKER / RECOVERY REQUIRED** | ControlCenter authority; current roadmap |
| Host Production acceptance | **REQUIRED / AFTER RECOVERY-2** | current roadmap |
| Commercial archive/package | **HOLD / NORMAL PROGRESSION PAUSED** | `docs/project-brain/plan/current-roadmap.md` |
| Cold format | **PARQUET+ZSTD SELECTED / MERGED** | PRs #157/#158 |
| Named multi-root archive layout | **MERGED** | PR #160 |
| Production Parquet acceptance | **HOLD / AFTER RECOVERY FREEZE** | current roadmap; PR #159 historical only |
| Runtime deployment simplification | **MERGED / PRESERVED / ROLLOUT HOLD** | runtime authority; PR #167 |
| WTI retirement | **HOLD / AFTER STABILITY OBSERVATION** | current roadmap |
| Archive/purge throughput | **CAPACITY PROBLEM / SAFETY GATES PRESERVED** | Recorder throughput authority |
| Runtime/Lifecycle consolidation | **CODE/MIGRATION MERGED; RECORDER RECOVERY REQUIRED** | runtime and Recorder authorities |
| Web Application Shell | **CODE/MIGRATION MERGED; HOST OWNERSHIP NOT RECONCILED** | ControlCenter authority |
| Vector telemetry | **DEFERRED / LATER CANDIDATE** | upstream replacement matrix |
| Dataset/model promotion | **BLOCKED BY EXISTING GATES** | model authorities |
| Hard Risk / Production writes | **HUMAN-AUTHORIZED ONLY** | `PROJECT_CHARTER.md`, `AGENTS.md` |

## Current limits

`MERGED != DEPLOYED`; `DEPLOYED != VERIFIED`. The recovery freeze does not authorize a Recorder
restart, ControlCenter restart, Nomad deployment, runtime rollout, archive activation, purge,
training, holdout access, Hard Risk changes, or trading writes.

## Current execution route

The sole approved execution sequence is owned by `docs/project-brain/plan/current-roadmap.md`.

## Update policy

Update only when durable workstream state or source-of-truth routing changes. Keep transient receipts, PIDs, timestamps, and measurements in bounded evidence.
