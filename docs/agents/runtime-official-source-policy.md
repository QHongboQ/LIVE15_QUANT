# LIVE15 runtime official-source policy

## Purpose

Git Project Brain stores LIVE15's durable local rules, ownership boundaries, safety gates, source-priority policy, and acceptance criteria. It is **not** a frozen copy of external vendor documentation, tutorials, examples, or GitHub implementation recipes.

External official documentation is a runtime authority to be retrieved by the agent when the task is executed. This avoids turning a current vendor tutorial into stale LIVE15-owned procedure.

## Mandatory task-time retrieval

For every non-trivial problem covered by Upstream Reuse First, the executing ChatGPT/Codex agent must itself retrieve the current sources at task time in the required order:

1. current official documentation, release notes, migration guides, tutorials, and maintained examples;
2. current official upstream GitHub source, tests, examples, changelog, Issues, Pull Requests, and Discussions;
3. other mature GitHub implementations when needed;
4. broader community/web sources only for unresolved gaps;
5. LIVE15-specific invention only as the last-last-last fallback.

Do not treat a previous chat summary, an old copied tutorial, or a prompt-authored reconstruction of official steps as a substitute for this retrieval.

If current official sources cannot be accessed or verified, stop and report that source-retrieval blocker rather than silently executing a remembered or copied procedure.

## What Project Brain may store

Project Brain may store:

- the source-priority rule and requirement to refresh official sources;
- stable LIVE15-owned architecture, domain, risk, deployment-approval, and rollback boundaries;
- selected upstream product/project identity;
- an intentional dependency/version pin when the repository deliberately owns that compatibility decision;
- links/pointers to official source locations when useful for routing;
- evidence of which sources were consulted for a completed task, including URL, version/tag/commit where relevant, and retrieval date.

Project Brain must not normally store a step-by-step copy or paraphrase of an external tutorial as the implementation authority.

An external procedure may be snapshotted only when LIVE15 deliberately pins that external behavior for reproducibility or compatibility. Such a snapshot must be clearly marked as a historical/version-pinned reference, not as a replacement for checking the current official source on a future task.

## Prompt discipline

User-facing Codex tasks should specify:

- the goal;
- LIVE15-owned boundaries and prohibitions;
- the upstream product/project to consult when already selected;
- the mandatory source-search order;
- acceptance, validation, rollback, and human gates.

They should **not** normally embed a copied sequence of vendor tutorial commands or convert the vendor tutorial into a new LIVE15 deployment recipe. The executing agent must read the current official instructions itself, derive the current procedure, and then map that procedure to the local LIVE15 environment.

The preferred task shape is:

```text
refresh Project Brain
-> retrieve current official docs/tutorials
-> retrieve current official GitHub evidence
-> identify the official supported path
-> map only LIVE15-specific inputs/boundaries
-> execute or prepare that path to the current authorization limit
-> validate against official behavior + LIVE15 contracts
```

## Conflict handling

If current official guidance conflicts with an older LIVE15 assumption, prior copied instructions, or an existing local adapter, do not preserve the local behavior automatically. Re-evaluate against the current upstream source and LIVE15's durable domain/safety requirements.

If the conflict changes architecture, Production safety, an irreversible policy, or another elevated-review boundary, stop for the relevant human/architecture decision instead of inventing a hybrid path.

## Evidence receipt

For a non-trivial upstream-driven task, final evidence should identify the actual sources the executing agent retrieved, for example:

```text
OFFICIAL_DOCS_RETRIEVED_AT_TASK_TIME = YES
OFFICIAL_DOC_URLS = <current sources>
OFFICIAL_VERSION_OR_RELEASE = <when applicable>
OFFICIAL_GITHUB_EVIDENCE = <repo/tag/commit/issues/PRs when applicable>
PROMPT_COPIED_VENDOR_PROCEDURE_USED_AS_AUTHORITY = NO
```

This receipt proves that the current upstream source, not a frozen Project Brain copy, drove the implementation.
