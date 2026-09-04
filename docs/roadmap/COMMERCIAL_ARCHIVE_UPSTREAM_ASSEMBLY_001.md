# Commercial archive upstream assembly plan

**Status:** approved non-current design reference; selection state reconciled through PR #160.
**Scope:** data archive/package lifecycle only. Current execution ordering remains owned by
`docs/project-brain/plan/current-roadmap.md`; this document does not authorize Production mutation.

## Goal

Move LIVE15 toward a commercial-grade HOT/COLD archive lifecycle by retaining LIVE15-specific truth
and safety semantics while using mature upstream components for generic serialization, compression,
file formats, storage, and analytical reads.

The design principle remains:

> Keep LIVE15-specific adapters, replay semantics, manifest state, and purge authorization. Reuse
> upstream implementations for commodity file-format, compression, storage, and analytical work.

## Resolved selection state

The original Arrow-IPC-first candidate plan has been resolved by later bounded evidence and merged
implementation:

- PR #157 benchmarked one fixed replayable stream across the candidate archive formats. Parquet +
  ZSTD ranked first at 19.51 bytes/event while preserving exact Decimal/timestamp/order semantics and
  deterministic replay. Arrow IPC + ZSTD remained correct but was materially larger and is retained
  only as benchmark/prototype capability.
- PR #157 explicitly did **not** select S3 or MinIO. Cold object storage remains a future candidate,
  not the current Production-acceptance gate.
- PR #158 made PyArrow Parquet + ZSTD the Production-capable verified cold archive format while
  preserving LIVE15 semantic digest, replay verification, manifest transitions, bounded idempotent
  purge authorization, crash recovery, and fail-closed behavior. Production activation stayed off.
- PR #160 bound archive chunks to named storage roots with a centralized manifest and one active
  writer root. Unknown, missing, or escaping roots fail closed.

Therefore the current selected local cold path is **HOT SQLite -> LIVE15/Arrow semantic mapping ->
Parquet + ZSTD -> named archive root -> semantic/replay verification -> manifest eligibility**.
Arrow remains the in-memory schema/RecordBatch bridge and benchmark codec; Arrow IPC is not the
selected cold artifact. S3/MinIO and DuckDB remain later responsibility candidates and are not
prerequisites for the current Parquet Production acceptance.

## Safety baseline to preserve

The following LIVE15-owned behavior remains mandatory regardless of upstream components:

- HOT SQLite remains the mutable Recorder truth until a separate storage-owner decision changes it;
- immutable bounded archive chunks;
- separate archive manifest and explicit state transitions;
- atomic publication and file integrity checks;
- exact LIVE15 semantic digest and deterministic order-book replay verification;
- contiguous-range retention authorization;
- fail-closed purge behavior;
- restart-aware progress/recovery;
- archive truth separated from compact model-facing state.

The state-machine intent remains:

`WRITING -> WRITTEN -> CHECKSUM_VERIFIED -> REPLAY_VERIFIED -> COMMITTED -> PURGE_ELIGIBLE -> PURGED`

Generic implementation details may change only when semantic/recovery/rollback gates remain at least
as strong.

## Current responsibility assembly

| Responsibility | Selected/current direction | Disposition |
| --- | --- | --- |
| HOT mutable store | SQLite Recorder store | RETAIN |
| Event/schema bridge | PyArrow schema / RecordBatch with LIVE15/Kalshi mapping | RETAIN thin LIVE15 mapping |
| Verified cold file | PyArrow Parquet + ZSTD | SELECTED / MERGED, Production acceptance pending |
| Cold local layout | named roots + centralized SQLite manifest, one active writer root | SELECTED / MERGED |
| Semantic/replay verification | LIVE15 digest + deterministic Kalshi replay | RETAIN |
| Manifest / purge authorization | LIVE15 retention state machine | RETAIN |
| Arrow IPC | benchmark/prototype codec only | HISTORICAL / NOT SELECTED COLD FORMAT |
| S3 / MinIO | future durable-object-storage candidate | DEFERRED / NOT SELECTED |
| DuckDB / PyArrow analytical reads | future file-native analytical access | DEFERRED |
| Iceberg / larger catalog | only if demonstrated catalog/multi-engine scale requires it | DEFERRED |

## Current architecture

