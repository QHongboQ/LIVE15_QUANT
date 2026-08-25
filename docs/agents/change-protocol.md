# LIVE15 change protocol

Every coding task should make its boundary explicit before editing:

1. Read `AGENTS.md` and relevant architecture/recovery documents.
2. Inspect actual files, configuration, processes, and tests.
3. State ownership, in-scope files, acceptance criteria, and a time/change budget.
4. Preserve unrelated dirty work; never use broad staging or destructive cleanup.
5. Change the smallest safe surface. Avoid opportunistic refactors.
6. Validate with targeted tests and static checks appropriate to the risk.
7. Review the diff and secret/data boundary.
8. Report facts, changed files, checks, blockers, and rollback/next steps.

For behavioral changes, follow:

```text
reproduce → failing regression test → minimal implementation → targeted tests → relevant broader checks
```

Documentation-only, pure metadata, and audit tasks may use validation without artificial tests.
High-risk changes require explicit review before altering authoritative Recorder writes, gap or
settlement semantics, dataset boundaries, model targets, Hard Risk, or Production execution.

If scope expands materially or the original acceptance signal disappears, stop and reassess.
Do not keep patching through an unclassified failure.
