# GAP002 dependency-closure discovery

`STATUS = DISCOVERY_ONLY`

`GAP002_EXECUTED = NO`

`CLASSIFICATION_EXECUTED = NO`

`FROZEN_SET_DECLARED = NO`

## Audited source

- `origin/main`: `057e67e44e1f305b75466fe826defc3ca10d55db`
- Project Brain route: `AGENTS.md` -> `docs/project-brain/README.md` ->
  `dependencies/README.md` -> `gap002-closure.md`.
- `GAP002_DEPENDENCY_AUDIT_EXECUTED = NO` is confirmed by
  [`gap002-closure.md`](../project-brain/dependencies/gap002-closure.md),
  [`reliability.md`](../project-brain/capabilities/records/reliability.md), and
  [`current-roadmap.md`](../project-brain/plan/current-roadmap.md).

## Future acceptance requirement

The approved roadmap requires a future `WS-RESYNC-001 + GAP-002` run to establish its
proof on a frozen critical path, not this discovery branch. Current Recorder and test contracts
show the minimum behavior that such a proof must demonstrate:

1. an SDK stream discontinuity, sequence defect, or reconnect makes every affected book
   unavailable and opens typed `kalshi_ws` gap facts;
2. deltas cannot revive the books; the complete current ticker set needs fresh authoritative
   snapshots before synchronization and consumption resume;
3. recovery records synchronized health/checkpoints and closes the corresponding persisted gap
   facts without leaving an active `kalshi_ws` gap.

This is not a frozen acceptance script or a GAP002 result. The exact runtime duration, evidence
receipt, and frozen dependency set remain **UNRESOLVED** until Phase 3. Evidence:
[`docs/continuous_recorder.md`](../continuous_recorder.md),
[`docs/reliability/ws-resync-001-protocol-audit.md`](../reliability/ws-resync-001-protocol-audit.md),
and [`tests/test_kalshi_ws_recorder.py`](../../tests/test_kalshi_ws_recorder.py).

## Actual dependency path

```text
Kalshi Production WebSocket
  -> pinned kalshi-sdk==12.0.0 typed transport/subscription
  -> KalshiWebSocketGateway / SdkProductionRecorderHost
  -> SdkRecorderMarketDataProvider + LIVE15 reliability coordinator
  -> KalshiNativeRecorder synchronized-book and gap handling
  -> RecorderStore: WS events, checkpoints, typed OPEN/RECOVERED data_gaps
  -> Recorder health / read-only evidence projection
```

| Responsibility | Current owner | Evidence |
| --- | --- | --- |
| WebSocket transport, typed subscriptions, SID routing, reconnect/resubscribe | `kalshi-sdk==12.0.0` | [`requirements.lock`](../../requirements.lock); [`docs/kalshi-sdk-v12-migration.md`](../kalshi-sdk-v12-migration.md); [`kalshi_gateway/websocket.py`](../../src/live15_quant/kalshi_gateway/websocket.py) |
| Gateway adaptation and production session boundary | LIVE15 `KalshiWebSocketGateway` / `SdkProductionRecorderHost` | [`kalshi_gateway/websocket.py`](../../src/live15_quant/kalshi_gateway/websocket.py); [`production_recorder_host.py`](../../src/live15_quant/kalshi_gateway/production_recorder_host.py) |
| Snapshot validity, sequence validity, synchronization, and fail-closed recovery | LIVE15 reliability coordinator/provider | [`reliability.py`](../../src/live15_quant/kalshi_gateway/reliability.py); [`recorder_provider.py`](../../src/live15_quant/kalshi_gateway/recorder_provider.py); [`tests/test_recorder_market_data_provider.py`](../../tests/test_recorder_market_data_provider.py) |
| Gap detection/closure, quarantine, Recorder health, and persistence evidence | LIVE15 `KalshiNativeRecorder` / `RecorderStore` | [`native_recorder.py`](../../src/live15_quant/native_recorder.py); [`tests/test_kalshi_ws_recorder.py`](../../tests/test_kalshi_ws_recorder.py); [`docs/continuous_recorder.md`](../continuous_recorder.md) |

