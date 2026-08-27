# Domain documentation layout

LIVE15 is **single-context**:

- `CONTEXT.md` is the concise shared vocabulary and pointer map.
- `PROJECT_CHARTER.md` holds durable strategy and authority boundaries.
- `docs/adr/` holds accepted architectural decisions.
- Domain documents under `docs/` hold detailed evidence and contracts.

Agents load `AGENTS.md` first and then only the pointer relevant to the task.
They must not read every document as session startup context.
