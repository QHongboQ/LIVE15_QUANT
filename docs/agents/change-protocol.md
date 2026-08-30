# LIVE15 change protocol

## Protected-main publication

The repository uses protected `main`: changes are published through an isolated worktree and an
`agent/<task-id>` feature branch. The required sequence is Maker implementation, tests and other
validation, independent Checker review, feature-branch push, pull request, resolution of review
conversations, human approval, then Squash or Rebase merge. Direct pushes to `main`, force pushes,
ruleset bypasses, and automatic merges are prohibited. Status checks are not yet required by the
ruleset; adding a canonical required check is deferred to GOV-002. Any host/unsandboxed Git use is
limited to an explicitly approved Git boundary and must not become arbitrary host shell access.

Every coding task should make its boundary explicit before editing:

1. Read `AGENTS.md` and relevant architecture/recovery documents.
2. Inspect actual files, configuration, processes, and tests.
3. State ownership, in-scope files, acceptance criteria, and a time/change budget.
4. Preserve unrelated dirty work; never use broad staging or destructive cleanup.
5. Change the smallest safe surface. Avoid opportunistic refactors.
6. Validate with targeted tests and static checks appropriate to the risk.
7. Review the diff and secret/data boundary.
8. Report facts, changed files, checks, blockers, and rollback/next steps.

The standing authority for ordinary repo-local engineering fixes and maintenance permits change and
merge only after the required upstream review, regression coverage, Maker/Checker validation, and
green CI. It never extends to elevated-review zones, Production trading writes, holdout access,
training/promotion gates, Hard Risk changes, or irreversible policy changes; those retain their
explicit human authority.

For behavioral changes, follow:

```text
reproduce → classify owner → upstream/native check → failing regression test → minimal implementation → targeted tests → relevant broader checks
```

Documentation-only, pure metadata, and audit tasks may use validation without artificial tests.
High-risk changes require explicit review before altering authoritative Recorder writes, gap or
settlement semantics, dataset boundaries, model targets, Hard Risk, or Production execution.

## Platform-owned failure gate

After reproduction, explicitly classify the failure owner before editing application code. If the
cause is configuration, environment/operator state, Windows/Linux/macOS platform behavior,
permissions/ACL/ownership, native service management, scheduler/process lifecycle,
deployment/revert, discovery, telemetry/logging, packaging, or behavior already owned by a selected
mature upstream project, the default action is **native/upstream remediation plus LIVE15
validation**, not a new LIVE15 subsystem.

### Upstream resolution must precede blocker or local invention

A platform-owned failure is not allowed to jump directly from classification to `BLOCKED`, and it
is not allowed to trigger a local workaround first. Before a blocker or local implementation is
accepted, complete the relevant upstream-resolution pass in this order:

1. official documentation, release notes, migration guides, tutorials and maintained examples;
2. the selected/pinned project's official GitHub source, tests, examples, changelog, Issues, Pull
   Requests and Discussions;
3. other mature, actively maintained, license-compatible GitHub projects that already solve the
   same problem;
4. broader community/web material when needed for missing operational detail or real-world
   deployment experience;
5. only then consider a LIVE15-specific implementation, and only when the requirement is genuinely
   project-specific or no reusable upstream path exists.

Prefer following the official tutorial/mechanism directly. Do not study upstream and then rewrite
an equivalent subsystem locally. "The operator action cannot be performed in this session" does
not mean "the upstream solution cannot be prepared in this task": continue preparing the standard
configuration, artifact, jobspec, install plan, validation, and bounded operator step until the
specific unauthorized mutation is reached.

Search with exact observed error text, API/function names, OS/runtime/dependency versions, and
topology. When a suitable mature implementation exists, reuse priority is
`dependency -> pinned dependency/fork -> vendored upstream module -> narrow attributed port -> local reimplementation`.
Respect licenses and attribution. Repeated special-case patching is not a substitute for reuse or
consolidation around the shared/upstream abstraction.

Before finalizing `environment/operator/installation`, record:

```text
UPSTREAM_OFFICIAL_DOCS = CHECKED
UPSTREAM_TUTORIALS_EXAMPLES = CHECKED
UPSTREAM_GITHUB_SOURCE_TESTS = CHECKED
UPSTREAM_GITHUB_ISSUES_PRS = CHECKED
MATURE_GITHUB_ALTERNATIVES = CHECKED/NOT_NEEDED
STANDARD_UPSTREAM_PATH_FOUND = YES/NO
UPSTREAM_RESOLUTION_EXHAUSTED = YES/NO
BLOCKER_ALLOWED = YES/NO
```

If `STANDARD_UPSTREAM_PATH_FOUND = YES`, continue with that standard path to the maximum extent
allowed by the current task and stop only at the exact human/operator mutation that lacks
authorization. Local invention is a last-last-last fallback, not a peer option. For ordinary
generic/platform problems, the preferred outcome is **no local reimplementation at all**. A local
solution is justified only when the requirement is genuinely LIVE15-specific or the official,
GitHub and broader upstream search finds no reusable implementation.

### Official procedures are task-time external authority

