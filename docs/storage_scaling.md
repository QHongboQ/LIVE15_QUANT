# Storage scaling audit and lossless WS archive benchmark

This audit is intentionally offline. `live15-storage-audit` refuses the configured active recorder
database, requires a fixed WAL-free snapshot, opens it `query_only`/`immutable`, and creates all
benchmark artifacts in an auto-removed temporary directory. It never deletes HOT rows, runs
`VACUUM`, or modifies the production database.

## 2026-08-21 snapshot attribution

The consistent snapshot contained 3,159,599 4-KiB pages: 12,941,717,504 bytes (12.05 GiB).
Only seven pages were unowned/free metadata. The custom page attribution walks table and index
B-trees without the unavailable `dbstat` extension and was cross-checked against SQL row counts.

| object | rows/entries | allocated bytes | database % | bytes/entry |
|---|---:|---:|---:|---:|
| `kalshi_ws_orderbook_events` | 15,422,182 | 7,788,224,512 | 60.18% | 505.0 |
| `idx_kalshi_ws_event_ticker` | 15,422,182 | 1,233,981,440 | 9.54% | 80.0 |
| `idx_kalshi_ws_event_replay` | 15,422,182 | 831,111,168 | 6.42% | 53.9 |
| WS event unique index | 15,422,182 | 825,102,336 | 6.38% | 53.5 |
| `coinbase_ticks` | 2,020,136 | 518,516,736 | 4.01% | 256.7 |
| `kalshi_prediction_quotes` | 245,506 | 323,670,016 | 2.50% | 1,318.4 |
| `underlying_observations` | 809,674 | 302,252,032 | 2.33% | 373.3 |
| `secondary_underlying_observations` | 757,387 | 267,800,576 | 2.07% | 353.6 |
| `kalshi_market_lifecycle` | 4,218 | 5,079,040 | 0.039% | 1,204.1 |
| `kalshi_ws_book_checkpoints` | 597 | 1,560,576 | 0.012% | 2,613.9 |
| `kalshi_settlements` | 1,067 | 442,368 | 0.003% | 414.6 |
| `data_gaps` | 1,335 | 380,928 | 0.003% | 285.3 |
| `recorder_events` | 361 | 81,920 | <0.001% | 226.9 |

The WS event table plus its three indexes consume 10,678,419,456 bytes, **82.51% of the entire
database**, or 692.4 bytes/event. This is the source of the 10-GiB growth. Checkpoints are not the
cause. There is no duplicate-write bug under the exact `(connection_id, subscription_id,
sequence)` unique fact key; reconnect snapshots and ACKs are distinct replay facts, not duplicates.

The observed interval was 18:05:47–21:52:34 UTC (13,606.89 seconds):

- 15,421,318 deltas, 564 snapshots, and 300 sequenced ACKs;
- 1,133.4 events/s average and 4.08 million rows/hour;
- WS table+indexes: 2.63 GiB/hour, 63.15 GiB/day;
- during the later 47.98-minute audit interval, the active main file grew from the snapshot's
  12,941,717,504 bytes to 16,051,740,672 bytes: about **3.62 GiB/hour / 86.9 GiB/day**;
- unchanged for 30 days: about **2.55 TiB** at the observed total-file rate (peak observations can
  still project roughly 3.3 TiB/month).

The WS-only rate is derived from WS pages and the full WS timestamp span. The 86.9-GiB/day figure is
a separate observed file-size delta, not `total database size / WS uptime`; pre-existing tables make
that latter calculation invalid. WAL is reported separately and is reused, not added as daily growth.

Most repeated bytes are wide TEXT timestamps, connection/ticker/market/provenance strings, JSON
empty arrays, hashes, and the same identity in three indexes. This is representational repetition,
not duplicate truth. Deltas are more than 99.99% of WS records. Full reconstructed books are stored
only as sparse checkpoints.

## Four-way benchmark

The benchmark uses 100,000 arrival-ordered real records from one indexed subscription stream.
Every candidate is decoded and replayed; all four produced the identical final book SHA-256
`3060da16…acd3`, identical sequence range, gap semantics, and exact Decimal strings.

