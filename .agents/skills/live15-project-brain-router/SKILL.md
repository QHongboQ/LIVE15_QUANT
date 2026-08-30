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

For a durable Project Brain change, use the same current-root traversal to identify the owner before
editing. Then apply `docs/agents/change-protocol.md` → **Recursive Project Brain maintenance**; that
section is the single authority for content, structure, ambiguity, and split semantics. This skill
locates the owner; the protocol determines the minimum update surface.
