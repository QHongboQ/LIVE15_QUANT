# EVID-001B — Immutable Model vNext Dataset Freeze

Status: `FROZEN_DEVELOPMENT_DATASET`

This report records the immutable evidence freeze at the pre-registered cutoff
`2026-08-25T19:35:14.898895+00:00`. It is a data/target contract artifact only:
no model training, model evaluation, MVN-003 work, Paper wiring, or Production
write was performed.

## Frozen identity

- Dataset: `live15-dataset-v2-4bb4934bf328b6b024ff`
- Deterministic build hash: `4bb4934bf328b6b024ff4183df134c481d962a041dc6ae760a3816d3c5228113`
- Artifact root: `data/datasets/live15-dataset-v2-4bb4934bf328b6b024ff`
- Source identity: `0cd0f7c314ef72be13a65bfee27fde4e0c4f46c9242491de9d1563a6aa110002`
- Source row-id limits: settlements `4280`, lifecycle `16969`, quotes `1438050`, Coinbase `12927989`, underlying `3154834`, gaps `40018`.

The pool was read from the EVID-001A-audited `current_trainable.sqlite3`
materialization (`dataset-builder-shared-v1`), bounded to those registered
limits. Dataset v1 remains untouched.

The pinned materializer lineage is dataset version `1.2.0`, materializer schema
`1`, feature schema `1.0.0`, and eligibility policy
`dataset-builder-shared-v1`. Row-level identity is carried by the canonical
training-row hash and each row retains event/ticker/decision/provenance fields.

## Fast-freeze performance

- Fast immutable freeze elapsed: 30.36 seconds.
- Materializer DB read: 546,529,280 bytes; frozen JSONL/manifest output:
  608,562,697 bytes.
- The interrupted 10-GB raw replay is preserved as an audit/rebuild path only;
  it is not part of the normal freeze path and no full replay was completed.

The deferred engineering item is `STORAGE-002 — Data Pipeline Throughput &
Freeze Architecture` (Recorder ingest, archive/materializer throughput, freeze
I/O, backlog ETA, and disk headroom). It was not executed here.

## Storage-pipeline responsibility boundary

1. Recorder: continuous raw truth and authoritative persistence.
2. WS Archive: hot retention and cold raw storage.
3. Materializer: continuous leakage-safe feature/eligibility computation.
4. Dataset Freeze: cheap immutable copy of already-materialized eligible evidence.
5. Raw Replay: slow audit, rebuild, and disaster-recovery path only.

The normal EVID-001B path is steps 3 → 4. It does not mix raw replay with the
freeze operation.

## Contract and evidence

- Audited pool: 3,519 eligible events / 26,032 rows; frozen artifact after two
  embargo bundles: 3,489 events / 25,975 rows.
- Independent UTC days: 6 (`2026-08-20` through `2026-08-25`).
- Path targets: 5s, 15s, 30s, 60s, 120s, 180s, 300s, and `window_end`.
- Target matching tolerance: ±2 seconds; no forward fill, interpolation, or
  future-nearest join. Valid counts are 0 / 0 / 1,391 / 5,521 / 14,473 /
  6,024 / 5,949 / 0 respectively.
- Split: whole event/window bundles in chronological order, with 600-second
  purge and 600-second embargo; 122 rows in the two excluded bundles.
- Fresh holdout: chronological test tail marked `UNREVEALED_FROZEN`; no holdout
  evaluation was performed.
- LeakageChecker: `PASS`; final Dataset v1 final test was not consumed.
- Independent Checker: `PASS` for the fast-freeze boundary, provenance,
  chronological split, purge/embargo, holdout, and no-training constraints.
- Runtime verification was read-only: Recorder SDK/WS was synchronized and the
  Materializer checkpoint was advancing. Overall health was `DEGRADED` only
  because the existing WS archive source reported a failure; the freeze did not
  change that runtime state.
- Evidence remains development-only. Microstructure is
  `INSUFFICIENT_MICROSTRUCTURE_EVIDENCE`; sequence gate remains
  `INSUFFICIENT_SEQUENCE_EVIDENCE`; high-volatility coverage is zero.

## Files and boundaries

The artifact contains `training_rows.jsonl`, `path_targets.jsonl`, `splits.json`,
and `manifest.json`. Large files remain under ignored `data/` and are not added
to Git. Only this lightweight report and the reproducible freeze utility are
committed. Runtime, Recorder, Dataset v1 identity, models, Paper, Production,
and settlement semantics were not modified.

Next action is explicit review before any MVN-002 structured baseline work;
there is no automatic promotion or MVN-003 start.
