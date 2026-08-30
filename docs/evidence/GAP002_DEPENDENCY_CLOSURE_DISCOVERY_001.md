# GAP002 dependency-closure discovery

`STATUS = DISCOVERY_AND_CLOSURE_EVIDENCE`

`GAP002_EXECUTED = NO`

`CLASSIFICATION_EXECUTED = YES`

`GAP002_FROZEN_SET = DECLARED_FOR_PARALLEL_ISOLATION_ONLY`

`PHASE3_RUNTIME_BASELINE = NOT_DECLARED`

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
[`tests/test_recorder_market_data_provider.py`](../../tests/test_recorder_market_data_provider.py),
and [`tests/test_native_recorder.py`](../../tests/test_native_recorder.py). The legacy
`FakeProductionWs` tests additionally preserve domain gap semantics, but are not SDK-native
end-to-end evidence.

## Actual dependency path

```text
Kalshi Production WebSocket
  -> pinned kalshi-sdk==12.0.0 typed transport/subscription
  -> KalshiWebSocketGateway / SdkProductionRecorderHost
  -> SdkRecorderMarketDataProvider + LIVE15 reliability coordinator
  -> RecorderMarketDataConsumer + RecorderStoreDomainWriter
  -> RecorderStore: SDK WS events/checkpoints; KalshiNativeRecorder typed OPEN/RECOVERED data_gaps
  -> KalshiNativeRecorder health / read-only evidence projection
```

| Responsibility | Current owner | Evidence |
| --- | --- | --- |
| WebSocket transport, typed subscriptions, SID routing, reconnect/resubscribe | `kalshi-sdk==12.0.0` | [`requirements.lock`](../../requirements.lock); [`docs/kalshi-sdk-v12-migration.md`](../kalshi-sdk-v12-migration.md); [`kalshi_gateway/websocket.py`](../../src/live15_quant/kalshi_gateway/websocket.py) |
| Gateway adaptation and production session boundary | LIVE15 `KalshiWebSocketGateway` / `SdkProductionRecorderHost` | [`kalshi_gateway/websocket.py`](../../src/live15_quant/kalshi_gateway/websocket.py); [`production_recorder_host.py`](../../src/live15_quant/kalshi_gateway/production_recorder_host.py) |
| Snapshot validity, sequence validity, synchronization, and fail-closed recovery | LIVE15 reliability coordinator/provider | [`reliability.py`](../../src/live15_quant/kalshi_gateway/reliability.py); [`recorder_provider.py`](../../src/live15_quant/kalshi_gateway/recorder_provider.py); [`tests/test_recorder_market_data_provider.py`](../../tests/test_recorder_market_data_provider.py) |
| Durable SDK event persistence and checkpoint admission | LIVE15 `RecorderMarketDataConsumer` / `RecorderStoreDomainWriter` | [`recorder_consumer.py`](../../src/live15_quant/kalshi_gateway/recorder_consumer.py); [`tests/test_recorder_market_data_provider.py`](../../tests/test_recorder_market_data_provider.py) |
| Gap detection/closure, quarantine, Recorder health, and evidence projection | LIVE15 `KalshiNativeRecorder` / `RecorderStore` | [`native_recorder.py`](../../src/live15_quant/native_recorder.py); [`tests/test_native_recorder.py`](../../tests/test_native_recorder.py); [`docs/continuous_recorder.md`](../continuous_recorder.md) |

The legacy provider remains rollback-only; this discovery follows the documented authoritative
SDK-native Recorder route. No lifecycle, service, deployment, or shadow component appears in
the code path above.

## Seven required questions