```text
LIVE15 Recorder
      |
      v
HOT SQLite
      |
      v
LIVE15 event adapter
(Kalshi/LIVE15 semantics)
      |
      v
PyArrow RecordBatch
      |
      v
Parquet + ZSTD
      |
      v
named local archive root
      |
      v
file SHA + LIVE15 semantic/replay verification
      |
      v
manifest COMMITTED / PURGE_ELIGIBLE
      |
      v
bounded purge only after separate authorization
```

The current acceptance path stops before purge. Object storage and analytical-query expansion are
later responsibilities and must not be pulled into the Runtime/Recorder/Phase1 gate.

## Responsibility boundaries

### PyArrow / Parquet

PyArrow owns columnar arrays, Parquet encoding/decoding, Zstandard integration, and standard file
format behavior. LIVE15 owns only its event-to-schema mapping and the semantic/replay checks needed
to prove exact reconstruction.

Parquet + ZSTD is the selected verified cold artifact for the current archive lifecycle. A merged
implementation is not a Production acceptance: the Recorder/runtime and bounded Phase1 gates still
must pass before any archive activation.

### Arrow IPC

Arrow IPC remains useful for fixed-snapshot benchmarking and format comparison. It is not a second
Production cold format and must not become a parallel archive path.

### S3 / MinIO

No S3/MinIO adoption decision has been made. If local named roots later cease to satisfy durability
requirements, object storage may be evaluated as a separate bounded responsibility with current
official upstream evidence, integrity/recovery proof, and rollback. It is not part of the immediate
Runtime rollout, WTI retirement, or Parquet Phase1 acceptance.

### DuckDB / analytical access

Use mature file-native readers if/when analytical access becomes a demonstrated need. They consume
verified Parquet artifacts and do not own Recorder truth, archive publication, or purge authorization.

### Manifest and catalog

Keep the current SQLite archive manifest while it carries LIVE15-specific replay/purge state and no
demonstrated catalog bottleneck exists. Do not add Iceberg, Nessie, lakeFS, Hudi, or Delta merely to
replace SQLite.

## Non-self-reimplementation rule

1. Resolve the existing LIVE15 owner first.
2. Use mature upstream components for generic file-format/compression/storage/query behavior.
3. Keep new LIVE15 code limited to domain mapping, replay/continuity semantics, manifest safety, and
   thin orchestration glue.
4. Do not create a second serialization, compression, object-storage, or analytical engine when the
   selected upstream component already owns that responsibility.
5. Do not create a second archive format merely for compatibility with the superseded candidate plan.

## Resolved and remaining sequence

Resolved by bounded work:

1. legacy archive contract and real snapshots benchmarked;
2. Arrow IPC prototype/equivalence benchmark completed;
3. commercial bakeoff selected Parquet + ZSTD (#157);
4. Production-capable Parquet closed loop merged with archive activation disabled (#158);
5. named multi-root layout merged (#160).

Remaining execution is owned by the current roadmap, not by this design reference. At the present
boundary it consists of the Runtime/Recorder gate needed for PyArrow-capable Production execution,
separate WTI retirement, then a fresh bounded Parquet Production Phase1 acceptance that stops before
purge. S3/MinIO, DuckDB, and catalog expansion remain later candidates.

## Acceptance gates

No merged implementation or popular upstream component is Production-adopted merely by existence.
Each bounded step must preserve or prove:

- no loss of Recorder truth;
- exact Decimal/timestamp/identity semantics;
- deterministic replay equivalence;
- crash-safe publication and restart-safe recovery;
- integrity coverage at least as strong as the prior verified baseline;
- no weakening of contiguous-range purge authorization;
- no silent data loss under storage pressure;
- one clear owner per responsibility;
- documented rollback to the prior verified owner/state.

## Explicitly deferred

- replacing the HOT Recorder database;
- S3/MinIO adoption before a separate durability decision;
- DuckDB/PyArrow analytical-query rollout before a demonstrated consumer need;
- Kafka/Spark/Flink merely to imitate large-company scale;
- Iceberg/Hudi/Delta/catalog migration before scale requires it;
- model selection, factor mining, training cadence, or promotion logic.

## Read with

- `docs/evidence/LIVE15_COMMERCIAL_STORAGE_BAKEOFF_001.md` for the fixed-stream format result.
- `docs/project-brain/capabilities/records/recorder/truth.md` for Recorder truth ownership.
- `docs/project-brain/plan/current-roadmap.md` for the sole current execution order.
- `docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md` for generic replacement classification.
- `docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md` for replacement mechanics.

## Update rule

Update only when the approved archive/package design changes. Benchmark/evidence history remains in
bounded evidence; current execution ordering remains in the sole Project Brain roadmap.
