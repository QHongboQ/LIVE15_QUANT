# LIVE15 current state

## Source-of-truth rule

This file holds only stable workstream orientation. It is not a runtime receipt,
deployment log, or historical evidence store. Runtime facts come from the
current service/health evidence; research facts come from the Research Data
Authority and its evidence artifacts.

## Current phase

**Pre-training reliability/storage closeout plus current-main deployment proof gate.** The tracked
reliability workstream is execution-gated before the formal long-run training GO/NO-GO gate.

ControlCenter current truth is `docs/project-brain/capabilities/control-center.md`; completed
foundation evidence is the `PROJECT_PROGRESS.md` ledger.

## Workstream orientation

| Area | State | Authoritative source |
| --- | --- | --- |
| Kalshi WS / DataGap reliability | **TRACKED / EXECUTION_GATED** | `WS-RESYNC-001 + GAP-002`; dependency closure is complete, while critical-path prerequisite stabilization and frozen baseline remain required before direct execution. |
| Archive/purge throughput | **IN_PROGRESS** | `ST-005`, retention manifests and bounded trend evidence |
| Nomad secure migration | **VERIFIED** | `docs/project-brain/capabilities/control-center.md` |
| Production runtime closeout | **READY_FOR_PHASE_A_PREFLIGHT / HUMAN_GATE_PENDING_DEPLOYMENT_PROOF** | Current installed package, service health, and approved runtime evidence |
| Research coverage | Typed H0/H1/H2 authority | `docs/research_data_authority.md` and `/api/research-data` |
| Dataset/model promotion | Requires fresh forward challenger evidence | `docs/model_vnext_contract.md`, model lineage |
| Hard Risk / Production writes | Human-authorized only | `PROJECT_CHARTER.md`, `AGENTS.md` |

## Current limits

`MERGED != DEPLOYED`; `DEPLOYED != VERIFIED`. `WS-RESYNC-001 + GAP-002` remains tracked but
direct execution awaits critical-path prerequisite stabilization and the GAP002 frozen baseline;
`GAP002_DEPENDENCY_AUDIT_EXECUTED = YES`. Detail is recursively routed through
`capabilities/README.md`, not duplicated here.

## Current execution route

The approved sequence, GAP002 dual-lane strategy, and later gates are owned by
`docs/project-brain/plan/current-roadmap.md`. Capability detail is routed by
`docs/project-brain/capabilities/README.md`; execution constraints by
`docs/project-brain/constraints/README.md`.

## Update policy

Update this file only when a durable workstream state or source-of-truth rule
changes. Put measurements, PIDs, timestamps, and transient incidents in their
bounded evidence artifacts instead. Do not use this file to freeze or override
the separate runtime-closeout result.
