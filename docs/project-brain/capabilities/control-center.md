# ControlCenter

Revision: R5
Status: recovery required; current host ownership not reconciled.

## What it is

The read-oriented ControlCenter exposes truthful status and health projections.

## Current truth

The ControlCenter Nomad cutover and Web Application Shell are merged historical implementation and
cutover facts, not current host operational verification. The [full-system audit receipt](../../evidence/LIVE15_FULL_SYSTEM_ROOT_CAUSE_AUDIT_001.md) found that the main
`live15-control-center` Nomad job is not proven running, the legacy WinSW service is stopped and its
start path fails, and the desktop launcher still targets the legacy 127.0.0.1:8765 path. Current Web
operational status is therefore **BLOCKED / RECOVERY REQUIRED** until one lifecycle owner, desktop
entry, listener, deployed release identity, and host acceptance result are reconciled.

Do not rewrite historical cutover receipts. A future `CONTROL-CENTER-OWNERSHIP-RECOVERY` task follows
independent Recorder/Pyth recovery verification.

The packaged React terminal is the sole ControlCenter web owner. The handwritten legacy Web shell
is retired and is not a source-level fallback. A rollback after deployed cutover uses the prior
immutable application release.

The terminal's current Kalshi quote authority is the Recorder's synchronized SDK WebSocket book,
materialized as a bounded ten-row projection in the existing Recorder store. Raw WebSocket events
remain the history truth. ControlCenter reads both surfaces locally; Kalshi REST quotes are explicit
recovery/reconciliation fallback only and never average or silently overwrite synchronized books.
The bounded projection is invalidated atomically on desynchronization and is accepted only when the
Recorder heartbeat identifies that exact ticker as synchronized.

## Recorder schema and rollout contract

The current Recorder metadata schema is v11. The v10 to v11 migration adds only the bounded derived
`kalshi_ws_current_books` synchronized-current-book projection; raw immutable WebSocket history is
unchanged.

Merging this change does not migrate Production. Deployment of a v11-compatible Recorder performs
the migration. A ControlCenter-only deployment is insufficient while a pre-v11 Recorder is still
running, because that Recorder cannot create the realtime projection. The deployment plan must
therefore include an explicit Recorder plus ControlCenter rollout.

After a database has migrated to schema v11, Recorder rollback must use a v11-compatible rollback
release or an explicitly authorized pre-migration database restore. The pre-v11 Recorder binary is
not a safe automatic rollback target against a schema-v11 database.

`/ws/terminal` is a localhost/origin-constrained, read-only subscription boundary for `overview`,
`markets`, and exact `market:<asset>` channels. Clients take an HTTP snapshot before subscribing,
reject non-increasing connection-local sequences, and reconcile from HTTP after reconnect or tab
visibility recovery. Hidden tabs unsubscribe and inactive Portfolio, Research, and Admin tabs do
not issue their heavier requests.

Current-contract market history is bounded to the active ticker/window. Underlying history comes
from existing Coinbase/Pyth observations with per-second extrema; probability history replays the
existing synchronized Kalshi WebSocket order-book facts under a hard event bound. Account equity
history is forward-collected append-only from successful read-only account summary observations at
one-minute active / fifteen-minute idle cadence, with an immediate observation when a sampled
authoritative value changes; it is never synthesized or backfilled. Probability downsampling keeps
per-bucket extrema. Exact WTI Pyth unavailability remains a truthful feed-local degradation with
reprobe and no silent substitute.

## Interfaces / dependencies

`docs/runtime_ownership_and_self_healing.md`; `docs/deployment/NOMAD_CONTROL_CENTER_CUTOVER_FINAL_001.md`; `dependencies/platform/runtime-ownership.md`.

## Read next

For current status use `status/README.md`; for retirement constraints use `constraints/execution/runtime-upstream-boundary.md`.

## Update rule

Update only after an approved ControlCenter ownership, retirement, or health-contract change.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 authority baseline. |
| R2 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 | Removed the stale implication that Recorder remained outside Nomad ownership. |
| R3 | WEB-CUTOVER-CLEANUP-AND-RETIRED-SURFACE-001 | Retired the legacy handwritten Web shell and recorded immutable-release rollback. |
| R4 | WEB-REALTIME-HISTORY-PROJECTION-001 | Made synchronized Recorder WS books primary, added local subscriptions and bounded current-contract/forward account history, and recorded visibility-aware query lifecycle. |
| R5 | PROJECT-RECOVERY-FREEZE-001 | Downgraded current operational verification after the host audit while preserving historical Nomad cutover and Web-shell implementation evidence. |