The legacy provider remains rollback-only; this discovery follows the documented authoritative
SDK-native Recorder route. No lifecycle, service, deployment, or shadow component appears in
the code path above.

## Seven required questions

| Question | Answer | Evidence |
| --- | --- | --- |
| 1. Is the Recorder process itself required for valid GAP002 acceptance? | **YES** | The Recorder is the authoritative consumer/store owner and owns synchronized consumption, typed gaps, checkpoints, and health: [`docs/continuous_recorder.md`](../continuous_recorder.md), [`native_recorder.py`](../../src/live15_quant/native_recorder.py), and `test_recorder_uses_only_synchronized_ws_and_closes_sequence_gap`. |
| 2. Is Recorder service death/restart required? | **NO** | Current semantic recovery coverage is in-process: `test_recorder_uses_only_synchronized_ws_and_closes_sequence_gap`, `test_transport_stall_invalidates_all_books_and_requests_reconnect`, and `test_stalled_dirty_subscription_escalates_snapshot_resubscribe_then_reconnect` in [`tests/test_kalshi_ws_recorder.py`](../../tests/test_kalshi_ws_recorder.py). No current GAP002 authority requires service death. |
| 3. Is the Recorder lifecycle owner therefore part of the GAP002 path? | **NO** | The process is required, but its generic WinSW lifecycle owner is not required to prove the in-process gap/recovery predicates. [`runtime_ownership_and_self_healing.md`](../runtime_ownership_and_self_healing.md) separates `LIVE15Recorder` WinSW ownership from domain WS recovery. |
| 4. Is RuntimeSupervisor part of the GAP002 path? | **NO** | It never starts, stops, or restarts Recorder; [`tests/test_runtime_supervisor.py`](../../tests/test_runtime_supervisor.py) asserts this, and the ownership document limits it to auxiliary workers. |
| 5. Is `kalshi_sdk_ws_shadow` part of the GAP002 path? | **NO** | It is `ON_DEMAND`, writes a separate ignored shadow store, and cannot activate Recorder writes: [`docs/kalshi-sdk-ws-shadow.md`](../kalshi-sdk-ws-shadow.md), [`runtime_ownership_and_self_healing.md`](../runtime_ownership_and_self_healing.md), and [`tests/test_kalshi_sdk_ws_shadow.py`](../../tests/test_kalshi_sdk_ws_shadow.py). |
| 6. Can GAP002 acceptance be proven using in-process disconnect/reconnect/resync without service death? | **YES** | The existing in-process Recorder/provider tests prove quarantine, replacement-session snapshots, resubscribe/reconnect escalation, gap closure, and synchronized recovery. A future run still must freeze its exact evidence contract before claiming GAP002 PASS. |
| 7. What concrete code/interfaces/services/data paths form the actual GAP002 dependency path? | **YES — path identified** | The execution-order path and responsibility table above; relevant interfaces are `SdkProductionRecorderHost`, `SdkRecorderMarketDataProvider`, `KalshiNativeRecorder`, and `RecorderStore`. The only service on the semantic path is the running Recorder process, not a service-death/restart transition. |

## Unresolved questions

- The future frozen acceptance harness, bounded observation period, and exact durable receipt set.
- The exact current runtime configuration/credential-path proof for the future run; no secret content
  was read here.
- Any Phase 2 classification of the listed nodes. This task deliberately makes none.

## Supporting pointers

- Authority: [`gap002-closure.md`](../project-brain/dependencies/gap002-closure.md),
  [`reliability.md`](../project-brain/capabilities/records/reliability.md), and
  [`current-roadmap.md`](../project-brain/plan/current-roadmap.md).
- Pinned/upstream boundary: [`pyproject.toml`](../../pyproject.toml),
  [`requirements.lock`](../../requirements.lock), and
  [`docs/kalshi-sdk-v12-migration.md`](../kalshi-sdk-v12-migration.md).
- Behavioral coverage: [`tests/test_kalshi_ws_recorder.py`](../../tests/test_kalshi_ws_recorder.py),
  [`tests/test_recorder_market_data_provider.py`](../../tests/test_recorder_market_data_provider.py),
  and [`tests/test_runtime_supervisor.py`](../../tests/test_runtime_supervisor.py).
