# Parquet Production Acceptance Phase 1 — pre-acceptance stop receipt

Observed: 2026-09-03T18:10:49Z (Recorder heartbeat) / 2026-09-03T11:11:56-07:00
(acceptance observation)

## Base and scope

- Required merged base: `634167d0303d87e6b6a20ef61beeaa347c02571c`.
- Branch base verified: `origin/main` is exactly that commit.
- Production database observed read-only: `D:\LIVE15_QUANT\data\live15.sqlite3`.
- This receipt records a stop before archive selection or any Production archive/manifest write.

## Pre-acceptance runtime evidence

- Nomad read-only API (`127.0.0.1:4646/v1/job/live15-recorder`) reported job
  `live15-recorder`, type `service`, status `running`, modify index `9462`.
- Recorder heartbeat recorded `kalshi_ws_connection_state: synchronized`,
  `kalshi_ws_synchronized_count: 10`, and ten synchronized markets.
- WS and persistence workers were fresh (about 0.03 seconds old); queue depth, dropped events,
  full waits, and high-water mark were all zero.
- The heartbeat reported `ws_archive: {}`. No `ws_archive*` entry was observed under the Production
  data directory during this preflight.

## Stop condition

The authoritative Recorder heartbeat reported `status: degraded` with:

- `source_failure:pyth:WTI Oil`
- `stale_source:pyth:WTI Oil`

The Phase 1 task requires a healthy Recorder before archive state is touched and explicitly directs
the operator to stop if it is unhealthy. Therefore no archive unit was selected, no configured
archive root or manifest was opened for writing, no Parquet artifact was created, and no corruption
copy test was run.

## Production-data assertion

`PRODUCTION HOT ROWS DELETED = 0`

No Production SQLite write, Recorder restart, configuration change, archive deployment, purge, or
VACUUM was performed.

## Required next condition

Re-run the bounded Phase 1 preflight only after the Recorder heartbeat is healthy (the Pyth WTI Oil
failure and stale-source condition are cleared) and independently reconfirm the live Nomad/heartbeat
facts. The next phase must begin again at pre-acceptance health; it must not treat this receipt as
archive authorization.
