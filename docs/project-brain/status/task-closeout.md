# Durable task closeout

Revision: R3
Status: status authority.

## What it is

Routes durable task results and copy-ready task requirements without adding them to always entry.

## Current truth

Before closing an important task, decide whether it changed durable project
state. Record only the authority that changed: task status/result/next action
in `PROJECT_PROGRESS.md`; whole-project phase in `CURRENT_STATE.md`; a durable
bug in `BUG_REGISTRY.md`; a strategy or architecture decision in the charter or
ADR; and vocabulary/routing in `CONTEXT.md`. User-facing Codex task specifications must explicitly
state the selected model and reasoning level, chosen dynamically for the task's complexity, risk,
and token cost; use the least expensive adequate setting rather than a fixed default. They should
also state goal, authority, prohibitions, acceptance, validation, and return format. Use Existing
Owner First to locate and classify the current authority/capability/implementation/plan, then apply
Upstream Reuse First. Existing ownership is discovery, not a retention preference: if the
responsibility is generic and the current owner is LIVE15-local, upstream comparison is mandatory
before deciding to repair or extend it. Direct reuse without that comparison is reserved for genuine
LIVE15 domain core or an already selected upstream-backed thin adapter.

## Interfaces / dependencies

`PROJECT_PROGRESS.md`; `CURRENT_STATE.md`; `BUG_REGISTRY.md`; `PROJECT_CHARTER.md`; `CONTEXT.md`.

## Read next

Use the selected durable authority above; use `docs/agents/change-protocol.md` for change execution.

## Update rule

Update only when the durable closeout authority or copy-ready task requirement changes.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-V2-MERGE-GATE-FINAL | Moved closeout detail out of always entry. |
| R2 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 | Aligned closeout with Existing Owner First before upstream resolution. |
| R3 | GENERIC-OWNER-UPSTREAM-RESOLUTION-001 | Clarified that owner discovery prevents duplication but does not justify retaining generic local infrastructure without upstream comparison. |
