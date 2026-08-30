---
name: live15-project-brain-router
description: Route LIVE15 task intent to the minimum authoritative Project Brain category index. Use for planning, feature, dependency, execution, status, architecture, or diagnosis requests before loading task detail.
---

# LIVE15 Project Brain router

Start with `AGENTS.md` and `docs/project-brain/README.md`, then choose one category index:

- next step / planning → `docs/project-brain/plan/README.md`
- feature explanation → `docs/project-brain/capabilities/README.md`
- dependency / impact / topology → `docs/project-brain/dependencies/README.md`
- execution / command / deployment → `CURRENT_STATE.md`, then `docs/project-brain/constraints/README.md`
- project status → `CURRENT_STATE.md`, then `PROJECT_PROGRESS.md` / `docs/project-brain/status/README.md`
- architecture rationale → `PROJECT_CHARTER.md`, then `docs/adr/README.md`
- bug diagnosis → selected capability/dependency index, `BUG_REGISTRY.md`, relevant constraints, then task-time upstream resolution

For Production writes, Hard Risk, training/promotion, holdout, Recorder writes/gap/quarantine/resync, settlement labels, or deployment/restart, load the relevant permanent authority and constraint before proceeding. Token efficiency never bypasses these gates.

Route by category index; do not encode feature facts here. New capabilities normally update `capabilities/README.md`, not this skill.
