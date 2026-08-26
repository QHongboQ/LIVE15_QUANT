# BASELINE-001B — Data Foundation Closeout

Status: **AUDIT COMPLETE — DEVELOPMENT/RESEARCH BASELINE**

Protected main at audit: `da83c473cf66f84b4d3621d9ef55f7164c4ba9dc` (PR #14 squash merge).
No model training, Dataset v2 mutation, holdout access, Recorder mutation, Paper activation,
or Production writes were performed.

## Evidence layers

| Layer | Evidence | Gate |
|---|---|---|
| H0 Recorder | SDK-authoritative `data/live15.sqlite3`; 10 markets, 5 archive/checkpoint UTC days; gaps retained and typed | HEALTHY / read-only audit |
| Current trainable | 4,350 events, 26,402 rows, 3,582 eligible events, 6 UTC days, 10 assets | Mutable materializer output; not a frozen dataset |
| H1 historical | `historical-research-f2d529adfb95080971becdaf`, 90 days, 59,056 markets, 886,454 trades, 5,242 candles | Official Kalshi research layer; 8-fold plan only |
| H2 microstructure | DepthFeed snapshot/delta evidence not materialized | `INSUFFICIENT_MICROSTRUCTURE_EVIDENCE` |
| Frozen dataset | `live15-dataset-v2-4bb4934bf328b6b024ff`; holdout `UNREVEALED_FROZEN` | Untouched and unaccessed |

## Sequence readiness reconciliation

EVID-RECON-001's `SEQUENCE_READY_FOR_BOUNDED_MODEL_TRAINING` was valid only for its explicitly
stratified H1 artifact: 7 UTC days, 144 events, 10,653 causal sequences, and four expanding folds
with a 600-second purge/embargo. The data did not regress.

The discrepancy was a canonical aggregation bug. `CanonicalEvidenceSnapshot._aggregate_days()`
used a record's general `coverage_days` even when the requested capability (path/snapshot/delta)
had an explicit count of zero. H0 had six general coverage days but no materialized path rows, so
the old code falsely reported six H0 path days and allowed a `READY` Path preflight. The smallest
fix now filters coverage days by the requested capability. Regression coverage is in
`tests/test_canonical_evidence.py`.

With the fix, the current canonical preflight is:

- `h0_path_days=0`, `h1_path_days=7`, `h2_path_days=0`;
- canonical evidence status: `READY` (evidence exists and is internally consistent);
- `training_preflight(model_family="path_expert")`: `PARTIAL`;
- reason: `H0_PRIORITY_VALIDATION_REQUIRED`.

Thus Path research is bounded-development eligible on H1, but not globally READY for an H0-aware
tournament. No model training occurred.

## Integrity and readiness

- Canonical evidence gate: **PASS**.
- As-of/anti-leakage policy remains authoritative; no future fill, interpolation, or fabricated
  target was introduced. Five/15-second and window-end targets remain unavailable where the
  observation contract cannot be satisfied.
- Sequence evidence is **PARTIAL**; microstructure remains **INSUFFICIENT_MICROSTRUCTURE_EVIDENCE**.
- Structured Path preflight is **PARTIAL**, not READY; a Path Model Tournament is **not approved**.

## Storage lifecycle

| Capability | Actual state |
|---|---|
| HOT DB | 56,981,635,072 bytes; WAL 67,108,864 bytes; SHM 32,768 bytes |
| Archive | Implemented and automatic while Recorder runs; current Recorder is stopped |
| Archive verification | 33,247 verified chunks, 1 failed chunk; last success `2026-08-25T20:37:07Z` |
| Archive backlog | Blocked at failed chunk `ws-244812239-244812260`; 813 purge-eligible chunks remain |
| Purge | Implemented and automatic in the active Recorder loop, gated by archive/replay/checksum/contiguous-range proof |
| Compaction | Implemented as offline `compact-copy`; not automatic; current gate denied (0 reclaimable bytes; requires 8 GiB and 25%) |
| Growth estimate | Raw WS ≈29.2 GB/day; net disk sample ≈22.2 GB/day; short-window runway ≈7.1–9.4 days |

Archive, purge, and compaction are separate lifecycle stages. No destructive purge or compaction was
run in this closeout. The lifecycle is classified **`STORAGE_LIFECYCLE_PARTIAL`** because the local
manifest contains a failed chunk and current archive progress is stale, although the safety gates
remain fail-closed.

## Governance and residue

PR #14 (`32935451418`) passed CI and was squash-merged. PRs #12 and #1 remain open historical
work and require human review; they were not closed or deleted. Existing dirty files on local
`main`, active worktrees, runtime PID files, and the `.agents` dry-run example were preserved.
Only deterministic pytest cache/temp residue was targeted for cleanup; filesystem ACLs denied
removal, so no unverified deletion was attempted.

The annotated tag `data-foundation-v1` is intentionally deferred until this manifest/fix PR is
reviewed and merged under protected-main governance and the remaining PARTIAL preflight/storage
limitations are accepted.

## Reproducibility

Machine-readable details, observations, source identities, and validation status are in
[`data-foundation-001b.json`](data-foundation-001b.json). Raw SQLite databases and credentials
remain outside Git under ignored paths.
