# LIVE15 agent index

**Role:** TOP-LEVEL INDEX. Git/repo Project Brain is durable; chat history is not. Follow pointers
recursively and read one necessary child at a time—do not scan the repository.

`ALWAYS-ENTRY: AGENTS.md → docs/project-brain/README.md → selected category index → narrower index → authority leaf/evidence.`

## Permanent gates

- High-risk work (Production writes, Hard Risk, training/promotion, holdout, Recorder
  writes/gap/quarantine/resync, settlement labels, deployment/restart) loads its permanent
  authority before action. Safety and human gates never yield to token efficiency.
- Protected `main` work uses an isolated `agent/<task-id>` branch, Maker, independent Checker,
  push, PR, and green CI; never bypass protection or push directly to `main`.
- **Existing Owner First precedes Upstream Reuse First.** For non-trivial design or implementation,
  route through `docs/agents/change-protocol.md`, resolve the current LIVE15 authority/capability/
  implementation/plan, and reuse, extend, consolidate, or replace that owner before creating one.
  If no suitable internal implementation owns generic behavior, follow the protocol's mandatory
  upstream-resolution order before any LIVE15-specific implementation.

## Need → read next

| Need | Read next |
| --- | --- |
| Project Brain route | `docs/project-brain/README.md` |
| permanent strategy / human authority | `PROJECT_CHARTER.md` |
| vocabulary | `CONTEXT.md` |
| current orientation / task ledger | `CURRENT_STATE.md` / `PROJECT_PROGRESS.md` |
| agent procedure, source policy, diagnosis, skills | `docs/agents/README.md` |
| durable task closeout | `docs/project-brain/status/task-closeout.md` |

If the Project Brain cannot be refreshed, say so; do not issue an executable recommendation from
stale context.
