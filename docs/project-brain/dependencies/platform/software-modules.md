# Software and module dependencies

Revision: R2
Status: index authority.

## What it is

Routes package/module dependencies separately from data flow and runtime ownership.

## Current truth

Pinned external packages and their LIVE15 adapter boundary are owned by manifests and
`docs/kalshi_native_architecture.md`; this leaf does not duplicate package inventories.

## Architecture boundary

```text
external kalshi-sdk==12.0.0
  -> LIVE15 KalshiGateway / immutable adapter
  -> Reliability
  -> authoritative Recorder / RecorderStore
  -> Materializer / Dataset / Paper
  -> Model / Decision / Hard Risk / Execution / Control Center
```

The SDK owns Kalshi transport, authentication, typed subscriptions, SID routing,
reconnect/resubscribe, and generic REST/order primitives. LIVE15 owns the domain boundary,
15-minute universe/window identity, reliability and fail-closed policy, persistence, lifecycle
and settlement semantics, features/datasets/models, Paper, Risk, and UI. The Recorder provider
is SDK-authoritative; the legacy WebSocket is `LEGACY_ROLLBACK_ONLY`.

## Interfaces / dependencies

`third_party_manifest.json`; `docs/kalshi-sdk-v12-migration.md`; `docs/kalshi_native_architecture.md`.

## Read next

Use `../gap002-closure.md` only for a future task-specific closure.

## Update rule

Update only when dependency routing authority changes.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 dependency routing baseline. |
| R2 | PROJECT-BRAIN-V2-MERGE-GATE-FINAL | Moved architecture/ownership detail to its narrow authority. |
