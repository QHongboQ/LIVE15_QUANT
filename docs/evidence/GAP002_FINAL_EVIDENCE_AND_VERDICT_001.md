# GAP002 final Production evidence and verdict

`GAP002 = FAIL`

This is an evidence-only receipt. It records the one human-authorized operator action and
read-only Production observations; it changes no Production code, configuration, or data.

## Baseline and control boundary

| Field | Durable value |
| --- | --- |
| Release | `live15-0e93a9338f35-fa9f020f9231` |
| Git SHA | `0e93a9338f35debf832e6d33b521f1aa87e14b9c` |
| Source-tree SHA | `fa9f020f923136d1acf2fdf593dc7cfc86e46cb0` |
| Nomad allocation | `ea35d41b-f46d-63b3-8c28-c75221f7bb5a`, job version 4, healthy |
| Recorder PID | `21368` before and after; Nomad task restart count `0` |
| Action | `reconnect_kalshi_ws` |
| Requested | `2026-08-31T02:19:05.291882+00:00` |
| Consuming | `2026-08-31T02:19:05.864079+00:00` |
| Consumed | `2026-08-31T02:19:05.882304+00:00` |
| Action status | `consumed` |

The reconstructed pre-trigger cursors are: `data_gaps=62235`,
`kalshi_ws_orderbook_events=537289495`, and
`kalshi_ws_book_checkpoints=47089`. The old connection was
`sdk-recorder-b6ce192e303647509d289ad9ed3e2947`.

## Episode facts

The action durably appended two ten-market `kalshi_ws` `reconnect_gap` OPEN rounds under
the current Recorder session `2026-08-31T02:06:02.318144+00:00` and incident
`kalshi-ws:2026-08-31T02:06:02.318144+00:00`:

| Round | OPEN rows | Assets / instruments | RECOVERED rows | Recovery range |
| --- | --- | --- | --- | --- |
| Old-session buffered events | `62236`–`62245` | ETH `KXETH15M`; XRP `KXXRP15M`; BTC `KXBTC15M`; HYPE `KXHYPE15M`; BNB `KXBNB15M`; DOGE `KXDOGE15M`; SOL `KXSOL15M`; Silver `KXSILVER15M`; Gold `KXGOLD15M`; WTI Oil `KXWTI15M` | `62246`–`62255` | `2026-08-31T02:19:05.244888+00:00`–`2026-08-31T02:19:06.188159+00:00` |
| Replacement-session recovery | `62256`–`62265` | ETH `KXETH15M`; XRP `KXXRP15M`; BTC `KXBTC15M`; HYPE `KXHYPE15M`; BNB `KXBNB15M`; DOGE `KXDOGE15M`; SOL `KXSOL15M`; Silver `KXSILVER15M`; Gold `KXGOLD15M`; WTI Oil `KXWTI15M` | `62266`–`62275` | `2026-08-31T02:19:15.373960+00:00`–`2026-08-31T02:19:15.389130+00:00` |

Each RECOVERED fact matches its OPEN fact by `source`, `asset`, `instrument`, and
`gap_start`, with `recovered=1` and an ordered `gap_end`. The effective projection (not the
raw `recovered=0` count) reports `0` active `kalshi_ws` gaps after the episode.

## Replacement session and snapshot gate

The replacement connection was
`sdk-recorder-96670edf9f9c4c4f897025d478330d16`, distinct from the old connection. Its
first durable event was snapshot row `537290398` at
`2026-08-31T02:19:15.373960+00:00`. Rows `537290398`–`537290407` are fresh
`orderbook_snapshot` events, sequences 1–10, covering BNB, BTC, DOGE, ETH, Gold, HYPE,
Silver, SOL, WTI Oil, and XRP. The first nine remained `unsynchronized`; row `537290407`
(XRP) made the full ten-market set `synchronized` at
`2026-08-31T02:19:15.384498+00:00`. Checkpoint `47090` durably recorded that synchronized
boundary.

This proves fresh-snapshot recovery for the second OPEN round. Its final closing fact,
row `62275`, was appended at `2026-08-31T02:19:15.475074+00:00`; this is the episode end.

## Verdict

| Predicate | Result | Evidence |
| --- | --- | --- |
| `CONTROL_ACTION_CONSUMED` | YES | Immutable control record is `consumed`. |
| `BOOK_INVALIDATION_PROVEN` | YES | The consumed action appended both ten-market reconnect OPEN rounds and cleared the effective authority before replacement snapshots. |
| `GAP_OPEN_PROVEN` | YES | 20 append-only OPEN facts, rows `62236`–`62245` and `62256`–`62265`. |
| `DELTA_ONLY_RECOVERY_BLOCKED` | **NO** | The first OPEN round was closed by post-trigger old-connection `orderbook_delta` events (for example rows `537289496` onward) while their persisted `sync_status_after` remained `synchronized`, before any replacement-session snapshot. This contradicts the merged regression contract that a reconnect must remain unavailable until fresh snapshots. |
| `FRESH_SNAPSHOT_RECOVERY_PROVEN` | YES | The replacement session's complete ten-snapshot set made row `537290407` synchronized and closed the second round. |
| `NEW_WS_SESSION_PROVEN` | YES | Old and new durable SDK connection IDs differ. |
| `SYNC_RESUMED` | YES | Replacement snapshot row `537290407` and current health both show synchronized authority for 10 markets. |
| `GAP_RECOVERED_DURABLY` | YES | 20 logical OPEN identities have matching append-only RECOVERED facts. |
| `ACTIVE_KALSHI_WS_GAPS_AFTER` | `0` | Repository effective-gap identity projection. |
| `NOMAD_ALLOCATION_HEALTHY_AFTER` | YES | Allocation `ea35d41b-f46d-63b3-8c28-c75221f7bb5a` is running and healthy. |
| `RECORDER_RESTARTED` | NO | PID remains `21368`; the active Nomad task has `Restarts: 0`. |
| `RECORDER_FATAL_AFTER` | NO | Current health has empty `fatal_task` and `fatal_error_type`. |

`GAP002 = FAIL` because `DELTA_ONLY_RECOVERY_BLOCKED = NO`. A later complete replacement
snapshot set and final synchronized health do not erase the earlier durable old-session
delta closure.

## Post-episode bounded health

The heartbeat advanced from `2026-08-31T02:20:49.576417+00:00` to a later observed health
record while PID `21368` remained present. The final bounded observation reports
`kalshi_ws_connection_state=synchronized`, `kalshi_ws_synchronized_count=10`, and empty
fatal fields. Pyth WTI source-staleness and archive/persistence warnings are recorded as
out-of-GAP002-path conditions and are not used to determine this verdict.

No training, trading writes, holdout access, Recorder restart, additional reconnect, Nomad job
run, or Production mutation occurred during this evidence task.