| Question | Answer | Evidence |
| --- | --- | --- |
| 1. Is the Recorder process itself required for valid GAP002 acceptance? | **YES** | The Recorder is the authoritative consumer/store owner: it selects the SDK host, receives post-commit progress, owns typed gaps/health, and exposes the read-only projection. See [`native_recorder.py`](../../src/live15_quant/native_recorder.py), [`recorder_consumer.py`](../../src/live15_quant/kalshi_gateway/recorder_consumer.py), and [`docs/continuous_recorder.md`](../continuous_recorder.md). |
| 2. Is Recorder service death/restart required? | **NO** | SDK-provider tests cover in-process quarantine, replacement-session snapshots, and recovered synchronization; consumer tests cover durable checkpoint admission. Legacy `FakeProductionWs` tests separately cover the same domain recovery ladder. No current GAP002 authority requires service death. |
| 3. Is the Recorder lifecycle owner therefore part of the GAP002 path? | **NO** | The process is required, but its generic WinSW lifecycle owner is not required to prove the in-process gap/recovery predicates. [`runtime_ownership_and_self_healing.md`](../runtime_ownership_and_self_healing.md) separates `LIVE15Recorder` WinSW ownership from domain WS recovery. |
| 4. Is RuntimeSupervisor part of the GAP002 path? | **NO** | It never starts, stops, or restarts Recorder; [`tests/test_runtime_supervisor.py`](../../tests/test_runtime_supervisor.py) asserts this, and the ownership document limits it to auxiliary workers. |
| 5. Is `kalshi_sdk_ws_shadow` part of the GAP002 path? | **NO** | It is `ON_DEMAND`, writes a separate ignored shadow store, and cannot activate Recorder writes: [`docs/kalshi-sdk-ws-shadow.md`](../kalshi-sdk-ws-shadow.md), [`runtime_ownership_and_self_healing.md`](../runtime_ownership_and_self_healing.md), and [`tests/test_kalshi_sdk_ws_shadow.py`](../../tests/test_kalshi_sdk_ws_shadow.py). |
| 6. Can GAP002 acceptance be proven using in-process disconnect/reconnect/resync without service death? | **YES** | `SdkRecorderMarketDataProvider` tests prove quarantine and replacement-session snapshots in-process; `RecorderMarketDataConsumer` tests prove its durable boundary. A future run still must freeze its exact evidence contract before claiming GAP002 PASS. |
| 7. What concrete code/interfaces/services/data paths form the actual GAP002 dependency path? | **YES — path identified** | The execution-order path and responsibility table above; relevant interfaces are `SdkProductionRecorderHost`, `SdkRecorderMarketDataProvider`, `RecorderMarketDataConsumer`, `RecorderStoreDomainWriter`, `KalshiNativeRecorder`, and `RecorderStore`. The only service on the semantic path is the running Recorder process, not a service-death/restart transition. |

## Classification and closure

| Responsibility | Classification | Basis |
| --- | --- | --- |
| `kalshi-sdk==12.0.0` WebSocket transport, typed subscription, SID routing, reconnect/resubscribe, and SDK replacement-snapshot mechanics | **ALREADY_UPSTREAM** | The pinned SDK owns these generic mechanisms; the documented SDK-native Recorder route consumes them without a local replacement transport. |
| `KalshiWebSocketGateway`, `SdkProductionRecorderHost`, and exact SDK-to-domain adaptation | **LIVE15_KEEP** | Thin but project-specific environment, immutable DTO, exact-ticker/window, and callback/session boundary; not a generic transport reimplementation. |
| Reliability coordinator/provider: sequence validity, complete-snapshot synchronization, fail-closed state, and recovery admission | **LIVE15_KEEP** | These determine which domain books are authoritative; the SDK supplies frames/reconnect, not LIVE15's validity or availability contract. |
| `KalshiNativeRecorder` / `RecorderStore`: typed gaps, quarantine/closure, checkpoints, health, and durable evidence | **LIVE15_KEEP** | Recorder truth and persistence are explicitly outside generic SDK/lifecycle ownership. |
| Recorder WinSW lifecycle ownership | **OUT_OF_GAP002_PATH** | A running Recorder process is required, but service death/restart is not an acceptance predicate. Any lifecycle migration is nevertheless conflicting with the reserved surface below. |
| `LIVE15RuntimeSupervisor` and its WinSW service | **OUT_OF_GAP002_PATH** | It owns auxiliary children and never Recorder; a migration touching shared owner/config contracts is conflicting. |
| `kalshi_sdk_ws_shadow` | **OUT_OF_GAP002_PATH** | On-demand, separate shadow storage, and no Recorder writes; a migration that changes shared SDK/Gateway/settings/owner surfaces is conflicting. |

