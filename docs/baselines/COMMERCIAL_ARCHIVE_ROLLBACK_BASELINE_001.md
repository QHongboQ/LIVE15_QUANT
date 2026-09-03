# Commercial archive rollback baseline 001

**Status:** FROZEN_ROLLBACK_BASELINE  
**Production mutation:** NONE  
**Baseline main:** `3cc4f26beef1993674f8440ba94356ff7b90d790`

## Purpose

Freeze the current verified LIVE15 WebSocket archive contract before replacing any generic archive responsibility with Arrow, Zstandard, S3/MinIO, Parquet, DuckDB, or another upstream component.

This baseline is the rollback and comparison target for `COMMERCIAL_ARCHIVE_UPSTREAM_ASSEMBLY_001`. It does not authorize a new archive implementation, Production deployment, data deletion, compaction, or training.

## Frozen current implementation

- HOT truth store: SQLite.
- Raw COLD truth: canonical JSONL encoded into immutable zlib chunks.
- Archive manifest: separate SQLite manifest.
- Default HOT retention: 21,600 seconds / six hours.
- Raw archive is replay truth; compact/model-facing state is not a substitute for raw events.
- Current archive wire version: 1.

Frozen source blobs:

| owner | blob SHA |
| --- | --- |
| `src/live15_quant/ws_archive.py` | `98378d911b3e0cc73bb6b643b882c630aac436f8` |
| `src/live15_quant/ws_retention.py` | `e3afd218d73dc5e70bd615e5fb556665fec274dc` |
| `src/live15_quant/config.py` | `098416532216d102be6a275b6871efe339495fb2` |
| `tests/test_ws_archive.py` | `8bfe826a76c113ffdee801c3be3a7c686a7fa586` |
| `tests/test_ws_retention.py` | `53673e94917150baa6fe29119028a97cb95c0ff4` |
| `docs/storage_scaling.md` | `f5ac2e01d0bd32b1ddc851dfd6e9441d6cf094bc` |

The machine-readable copy is `COMMERCIAL_ARCHIVE_ROLLBACK_BASELINE_001.json`.

## Frozen semantic and safety contract

The replacement must preserve all of the following:

- exact required values and Decimal semantics;
- arrival order and exact raw event identity;
- sequence gaps are preserved and never silently repaired;
- immutable archive chunks;
- same-filesystem staged/atomic publication before manifest authorization;
- file and logical checksum verification;
- deterministic source-vs-archive replay equivalence;
- manifest commit before purge eligibility;
- archive reopen/reverification immediately before purge;
- exact contiguous ID-range deletion only;
- restart-safe archive and partial-purge recovery;
- a partial purge may resume only from an exact contiguous suffix;
- FAILED/unverified chunks never authorize deletion;
- a FAILED chunk blocks later ranges instead of skipping raw truth;
- storage pressure may fail closed but may not silently drop Recorder truth.

The state-machine intent remains:

`WRITING -> WRITTEN -> CHECKSUM_VERIFIED -> REPLAY_VERIFIED -> COMMITTED -> PURGE_ELIGIBLE -> PURGED`

FAILED/quarantined evidence remains diagnostic and cannot be used to bypass verification.

## Frozen real-data benchmark

The existing offline audit used 100,000 arrival-ordered real records from one indexed subscription stream. Every candidate decoded and replayed to the same final order-book SHA-256 (`3060da16…acd3`) with the same sequence/gap semantics and exact Decimal strings.

| scheme | bytes/event | compression ratio vs current SQLite | write events/s | replay events/s |
| --- | ---: | ---: | ---: | ---: |
| current SQLite row/event | 645.45 | 1.00x | 20,114 | 4,551 |
| compact normalized SQLite | 420.78 | 1.53x | 37,355 | 4,582 |
| canonical JSONL + zlib | 32.33 | 19.96x | 44,787 | 4,597 |
| compressed chunk + SQLite manifest | **32.50** | **19.86x** | **19,509** | **4,601** |

The current manifest-backed archive is therefore the performance and semantic rollback target. A future Arrow IPC + ZSTD candidate does not pass merely by becoming smaller or faster; it must also preserve the complete safety contract above.

## Frozen storage context

Historical measured/derived context from `docs/storage_scaling.md`:

- WS SQLite growth: about **63.15 GiB/day** at the measured WS-only rate.
- Long-sample COLD archive estimate: about **3.2 GiB/day**.
- Conservative first-production-chunk COLD estimate: about **4.3 GiB/day**.
- Strict six-hour WS HOT component: about **15.8 GiB**.
- Near-term compact HOT SQLite expectation including current non-WS tables: about **18–22 GiB**.

These figures are comparison evidence, not a claim that current Production is presently at exactly those rates. Any new benchmark must state its own snapshot/time/rate basis.

## Regression gate for replacements

At minimum, future raw-serialization/compression replacements must continue to prove the existing contracts covered by `tests/test_ws_archive.py` and `tests/test_ws_retention.py`, including round-trip equality, checksum rejection, crash boundaries, replay equivalence, gap preservation, state-machine ordering, exact bounded purge, restart recovery, post-verification corruption rejection, preserved-archive verification, and fail-closed behavior.

## Rollback rule

Until an upstream candidate passes semantic-equivalence, recovery, integrity, replay, performance, and rollback gates, the current JSONL + zlib + SQLite manifest owner remains the accepted raw archive implementation. No candidate may retire it on popularity or benchmark performance alone.
