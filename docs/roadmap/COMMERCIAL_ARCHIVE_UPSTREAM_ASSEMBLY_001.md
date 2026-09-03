# Commercial archive upstream assembly plan

**Status:** approved non-current design reference.  
**Scope:** data archive/package lifecycle only; this does not change the sole current execution mainline or authorize Production mutation.

## Goal

Move LIVE15 toward a commercial-grade HOT/COLD archive lifecycle by retaining LIVE15-specific truth and safety semantics while replacing generic infrastructure with mature upstream components wherever practical.

The design principle is:

> Keep LIVE15-specific adapters, replay semantics, and purge authorization. Reuse upstream implementations for commodity serialization, compression, analytical file formats, object storage, and file-native analytics.

This plan deliberately avoids a greenfield rewrite and avoids selecting one monolithic upstream project. The target is a bounded assembly of mature components, responsibility by responsibility.

## Current validated baseline to preserve

The existing archive lifecycle has already established important safety behavior that must survive any replacement:

- bounded HOT SQLite retention;
- immutable archive chunks;
- separate archive manifest;
- atomic publication;
- checksum verification;
- deterministic order-book replay verification;
- contiguous-range retention authorization;
- fail-closed purge behavior;
- restart-aware progress and recovery;
- archive truth separated from compact model-facing state.

The existing state-machine intent remains authoritative until a replacement proves semantic equivalence:

`WRITING -> WRITTEN -> CHECKSUM_VERIFIED -> REPLAY_VERIFIED -> COMMITTED -> PURGE_ELIGIBLE -> PURGED`

Generic implementation details may be replaced, but these safety gates must not be weakened merely to adopt an upstream library.

## Target responsibility assembly

| Responsibility | Current LIVE15 implementation | Preferred upstream direction | Planned disposition |
| --- | --- | --- | --- |
| HOT mutable store | SQLite Recorder store | SQLite initially | RETAIN until a separate storage-owner decision proves a better fit |
| Event/schema bridge | LIVE15-native records | Apache Arrow schema / RecordBatch adapter | RETAIN only LIVE15/Kalshi mapping logic |
| Raw immutable serialization | canonical JSONL + zlib chunks | Apache Arrow IPC | CANDIDATE REPLACEMENT |
| Compression | Python zlib | Zstandard through Arrow/standard upstream codec | CANDIDATE REPLACEMENT |
| Analytical historical format | ad-hoc/model-specific artifacts | Apache Parquet | ADD as derived analytical tier |
| Cold durable storage | local archive directory | AWS S3; MinIO as S3-compatible local/self-hosted option | CANDIDATE REPLACEMENT |
| Object integrity | LIVE15 SHA-256 checks | S3/native object checksum plus retained logical verification | PARTIAL REPLACEMENT |
| Historical analytical query | bespoke readers / SQLite paths | DuckDB + PyArrow over Parquet/object storage | ADD / REPLACE generic readers where suitable |
| Archive manifest / retention state | SQLite manifest | retain near-term; evaluate Apache Iceberg only when scale/catalog needs justify it | RETAIN FOR NOW |
| Domain replay verification | LIVE15 deterministic Kalshi replay | no generic upstream substitute | RETAIN |
| Purge authorization / safety gate | LIVE15 retention state machine | no generic upstream substitute | RETAIN |

## Target architecture

```text
LIVE15 Recorder
      |
      v
HOT SQLite
      |
      v
LIVE15 event adapter
(Kalshi/LIVE15 semantics only)
      |
      v
Apache Arrow RecordBatch
      |
      +------------------------------+
      |                              |
      v                              v
Arrow IPC + ZSTD                 Apache Parquet
RAW replay truth                ANALYTICAL tier
      |                              |
      v                              v
AWS S3 / MinIO                  AWS S3 / MinIO
      |                              |
      v                              v
object checksum                 DuckDB / PyArrow
      |
      v
LIVE15 logical/replay verification
      |
      v
manifest commit + retention authorization
      |
      v
bounded HOT purge
```

## Responsibility boundaries

### 1. Apache Arrow

Use Arrow for standardized schema, columnar batches, IPC encoding, and supported compression rather than maintaining a LIVE15-specific general-purpose wire format when equivalent behavior is proven.

LIVE15 remains responsible only for mapping Kalshi/LIVE15 event semantics into the Arrow schema and for validating that the encoded representation preserves exact required values and replay ordering.

### 2. Apache Parquet

Parquet is a derived analytical tier, not the sole raw replay truth.

