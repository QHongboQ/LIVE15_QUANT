# LIVE15 Commercial Storage Bakeoff 001

Status: **HOLD**. The one architecture selected for a later production-readiness review is **unchanged LIVE15 hot SQLite/replay truth with offline Parquet+ZSTD archive artifacts**. This is not a deployment or an S3/MinIO decision.

The fixed, read-only snapshot produced a complete replayable stream of 1,018,676 events (row IDs 232652–1251327), with replay hash `8d5b466abbf5201ef4cade2fdfff39f6971bb020ed19beeca99db3b1f5539cee`. All archive-format runs used that same stream.

## Archive format ranking

| Rank | Format | Bytes | Bytes/event | Encode events/s | Decode events/s | Replay events/s | Full-stream correctness |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | Parquet + ZSTD | 19,876,538 | 19.51 | 41,979 | 23,340 | 5,089 | PASS: exact, Decimal, UTC microseconds, order, deterministic replay |
| 2 | JSONL + zlib | 23,371,916 | 22.94 | 57,886 | 45,155 | 5,041 | PASS: exact, Decimal, UTC microseconds, order, deterministic replay |
| 3 | Arrow IPC + ZSTD | 42,669,274 | 41.89 | 44,800 | 23,572 | 5,072 | PASS: exact, Decimal, UTC microseconds, order, deterministic replay; truncated input fails closed |

Parquet is 15.4% smaller than JSONL+zlib and 53.4% smaller than Arrow IPC+ZSTD. JSONL+zlib remains the fastest encode/decode baseline, but Parquet is the best measured archive-density choice while retaining exact LIVE15 reconstruction.

## Full-storage systems

| Rank | Candidate | Outcome | Measured/observed result |
| --- | --- | --- | --- |
| 1 | InfluxDB 3 Core | DISQUALIFIED | The complete canonical-payload stream was submitted through a detached isolated Windows process. The server plateaued at 2.60 GB working set / 3.20 GB private memory, then made no CPU or durable-file progress; catalog/WAL files totalled 31 bytes and no export/correctness receipt could be produced. No reduced run substituted for the full stream. |
| 2 | QuestDB | HOST_INCOMPATIBLE | The official portable Windows launcher returned `ACCESS DENIED` and requested Administrator. No service, host change, Docker, WSL, or Linux runtime was used. |

The Influx benchmark deliberately retained the canonical `event_to_wire` payload as a base64 string, with `connection_id` and `row_id` identity tags and ordered reconstruction. That is substantial LIVE15-specific glue and did not complete on the required full stream; therefore there is no valid Influx storage, ingest, export, replay, or correctness metric to rank against the archive artifacts.

## Recorder ingest observation

Read-only observation of `D:\LIVE15_QUANT\data\live15.sqlite3` over 62.245 seconds found `kalshi_ws_orderbook_events` unchanged at 5,024,073 rows. The main database file grew 393,216 bytes (about 6,318 bytes/s), but this is not attributable to Kalshi WS events because its count was flat.

Nomad was running, but the existing runtime health state was `degraded`, `kalshi_ws_connection_state=reconnecting`, and `kalshi_ws_synchronized_count=0`. The measured Kalshi WS average and observed peak were both 0 events/s. Consequently the Parquet encode headroom ratios (41,979 / 0) are undefined rather than infinite: the run provides no healthy-recorder capacity margin. The stated >2x / 1.2–2x / <1.2x bands do not apply until a healthy synchronized window is measured.

## Decision

**HOLD.** Parquet+ZSTD is the archive-format adoption candidate, but production archive adoption remains blocked on a read-only measurement during a healthy, synchronized Recorder interval and the normal buffering/backpressure/recovery review. LIVE15 semantics, Recorder, Production database, Nomad configuration, and `D:\LIVE15_QUANT\data` were not modified.
