# GAP002 second Production acceptance evidence

## Verdict

`GAP002 = PASS`

This receipt records the single authorized second Production reconnect
acceptance episode. No retry, rearm, manual control edit, Recorder restart,
Nomad deployment, SQLite write, reconciliation, training, holdout access, or
trading write was performed as part of evidence collection.

The first Production episode remains historical evidence of a failure:

`FIRST_PRODUCTION_GAP002 = FAIL` (preserved separately in PR #117)

The repaired second episode passed the epoch-fence acceptance predicates.

## Immutable baseline

| Field | Value |
| --- | --- |
| Release | `live15-782fef7c5227-5931f8973dbe` |
| Main SHA | `782fef7c52272dcd7d96a98a164ed738955e5cda` |
| Source tree | `5931f8973dbe5bed3f71d2321217aac47a89529e` |
| Nomad allocation | `ebfa5b11-3210-d1bd-6e4a-49eff65cdbe7` |
| Recorder PID before / after | `15072` / `15072` |
| Old connection | `sdk-recorder-41ccc934830f4bdea5737a8b9eea3ecd` |
| Replacement connection | `sdk-recorder-83bd5afe30ed4ca48b9bf99334b269f1` |

## Authoritative control boundary

The one-shot operator action was observed read-only in its terminal state:

| Field | Local time | UTC |
| --- | --- | --- |
| action | `reconnect_kalshi_ws` | |
| action status | `consumed` | |
| requested | `2026-08-30T21:50:55.020997-07:00` | `2026-08-31T04:50:55.020997+00:00` |
| consuming | `2026-08-30T21:50:55.248926-07:00` | `2026-08-31T04:50:55.248926+00:00` |
| consumed | `2026-08-30T21:50:55.259141-07:00` | `2026-08-31T04:50:55.259141+00:00` |

## Durable evidence

Pre-trigger indexed boundaries were:

| Boundary | Value |
| --- | ---: |
| maximum data gap id | `63793` |
| maximum WebSocket event id | `545428088` |
| maximum WebSocket checkpoint id | `47991` |

Following the requested action, ten append-only `kalshi_ws` `reconnect_gap`
OPEN facts were detected at `2026-08-31T04:50:55.249915+00:00` (IDs
`63794`--`63803`). This proves book invalidation and GAP opening; their
`gap_start` values retain each instrument's final pre-invalidation event
time, as expected. The affected authoritative universe was BNB, BTC, DOGE,
ETH, Gold, HYPE, Silver, SOL, WTI Oil, and XRP.

All ten identities `(source, asset, instrument, gap_start)` have a matching
append-only RECOVERED fact (IDs `63804`--`63813`). Recovery `gap_end` values
range from `2026-08-31T04:51:01.022797+00:00` through
`2026-08-31T04:51:01.091529+00:00`; the corresponding facts were detected
from `2026-08-31T04:51:01.119856+00:00` through
`2026-08-31T04:51:01.796516+00`.

The replacement connection's first durable events were event IDs
`545584444`--`545584453`: exactly ten `orderbook_snapshot` events, one for
each affected ticker, received from `2026-08-31T04:51:01.017675+00:00`
through `2026-08-31T04:51:01.028882+00:00`. The final snapshot (XRP,
sequence 10) reached synchronized state. The first replacement-session delta
was event `545584454`, sequence 11, received at
`2026-08-31T04:51:01.031081+00:00` -- after the full fresh snapshot set.
Checkpoint `48002` durably records the new connection's sequence-10 XRP
snapshot at `2026-08-31T04:51:01.028882+00:00`.

The bounded old-session query (`old connection`, event id greater than the
pre-trigger maximum, persisted between action request and the first new
snapshot) found 428 raw `orderbook_delta` events. They span socket receive
times `2026-08-31T04:50:54.793314+00:00`--`04:50:55.265505+00:00` and
persistence times `2026-08-31T04:50:55.040620+00:00`--`04:50:55.269313+00:00`;
44 persisted after terminal consumption. Thus raw historical persistence was
preserved exactly as designed. Nevertheless, zero of the ten recovery facts
has a `gap_end` before the first replacement snapshot. The gap did not close
until replacement-session fresh snapshots arrived.

## Acceptance predicates

| Predicate | Result | Evidence |
| --- | --- | --- |
| BOOK_INVALIDATION_PROVEN | YES | 10 reconnect GAP OPEN facts after the requested action |
| GAP_OPEN_PROVEN | YES | IDs `63794`--`63803` |
| OLD_SESSION_BUFFERED_EVENTS_OBSERVED | YES | 428 old-session deltas; 44 persisted after consumption |
| OLD_SESSION_RAW_PERSISTENCE_PRESERVED | YES | bounded raw-event query above |
| OLD_SESSION_ACTIVE_PROJECTION_FENCED | YES | no GAP recovery before the replacement fresh snapshots despite buffered old deltas |
| OLD_SESSION_DELTA_CLOSED_GAP | NO | zero recovery `gap_end` values before first replacement snapshot |
| DELTA_ONLY_RECOVERY_BLOCKED | YES | first replacement delta follows all ten snapshots |
| FRESH_SNAPSHOT_RECOVERY_PROVEN | YES | ten durable replacement snapshots and checkpoint `48002` |
| NEW_WS_SESSION_PROVEN | YES | replacement connection differs from old connection |
| SYNC_RESUMED | YES | replacement snapshot sequence 10 synchronizes; final state synchronized / count 10 |
| GAP_RECOVERED_DURABLY | YES | 10 OPEN identities match 10 RECOVERED identities |
| ACTIVE_KALSHI_WS_GAPS_AFTER | 0 | effective append-only identity semantics, not raw `recovered=0` count |

## Post-episode bounded health

Nomad reports deployment `75abc8f0` successful. Allocation
`ebfa5b11-3210-d1bd-6e4a-49eff65cdbe7` remains healthy, desired `run`,
client `running`, job version 5, with zero task restarts. The Recorder PID
remained `15072`; health observations advanced from
`2026-08-30T21:57:40.832980-07:00` to
`2026-08-30T21:59:12.913617-07:00`. Final Kalshi WebSocket state was
`synchronized` with synchronized count `10`; `fatal_task` and
`fatal_error_type` were null.

Out-of-GAP002-path health issues: `source_failure:pyth:WTI Oil` and
`stale_source:pyth:WTI Oil`. They did not prevent any GAP002 predicate and
were not changed by this task.