It is intended for research, feature generation, historical scans, and future training reads. A Parquet publication must not authorize deletion of raw truth unless the raw archive safety contract separately passes.

### 3. AWS S3 / MinIO

Object storage becomes the commercial-grade cold durability boundary. Local disk may remain a cache or staging area, but durable archive truth should not depend on one workstation volume once this phase is adopted.

MinIO is acceptable only as an S3-compatible deployment option; the archive API boundary should remain portable to AWS S3.

### 4. DuckDB / PyArrow

Use upstream file-native readers for historical analytical access instead of growing bespoke historical query infrastructure. These components consume Parquet/Arrow artifacts and do not own Recorder truth or purge authorization.

### 5. Manifest and catalog

Keep the current SQLite archive manifest while the workload is single-system and the manifest also carries LIVE15-specific replay/purge state. Do not introduce Iceberg, Nessie, lakeFS, Hudi, or Delta solely to remove SQLite.

Evaluate Iceberg later only if file counts, multiple compute engines, schema evolution, or multi-writer/catalog requirements create a demonstrated need.

## Mandatory non-self-reimplementation rule

For this archive/package responsibility class:

1. Resolve the existing LIVE15 owner first.
2. For generic infrastructure, search mature upstream implementations before adding LIVE15-specific code.
3. Prefer adapting or wrapping an upstream component over reimplementing its general-purpose behavior.
4. New LIVE15 code should be limited primarily to:
   - Kalshi/LIVE15 event adapters;
   - domain replay and continuity semantics;
   - manifest orchestration needed to preserve existing safety gates;
   - bounded glue between upstream components.
5. Do not rewrite serialization, compression, object-storage clients, Parquet readers, or general analytical SQL engines when the selected upstream already owns that responsibility.

## Migration sequence

This is not the current execution mainline. When explicitly promoted to current work, migrate one responsibility at a time:

1. Freeze and benchmark the current archive contract as the rollback baseline.
2. Prototype Arrow schema + IPC on fixed real snapshots only.
3. Prove byte/value/ordering fidelity and deterministic replay equivalence against the current archive.
4. Benchmark Arrow IPC + ZSTD against the current JSONL + zlib archive for size, write rate, replay rate, failure recovery, and operational complexity.
5. If accepted, replace only the raw serialization/compression responsibility; keep the existing manifest and purge gates.
6. Add S3-compatible object storage behind a bounded archive-store interface; prove atomic/verified publication and rollback.
7. Add derived Parquet publication from verified raw archive data.
8. Add DuckDB/PyArrow analytical access over the derived Parquet tier.
9. Reassess whether the SQLite manifest is still an actual bottleneck before considering Iceberg or another catalog layer.
10. Retire superseded generic LIVE15 implementations only after independent verification and rollback proof.

## Acceptance gates

No upstream replacement is adopted merely because it is popular or standard. Each bounded replacement must prove:

- no loss of Recorder truth;
- exact required Decimal/timestamp/identity semantics;
- deterministic replay equivalence where applicable;
- crash-safe publication;
- restart-safe recovery;
- checksum/integrity coverage at least as strong as the current baseline;
- no weakening of contiguous-range purge authorization;
- no silent data loss under storage pressure;
- measurable operational or maintenance advantage;
- documented rollback to the previous verified owner.

## Explicitly deferred

The following are not part of the initial assembly:

- replacing the HOT Recorder database;
- Kafka/Spark/Flink merely to imitate large-company scale;
- Hudi/Delta for append-only raw truth without a demonstrated update/delete requirement;
- Iceberg catalog migration before scale requires it;
- lakeFS/Nessie-style dataset version control without a real versioning need;
- model selection, factor mining, training cadence, or promotion logic.

## Commercial-system analogy

The intended architectural pattern is analogous to mature financial data platforms that separate ingestion, durable raw storage, processed analytical storage, and downstream consumers. LIVE15 should emulate the responsibility boundaries and failure discipline, not the infrastructure scale of a multi-petabyte company.

## Read with

- `docs/storage_scaling.md` for the existing validated HOT/COLD baseline and benchmark evidence.
- `docs/project-brain/capabilities/records/recorder/truth.md` for Recorder truth ownership.
- `docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md` for responsibility-by-responsibility upstream replacement policy.
- `docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md` for replacement mechanics.

## Update rule

Update this document only when the approved archive/package upstream assembly design changes. Do not turn it into a second current roadmap. Promotion to active execution must update the sole current roadmap through the normal Project Brain authority process.