`RECORDER_NOMAD_MIGRATION_BEFORE_GAP = NOT_REQUIRED`

`RUNTIME_SUPERVISOR_MIGRATION_BEFORE_GAP = NOT_REQUIRED`

`MIGRATE_BEFORE_GAP_SET = NONE`

### GAP002 reserved surface

The following is the compact `GAP002_FROZEN_SET` for B-lane isolation only; it is not the
Phase-3 runtime baseline:

- `requirements.lock` / `pyproject.toml` entry `kalshi-sdk==12.0.0` and the SDK version behavior
  relied on by the Gateway;
- `src/live15_quant/kalshi_gateway/{websocket.py,production_recorder_host.py,recorder_provider.py,reliability.py,recorder_consumer.py}`;
- `src/live15_quant/native_recorder.py`, the `KalshiNativeRecorder` ->
  `SdkProductionRecorderHost` -> `RecorderMarketDataConsumer` -> `RecorderStoreDomainWriter` ->
  `RecorderStore` contract, typed `data_gaps`, SDK WS checkpoints, and synchronized-book availability;
- `src/live15_quant/config.py` fields `enable_kalshi_production_websocket`,
  `kalshi_recorder_provider`, `kalshi_production_api_key_id_path`,
  `kalshi_production_private_key_path`, `recorder_data_path`, and `recorder_health_path`, plus
  `src/live15_quant/kalshi_gateway/client.py` Production credential and sanitized runtime-environment
  contract; `deploy/windows/live15-recorder.xml` passes the external reference paths without secret content;
- the configured Recorder database path and `data/health.json` health projection;
- `LIVE15Recorder` process/service identity and its corresponding Recorder entry in
  `deploy/windows/runtime-ownership.json` where a change could affect the required process.

### Parallel boundary

`PARALLEL_REPLACEMENT_SAFE_SET = NONE` at the component-migration scope. The relevant
out-of-path components are not acceptance dependencies, but the following changes would collide
with the reserved surface and must wait/reconcile:

- Recorder lifecycle migration: **OUT_OF_GAP002_PATH_BUT_CONFLICTING** — changes the required
  Recorder process/service identity or its owner/health contract.
- RuntimeSupervisor migration: **OUT_OF_GAP002_PATH_BUT_CONFLICTING** — expected ownership/package
  work shares `deploy/windows/runtime-ownership.json` and may alter common runtime/config surfaces.
- `kalshi_sdk_ws_shadow` migration: **OUT_OF_GAP002_PATH_BUT_CONFLICTING** — it is independent only
  while it leaves the pinned SDK, Gateway/settings, owner contract, and Recorder-adjacent paths intact.
- Any SDK upgrade or shared Gateway/config change: **OUT_OF_GAP002_PATH_BUT_CONFLICTING** — these are
  themselves reserved critical-path surfaces.

Pure work demonstrably outside every listed file, interface, service identity, health/data path,
and shared dependency may be proposed later as B-lane work, but no such migration is authorized
or enumerated by this closure.

## Phase-3 details

- Future frozen acceptance harness: **PHASE3_FREEZE_DETAIL**.
- Bounded runtime observation period: **PHASE3_FREEZE_DETAIL**.
- Exact durable receipt set: **PHASE3_FREEZE_DETAIL**.
- Future runtime credential-path proof: **PHASE3_FREEZE_DETAIL**; the reference/path contract is
  reserved above, and no secret content was read.

None blocks dependency classification. This task does not execute Phase 2 migration or Phase 3
freeze work.

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