| scheme | bytes | bytes/event | ratio | write events/s | replay events/s |
|---|---:|---:|---:|---:|---:|
| exact current SQLite row/event schema | 64,544,768 | 645.45 | 1.0x | 20,114 | 4,551 |
| compact normalized SQLite | 42,078,208 | 420.78 | 1.53x | 37,355 | 4,582 |
| chunked canonical JSONL + zlib | 3,233,225 | 32.33 | 19.96x | 44,787 | 4,597 |
| compressed chunk + SQLite manifest | 3,249,609 | 32.50 | 19.86x | 19,509 | 4,601 |

The manifest candidate is recommended. The benchmark uses stdlib zlib so the clean Python 3.13
environment needs no extra native dependency; a later zstd codec can be evaluated behind the same
versioned wire format. Compression is lossless, not a sampled substitute. Random access is at
chunk granularity using manifest time/ticker/sequence ranges; SQLite is better for single-row random
access but about 20x larger. Chunk writes use a same-filesystem partial file, flush/fsync, atomic
publish, reopen/decode/checksum verification, then manifest commit. A crash before manifest commit
leaves an uncommitted recoverable file; it never authorizes HOT deletion.

## Recommended HOT/COLD architecture

1. Keep a bounded **HOT SQLite** window for live reads and recovery (initial recommendation: six
   hours, configured by both age and disk quota, not a hard-coded wall-clock schedule).
2. Seal arrival-ordered WS facts into hour/ticker-aware immutable chunks. Store canonical payload,
   every snapshot/delta/ACK, sequence, timestamp, provenance, and SHA-256.
3. Keep a small SQLite **manifest** with chunk path, time range, tickers, sequence range, counts,
   codec/wire version, checksum, and verification state.
4. Write a separate compact synchronized-state layer for Feature Store/model scans. Training should
   not scan hundreds of millions of raw deltas.
5. Raw COLD chunks remain the replay truth. Compact model states never replace raw archive facts.

Milestone 7.8B adds a separate append-only manifest and bounded HOT deletion. A range becomes purge
eligible only after archive publication, reopen/decode, file and logical checksum verification,
exact source-vs-archive event comparison, deterministic rolling-book replay, and manifest commit.
Deletion is ID-range bounded and restart-aware; it never fabricates a missing range. The default HOT
retention is six hours and the first three production chunks are shadow verification only.

The manifest state machine is `WRITING -> WRITTEN -> CHECKSUM_VERIFIED -> REPLAY_VERIFIED ->
COMMITTED -> PURGE_ELIGIBLE -> PURGED`; FAILED facts remain diagnostic and cannot authorize purge.
Archives and the manifest live outside the raw database, so later `VACUUM INTO` compaction cannot
remove their proof. Offline compaction additionally compares the complete table inventory and every
table row count before an atomic same-directory swap with an intact rollback copy.

## Model-facing sampling study

The same 100,000-event sample was reconstructed and compared without deleting raw events. Retention
below means selected state transitions divided by raw reconstructed transitions; it is a compact
training-layer measure, not archive loss.

| policy | states retained | top-of-book changes retained | meaningful depth/imbalance retained |
|---|---:|---:|---:|
| 100 ms | 3.95% | 27.79% | 28.26% |
| 250 ms | 1.90% | 18.76% | 19.05% |
| 500 ms | 1.03% | 13.03% | 13.13% |
| 1 s | 0.54% | 7.79% | 7.94% |
| top-of-book change | 4.97% | 100% | 95.59% |
| top-of-book or meaningful depth/imbalance change | 5.24% | 100% | 100% |

For later model work, change-driven sampling is the best initial compact candidate. Fixed 100-ms
sampling loses many transient book transitions despite retaining more clock regularity. Both can be
materialized from the raw archive; neither should replace it.

## Disk safety and fail-closed retention

Configurable defaults are warning at 70% volume usage or 100 GiB free, archive-immediate
at 75%, critical at 85% or 50 GiB free, and fail-safe recorder pause at 90% or 25 GiB free. The
stricter percentage/free-space condition wins. Control Center should expose HOT DB bytes, COLD
archive bytes, rolling GiB/day, compression ratio, free bytes, estimated days remaining, archive
lag, and last verified chunk. Quota pauses must be explicit diagnostics; silently dropping WS facts
is forbidden.

## Operational commands

`live15-ws-retention status`, `archive-once`, and `purge-once` expose bounded maintenance without a
second recorder. `compact-copy` refuses an active managed recorder and only creates a verified new
copy; swap/rollback remains an explicitly orchestrated offline operation. Runtime health exposes HOT
age, archive bytes/ratio, backlog lower bound, last verified chunk, purge counts, disk state, and
estimated remaining days. Raw archive read-through and model-state materialization remain future
work; neither is required to preserve exact replay truth.

