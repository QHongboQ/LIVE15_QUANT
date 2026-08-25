# EVID-001A — Current Trainable Pool Evidence Audit

Status: `NEW_DATASET_FREEZE_ELIGIBLE`

This is a read-only evidence audit. It did not retrain MVN-002, start MVN-003, freeze a
dataset, consume Dataset v1 final-test rows, or mutate the Recorder/current pool.

## Evidence decision

The current trainable pool contains 3,519 eligible independent events and 26,032 materialized
rows across six usable UTC days (2026-08-20 through 2026-08-25). It contains 2,428 new eligible
events after the Dataset v1 source cutoff, with all ten assets represented. Exact path-target
counts increased materially at 30s, 60s, 120s, 180s, and 300s. The proposed next action is
`EVID-001B — Freeze New Immutable Dataset`.

The atomic synchronized WS microstructure gate remains closed: the SDK shadow reports zero
synchronized sequences, 53,736 recent gaps, and a 0.00397 top-depth match rate. Therefore
`MVN003_EVIDENCE_ELIGIBLE` and `SEQUENCE_RESEARCH_ELIGIBLE` are false; sequence readiness remains
`INSUFFICIENT_SEQUENCE_EVIDENCE`.

## Current pool

| Measure | Value |
| --- | ---: |
| evaluated finalized events | 4,280 |
| eligible events | 3,519 |
| excluded events | 761 |
| unevaluated events | 0 |
| materialized rows | 26,032 |
| independent UTC days | 6 |
| new events since Dataset v1 cutoff | 2,428 |
| assets | 10 |
| latest materialized timestamp | 2026-08-25T19:35:14.898895Z |

Accepted rows had zero observed source-timestamp as-of violations and zero stale feature values.
The canonical shared DatasetBuilder evaluator remains authoritative for receive-timestamp and
gap/quarantine semantics; accepted rows contain 18 typed `not_enough_lookback` missing values.

## Path targets

| Horizon | Eligible target events | Valid target rows | Unavailable/rejected |
| ---: | ---: | ---: | ---: |
| 5s | 0 | 0 | 26,032 |
| 15s | 0 | 0 | 26,032 |
| 30s | 1,402 | 1,402 | 24,630 |
| 60s | 3,086 | 5,547 | 20,485 |
| 120s | 3,369 | 14,579 | 11,453 |
| 180s | 3,153 | 6,051 | 19,981 |
| 300s | 3,110 | 5,978 | 20,054 |
| window_end | 0 | 0 | 26,032 |

Unavailable targets remain typed and were not filled, interpolated, or backfilled.

## Dataset v1 comparison

| Evidence | Dataset v1 | Current pool | Increase |
| --- | ---: | ---: | ---: |
| independent events | 1,091 | 3,519 | 2,428 |
| rows | 7,984 | 26,032 | 18,048 |
| UTC days | 3 | 6 | 3 |
| assets | 10 | 10 | 0 |
| usable 30s targets | 271 | 1,402 | 1,131 |
| usable 60s targets | 1,176 | 5,547 | 4,371 |
| usable 300s targets | 1,223 | 5,978 | 4,755 |

The complete machine-readable evidence, proposed freeze contract, integrity assertions, and
Checker scope are in [`model_vnext_evidence_audit_20260826_001.json`](model_vnext_evidence_audit_20260826_001.json).