Project Brain stores LIVE15's durable local rules, ownership, safety boundaries, source priority,
and acceptance criteria. It must not normally freeze a step-by-step copy or paraphrase of an
external vendor tutorial and then treat that copy as the implementation authority.

The executing agent must retrieve the **current** official documentation/tutorials and official
GitHub evidence itself when the task runs, then derive the current supported procedure and map only
LIVE15-specific inputs and constraints onto it. A previous chat summary or a prompt-authored copy of
vendor steps is not a substitute for that retrieval. If current official sources cannot be reached
or verified, stop rather than execute a remembered procedure.

User-facing Codex prompts should therefore state the goal, selected upstream project, source-search
order, LIVE15 boundaries, acceptance criteria, rollback, and human gates; they should not normally
embed the vendor tutorial as a copied deployment recipe. Detailed policy and evidence receipt:
`docs/agents/runtime-official-source-policy.md`.

For such failures:

- a LIVE15 adapter/validator may inspect and fail closed;
- a one-time native installation/admin action may be documented and executed only inside its
  separately authorized host boundary;
- repo code must not become an ACL/owner repair manager, UAC/elevation wrapper, service manager,
  supervisor, restart manager, rollback controller, registry/discovery implementation, or generic
  recovery framework;
- a new Checker finding about another platform prerequisite may become an
  `environment/operator/installation` blocker only after the upstream-resolution gate above is
  complete; it is not a reason to keep extending the adapter;
- if the proposed solution adds a second/third special-case path or materially increases custom
  platform/lifecycle code, stop for architecture review before editing.

The simplicity target is deliberate: one clear owner per responsibility, the fewest code paths,
small configuration/thin adapters, mature defaults, and deletion of redundant local machinery.
Do not add speculative flexibility, duplicated safety controllers, or abstractions with no current
requirement. Tracking: `GOV-PLATFORM-REUSE-001` / GitHub issue #88 and
`UPSTREAM-MIGRATION-SEQUENCE-001` / issue #90.

## Project-brain and model-selection gate

Before issuing a copy-ready LIVE15 Codex task, consult the current Git Project Brain for the
relevant task. Durable project rules should be recovered from Git rather than asking the user to
re-paste them. Then state the selected model and reasoning level explicitly in the prompt.

Use `setup-matt-pocock-skills` only to change the configured workflow. Do not create a competing
project instruction system or copy full project history into another Markdown file, and do not load
every skill, ADR, or domain document at session start; follow the selected pointer instead.

### Lossless Project Brain size discipline

The intent-based Project Brain entry/index route is a decision-routing layer, not the only storage location for
durable context. The estimated 5,000-token compact-context limit is a **split threshold**, not permission
to delete facts or semantically compress away decision-relevant meaning. If new durable state would
breach the limit, move the full detail into a bounded file under the appropriate existing detail/
roadmap/evidence area and keep a clear pointer plus the minimum decision/status summary in the
bootstrap. Preserve provenance, cautions, gates, evidence references, and next-action semantics.
Do not satisfy the budget by shortening a statement in a way that loses information needed for a
future ChatGPT/Codex decision. The index may be concise; the external brain as a whole must remain
lossless for durable project state.

Selection is dynamic and cost-aware. Choose the **model family first** and the **reasoning level
second**. Do not treat Terra as a default and do not infer model strength from task importance
alone. Use the least expensive adequate combination for the actual bounded step, then escalate only
when evidence shows the current combination is insufficient.

### Model-family ladder

| Task shape | Preferred model |
| --- | --- |
| Deterministic formatting/lint, status checks, tiny docs, one- or two-line edits | **Luna** |
| Single-file bug, explicit failing test, narrow configuration or small bounded fix | **Luna** |
| Normal multi-file implementation, bounded bugfix with tests, routine PR work | **Terra** |
| Cross-module debugging, migrations, compatibility or integration work | **Terra** |
| Architecture, long-context synthesis, multi-system causal analysis, security/release audit | **Sol** |
| High-risk irreversible decisions or complex Production/data/safety analysis | **Sol** |

### Reasoning ladder

- **Low:** deterministic operation with explicit expected output and little ambiguity.
- **Medium:** normal engineering judgment, bounded investigation, or several interacting checks.
- **High:** ambiguous root cause, cross-system reasoning, high-risk decisions, or long autonomous work.

Typical combinations:

- formatting/lint or a known one-line fix → `Luna / Low`;
- single-file bug or small config repair → `Luna / Medium` (use High only if diagnosis is genuinely ambiguous);
- normal multi-file feature/bugfix + tests/PR → `Terra / Medium`;
- migration or cross-module compatibility diagnosis → `Terra / High`;
- architecture/security/public-readiness design or broad causal analysis → `Sol / Medium`;
- high-risk irreversible Production/data/safety decision → `Sol / High`.

A workflow may legitimately change combinations between steps. For example, a security audit may
use `Sol / Medium`, then a deterministic `.gitignore` edit may use `Luna / Low`. Do not keep an
expensive model/reasoning level for mechanical follow-up work merely because the parent task was
complex.

If scope expands materially, the original acceptance signal disappears, or the failure is owned by
a platform/upstream boundary outside LIVE15, stop and reassess. Do not keep patching through an
unclassified or externally owned failure.
