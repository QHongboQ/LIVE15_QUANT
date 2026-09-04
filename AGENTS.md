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
- **Owner Resolution First; generic owners are challengeable.** For non-trivial design or
  implementation, route through `docs/agents/change-protocol.md` and resolve the current LIVE15
  authority/capability/implementation/plan before changing anything. Existing-owner discovery
  prevents duplicate owners; it does **not** automatically justify retaining or extending a local
  generic/platform owner. For generic infrastructure, process/lifecycle, deployment, queueing,
  transport, telemetry, packaging, or similar commodity behavior, run the mandatory Upstream Reuse
  First comparison before repairing or extending the local owner. Reuse/extend the local owner
  directly only when the responsibility is genuinely LIVE15 domain-specific or already a selected
  upstream-backed thin adapter.

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
