# Software and module dependencies

Revision: R1
Status: index authority.

## What it is

Routes package/module dependencies separately from data flow and runtime ownership.

## Current truth

Pinned external packages and their LIVE15 adapter boundary are owned by manifests and `docs/kalshi_native_architecture.md`; this leaf does not duplicate package inventories.

## Interfaces / dependencies

`third_party_manifest.json`; `docs/kalshi-sdk-v12-migration.md`; `docs/kalshi_native_architecture.md`.

## Read next

Use `gap002-closure.md` only for a future task-specific closure.

## Update rule

Update only when dependency routing authority changes.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 dependency routing baseline. |
