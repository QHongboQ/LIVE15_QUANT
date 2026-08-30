# Runtime and upstream migration boundary

Revision: R1
Status: permanent routing constraint.

## What it is

Routes platform/generic work to native upstream mechanisms and preserves LIVE15 domain authority.

## Current truth

Upstream replacement is subtractive. Nomad/SCM may own generic lifecycle where justified; React
Admin/MUI, Vector, and Grafana retain approved/conditional status. NATS, DuckDB/Polars/Arrow need
measured need; Consul, Temporal, Kafka, and Redpanda are not introduced merely because mature.

Production writes remain disabled. LIVE15 retains Recorder/settlement truth, gap semantics,
as-of/freshness/synchronization, quarantine, RDA, Hard Risk, and execution authorization.

## Interfaces / dependencies

`AGENTS.md`; `PROJECT_CHARTER.md`; `docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md`;
`docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md`.

## Read next

For a platform task follow the task-time upstream-resolution gate in `docs/agents/change-protocol.md`.

## Update rule

Update only for an approved architecture or upstream-replacement decision.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 execution-boundary baseline, moved without semantic change. |
