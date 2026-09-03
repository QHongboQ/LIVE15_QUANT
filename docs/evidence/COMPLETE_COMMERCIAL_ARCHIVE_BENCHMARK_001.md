# Complete commercial archive benchmark 001

**Verdict: HOLD.** Arrow IPC+ZSTD passed every required semantic check, but the measured
full-stream packaging result does not demonstrate a storage or codec-throughput advantage over the
existing JSONL+zlib archive. This benchmark does not authorize S3/MinIO work, Recorder changes,
or archive-format adoption.

## Fixed input and method

- Fixed offline snapshot: `live15.sqlite3`, SHA-256
  `c5cb81ffcf6af78d51ec9ec32f3ec99e5c541dde70afeefc3faf9f85b1c64590`.
- One complete replayable subscription stream: 1,018,676 records, row IDs 232652–1251327,
  connection `sdk-recorder-45e5600463d9451ba28d50a54562b865`, subscription 3.
- Measurements use the existing LIVE15 JSONL+zlib encoder/decoder, Arrow IPC+ZSTD adapter, and
  deterministic order-book replay implementation without format changes or tuning.
- Machine-readable receipt: `COMPLETE_COMMERCIAL_ARCHIVE_BENCHMARK_001.json`.

## Measurements

| sample | format | total bytes | bytes/event | encode events/s | decode events/s | replay events/s |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 100,000 | JSONL+zlib | 2,307,882 | 23.08 | 54,234 | 34,617 | 4,843 |
| 100,000 | Arrow IPC+ZSTD | 4,212,546 | 42.13 | 51,273 | 20,986 | 4,886 |
| 1,018,676 | JSONL+zlib | 23,371,916 | 22.94 | 51,951 | 44,165 | 4,911 |
| 1,018,676 | Arrow IPC+ZSTD | 42,669,274 | 41.89 | 43,432 | 22,731 | 4,969 |

On the full stream, Arrow consumed 82.6% more bytes per event, encoded 16.4% slower, and decoded
48.5% slower. Its replay rate was 1.2% higher, which is not enough to offset the storage and codec
costs for this bounded raw-archive decision.

## Full-stream verification

Both formats passed exact round-trip, Decimal tuple fidelity, UTC microsecond timestamp fidelity,
row ordering, and deterministic replay equivalence. The final replay SHA-256 was
`8d5b466abbf5201ef4cade2fdfff39f6971bb020ed19beeca99db3b1f5539cee` for both formats. A
truncated Arrow IPC file failed closed.

## Decision boundary

Keep JSONL+zlib as the current raw archive package. Arrow IPC+ZSTD remains a semantically valid
prototype, but its adoption is **on hold** pending a separately authorized result that demonstrates
a material operational advantage without weakening the existing archive and retention safety gates.
