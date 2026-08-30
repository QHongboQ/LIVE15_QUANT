---
name: live15-project-brain-router
description: Route LIVE15 task intent recursively to the minimum authoritative Project Brain leaf. Use before loading task detail.
---

# LIVE15 Project Brain router

Start with `AGENTS.md` and the Project Brain root index. Select the first category from the root
Need table: planning, capability, dependency/topology, execution, status, strategy, vocabulary, or
diagnosis. Execution and status load current orientation when the root route requires it; diagnosis
also follows the task-time upstream-resolution procedure.

After selecting a category, **follow its pointers recursively**: read the selected index, choose one
child, and repeat until the smallest relevant authority leaf is reached. Do not hard-code a tree or
project facts here, and do not load siblings by default. Parent indexes own routes, not current facts.

For work identified as high-risk by `AGENTS.md`, load its permanent authority and relevant constraint
before proceeding. Token efficiency never bypasses these gates.

New subdomains normally update their parent index, not this skill.

## Update routing

For a durable Project Brain update, classify the change, start at the current root, and follow one
selected pointer recursively until reaching the narrowest authoritative owner. Do not infer an
owner from prior chat or a remembered filename; if ownership is ambiguous, STOP and resolve the
authority boundary before editing.

For content-only change, edit the owning leaf only. For a structural child change, edit the child
and its direct parent index; update higher ancestors only when their routing changes. New subdomains
are therefore normally discovered through the parent index, without changing this skill.