For unattended eligibility checks, `live15-archive-maintenance --once [--max-chunks 0..3]
[--max-purge-batches 0..100]` performs one bounded pass and exits. With no eligible production rows it returns
`WAITING_FOR_RETENTION_ELIGIBILITY`, the exact cutoff, oldest unarchived fact, and `next_eligible_at`;
it never sleeps, compacts, or installs a scheduler. Each purge transaction is capped by the configured
20,000-row batch and is authorized only after the archive is reopened and its file checksum, logical
checksum, deterministic replay hash, committed manifest, and exact contiguous ID range are rechecked.
Partial-purge recovery additionally requires the remaining raw IDs to be the exact contiguous suffix;
an unexplained hole fails closed. Recorder and CLI passes share a manifest lease, while a FAILED chunk
blocks all later ranges rather than allowing retention to skip raw truth.
The command reports HOT SQLite used bytes, reusable freelist bytes, physical DB/WAL bytes, COLD bytes,
and observed archive growth. Re-running resumes both archive and partial-purge progress from the manifest.

## Compaction benefit gate

Compaction is intentionally decoupled from archive/purge cadence. `compact-copy` reads only SQLite
`page_size`, `page_count`, and `freelist_count`, and refuses maintenance unless reclaimable space is
both at least 8 GiB and at least 25% of the database (environment overrides:
`LIVE15_WS_COMPACTION_MIN_RECLAIM_BYTES` and
`LIVE15_WS_COMPACTION_MIN_RECLAIM_PERCENT`). The AND policy prevents a fixed threshold from forcing
large rewrites on a much larger database, while also preventing a high percentage of a tiny file
from causing maintenance. Disk-warning policy remains separate and never authorizes deletion of
unverified truth.

A disposable production snapshot measured the first 72,112 verified rows. Exact range deletion
added 11,240 4-KiB freelist pages (46,039,040 bytes). Baseline and post-purge compact copies differed
by 44,548,096 bytes, only 0.213% of the 20,877,422,592-byte compact baseline. This is far below the
gate and does not justify pausing the recorder. The full pre-purge snapshot was 22,972,628,992 bytes;
its 2,095,206,400-byte baseline compaction benefit was pre-existing fragmentation, not a benefit of
the newly eligible archive ranges.

At the observed 63.15-GiB/day WS SQLite rate, a strict six-hour WS HOT component is about 15.8 GiB.
Including today's non-WS tables, near-term compact HOT SQLite is expected around 18–22 GiB; it is not
a permanent whole-database ceiling because Coinbase/Pyth/secondary/REST histories currently have no
retention. COLD WS archive growth is about 3.2 GiB/day at the long-sample 19.86x ratio and about
4.3 GiB/day at the conservative first-production-chunks physical ratio. Feature/dataset stores are
separate and currently negligible relative to raw storage. The previous total-file observation
implies a conservative non-WS/fragmentation upper bound near 23.8 GiB/day, so total post-retention
disk growth may initially be roughly 27–28 GiB/day until a longer steady-state measurement separates
non-WS truth growth from reusable SQLite pages.

## Controlled production purge acceptance

On 2026-08-22, maintenance reauthorized and purged the four exact production ranges `1–23344`,
`23345–47911`, `47912–70244`, and `70245–72112`. Seven transactions (20,000 rows maximum each)
deleted 72,112 rows; the longest transaction was 0.405 seconds. Transaction-local page counters
measured 45,944,832 newly reusable bytes (43.82 MiB). Concurrent WS writes had already reused about
5 MiB when the command returned, and consumed the remaining freelist within the next bounded
observation. Total page demand during that interval was approximately the newly freed 43.82 MiB plus
12.1 MiB of physical growth, directly demonstrating reuse before file extension. The main database
did not shrink: ordinary SQLite DELETE makes pages reusable inside the file and intentionally does not
rewrite the file.

All four compressed archives were reopened after deletion and passed file checksum, logical checksum,
manifest range, and stored deterministic replay-hash verification. The recorder remained synchronized
10/10 with millisecond-scale event-loop lag and no fatal task. The compaction gate remained closed
(0.161% immediately after deletion, versus required 8 GiB **and** 25%), and no VACUUM or compact copy
was attempted.
