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

## WTI health adjudication (read-only)

Classification: `REAL_CURRENT_WTI_UNDERLYING_DATA_PATH_FAILURE`, not
`OBSERVABILITY_STALE`.

- Kalshi owns the current WTI contract-market truth: the active ticker was
  `KXWTI15M-26SEP031415-15`; its lifecycle row was `open` / `active` and the most recent official
  Kalshi quote rows were `fresh` / `official_venue_order_book`.
- The synchronized WebSocket book for that same ticker used connection
  `sdk-recorder-bbc72726f1214e7080273cb969bac2c5`, subscription `3`, and advanced through
  sequence `903611` at `2026-09-03T18:14:22.196584+00:00`. This proves the WTI Kalshi contract path
  is current and synchronized.
- The WTI predictive-underlying owner is the configured exact Pyth feed
  `Commodities.USOILSPOT` (`925ca92ff005ae943c158e3563f59698ce7e75c5a8c8dd43303a0a154887b3e6`).
  Production had no `underlying_observations` rows for `WTI Oil`, while the heartbeat classified
  the WTI underlying market state as `source_unavailable`.
- Pyth is not retired or silently optional for this input: project authority defines it as the
  primary predictive underlying source for WTI, and the Recorder implementation records an exact
  `feed_unavailable` response as `UPSTREAM_UNAVAILABLE` under `pyth:WTI Oil` and schedules only an
  exact-feed re-probe. It deliberately does not substitute another feed.

Thus `source_failure:pyth:WTI Oil` and `stale_source:pyth:WTI Oil` describe a real, isolated
current failure of the WTI predictive-underlying path. They do not make Kalshi market/WS truth
stale, but they do mean the Recorder is not fully healthy under the Phase 1 gate. Archive acceptance
remains stopped pending a separately authorized WTI data-path recovery.

## Refined archive-path health gate

The archive acceptance depends on the Recorder's Kalshi WebSocket persistence path, not on the
WTI predictive-underlying input. The WTI health fact remains truthful and is not suppressed:

- Overall Recorder health: `degraded`.
- Reason: Pyth entitlement for the WTI predictive-underlying feed is unavailable.
- Archive dependency impact: `NONE`.

Fresh read-only preflight evidence for the archive dependency path:

- Nomad job `live15-recorder` remained `running`.
- Kalshi WS was `synchronized` with `10` synchronized markets.
- `kalshi_ws` and `kalshi_ws_persistence` progress ages were about 0.07 seconds.
- Queue depth was `0`; queue capacity, dropped events, and full waits were all `0`.
- The Production WS high-water row ID advanced from `53,715,798` to `53,730,960` in 10.026
  seconds: 15,162 newly persisted events, or about 1,512 events/second.

## Archive-root configuration stop

The Production Nomad job leaves both `LIVE15_WS_ARCHIVE_ROOT` and
`LIVE15_WS_ARCHIVE_MANIFEST_PATH` unset. No existing `ws_archive*` path was present in
`D:\LIVE15_QUANT\data`. The code's development fallback would derive a path below the mutable
data directory, but the Phase 1 contract explicitly requires a configured, repository-approved
Production archive root and prohibits inventing a long-term path.

Accordingly, Phase 1 stops before the archive write. Required follow-up is an explicit Production
configuration of the approved archive root and manifest path. This is a configuration/authority
decision; this task did not make it.

## Production-data assertion

`PRODUCTION HOT ROWS DELETED = 0`

No Production SQLite write, Recorder restart, configuration change, archive deployment, purge, or
VACUUM was performed.

## Required next condition

Re-run the bounded Phase 1 preflight only after the Recorder heartbeat is healthy (the Pyth WTI Oil
failure and stale-source condition are cleared) and independently reconfirm the live Nomad/heartbeat
facts. The next phase must begin again at pre-acceptance health; it must not treat this receipt as
archive authorization.
