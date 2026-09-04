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

## Owner Resolution First

Before proposing, designing, planning, or implementing a non-trivial component, manager, service,
framework, abstraction, model layer, data path, controller, registry, dashboard, queue, scheduler,
helper, or authority:

1. classify the responsibility;
2. enter through `AGENTS.md` and the Project Brain root;
3. recursively locate the existing authority;
4. determine whether the capability already exists;
5. determine whether an implementation already owns the responsibility;
6. determine whether an approved plan already covers it; and
7. classify the owner as `DOMAIN_CORE`, `THIN_UPSTREAM_ADAPTER`, or `GENERIC_LOCAL_INFRASTRUCTURE`.

Existing-owner discovery is mandatory because it prevents duplicate ownership and identifies what
would be reused, migrated, or retired. **It is not a retention preference.** An existing local owner
for generic/platform behavior does not get repaired or extended merely because it already exists.
Its continued existence must first survive the Upstream Reuse First comparison.

Create a new owner only when no existing or upstream owner can correctly own the responsibility.
The design decision must answer, in substance:

- `EXISTING_AUTHORITY_FOUND = YES/NO`
- `EXISTING_CAPABILITY_FOUND = YES/NO`
- `EXISTING_IMPLEMENTATION_FOUND = YES/NO`
- `EXISTING_PLAN_FOUND = YES/NO`
- `RESPONSIBILITY_CLASS = DOMAIN_CORE / THIN_UPSTREAM_ADAPTER / GENERIC_LOCAL_INFRASTRUCTURE`
- `GENERIC_LOCAL_OWNER_RETENTION_JUSTIFIED = YES/NO/N/A`
- `WHY_EXISTING_OWNER_CAN_OR_CANNOT_BE_USED =`
- `WHY_NEW_OWNER_IS_REQUIRED =`

This is a mandatory decision boundary for non-trivial additions in both ChatGPT strategy work and
Codex implementation planning, not boilerplate for an obvious one-line edit. One responsibility
has one clear owner. Do not create B when A already owns the same responsibility; equally, do not
keep repairing A when A is generic local machinery that a mature upstream owner can replace.

For `DOMAIN_CORE` or a selected `THIN_UPSTREAM_ADAPTER`, reuse, extend, consolidate, or replace the
current owner as appropriate. For `GENERIC_LOCAL_INFRASTRUCTURE`, **Upstream Reuse First begins
immediately after owner discovery, even when a local implementation already exists.** The local
implementation is a migration source and rollback boundary, not evidence that it should remain.

The required order is:

```text
existing owner discovery and responsibility classification
  -> if domain core: reuse / extend / consolidate the LIVE15 owner
  -> if generic local infrastructure: challenge retention through Upstream Reuse First
  -> official upstream mechanism and sources
  -> mature license-compatible alternatives
  -> pinned configuration plus thin LIVE15 adapter and validation
  -> retain/reimplement generic LIVE15 behavior only with explicit evidence that upstream is unsuitable
```

The standing authority for ordinary repo-local engineering fixes and maintenance permits change and
merge only after the required upstream review, regression coverage, Maker/Checker validation, and
green CI. It never extends to elevated-review zones, Production trading writes, holdout access,
training/promotion gates, Hard Risk changes, or irreversible policy changes; those retain their
explicit human authority.

For behavioral changes, use the least-cost route:

```text
resolve owner/class → challenge generic local ownership upstream → reuse selected owner / replacement
  → bounded reversible execution → observe
  → diagnose only a concrete remaining failure
  → (genuine authoritative defect in retained LIVE15 code) classify → failing regression test
  → minimal implementation → targeted tests → relevant broader checks
```

The reproduction, hypothesis, and regression-test phases are required for a genuine defect in
authoritative retained code, not for generic machinery already approved for retirement. A new local
generic implementation still requires the Upstream Reuse First review before it is written.

Documentation-only, pure metadata, and audit tasks may use validation without artificial tests.
High-risk changes require explicit review before altering authoritative Recorder writes, gap or
settlement semantics, dataset boundaries, model targets, Hard Risk, or Production execution.

## Platform-owned failure gate

After reproduction, explicitly classify the failure owner before editing application code. If the
cause is configuration, environment/operator state, Windows/Linux/macOS platform behavior,
permissions/ACL/ownership, native service management, scheduler/process lifecycle,
deployment/revert, discovery, telemetry/logging, packaging, or behavior already owned by a selected
mature upstream project, the default action is **native/upstream remediation plus LIVE15
validation**, not a new LIVE15 subsystem. The same rule applies when LIVE15 already has a generic
local implementation: existing ownership does not exempt that implementation from upstream
replacement review before further repair or extension.

### Upstream resolution at the decision point

A platform-owned failure defaults to native/upstream remediation plus LIVE15 validation. When an
approved replacement is selected, run that bounded reversible path first and observe it; do not
pre-debug the retiring machinery. If a concrete failure remains, diagnose its owner and consult
only the targeted upstream evidence needed for the exact observed error text, API, or version.
Perform the full Upstream Reuse First search below when
selecting a new owner, deciding whether a generic local owner should be retained, or considering a
new local implementation:

1. official documentation, release notes, migration guides and maintained examples;
2. the selected/pinned project's source, tests, changelog, Issues, Pull Requests and Discussions;
3. another mature, maintained, license-compatible implementation when the selected owner is
   insufficient;
4. broader sources only for missing operational detail;
5. a LIVE15-specific implementation only when the requirement is genuinely project-specific or no
   reusable upstream path exists.

Prefer the official mechanism directly and preserve the reuse order
`dependency -> pinned dependency/fork -> vendored upstream module -> narrow attributed port ->
local reimplementation`. Respect licenses and attribution. “The operator action cannot be
performed in this session” still permits preparing the standard configuration and validation until
the exact human mutation is reached; a pending human mutation alone does not require a new blocker
receipt or speculative preflight.

If a standard upstream path exists, continue with it to the maximum extent allowed by the task and
stop only at the exact human/operator mutation that lacks authorization. For ordinary
generic/platform problems, prefer no local reimplementation. A local solution is justified only
when the requirement is genuinely LIVE15-specific or no reusable upstream path exists.

### Official procedures are task-time external authority

Project Brain stores LIVE15's durable local rules, ownership, safety boundaries, source priority,
and acceptance criteria. It must not normally freeze a step-by-step copy or paraphrase of an
external vendor tutorial and then treat that copy as the implementation authority.

For a new upstream path, a changed version, or an unverified privileged/runtime procedure, the
executing agent must retrieve the **current** official documentation/tutorials and official GitHub
evidence itself, then map only LIVE15-specific inputs and constraints onto it. A previously selected
and validated replacement may use its recorded procedure for the first bounded reversible execution;
refresh sources when the version/behavior changed or a concrete failure needs clarification. A
remembered procedure without either current sources or prior validation remains inadmissible.

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
- a Checker finding is validation feedback against the original task contract. Record or defer an
  out-of-scope prerequisite; it does not expand the task or become a blocker automatically. A
  concrete in-scope failure may be classified as an `environment/operator/installation` blocker
  after the relevant owner check;
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

### Recursive Project Brain maintenance

Before updating durable Project Brain state, start at current `AGENTS.md` and the Project Brain
root, classify the requested intent, and follow one selected index pointer at a time to the
narrowest authoritative leaf. Indexes own routing; leaves own facts. Do not infer an owner from
chat memory, a previous session, or a known filename when current indexes can resolve it. If
ownership is ambiguous, **STOP** and resolve the authority boundary before editing.

For a content-only update whose ownership and route are unchanged, update the owning leaf only.
Do not synchronize ancestors or siblings merely to make copies agree. For a structural change—a
new, renamed, moved, retired, or split child—update the affected child and its direct parent index.
Propagate farther upward only when that higher index's visible routing changes.

When a leaf becomes too broad, split it losslessly into a folder with an INDEX `README.md` and
multiple real child authorities. The parent keeps scope and pointers only; child leaves own the
moved facts. This may repeat to useful depth, without an artificial depth limit or meaningless
single-child folder. One durable fact has one authoritative home; different fact classes about one
component may have different owners, but duplicate authority is not permitted.

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

Route health uses the conservative `ceil(len(UTF-8 bytes) / 4)` estimate: **HEALTHY** is `<=3500`,
**WATCH / CONSIDER RECURSIVE SPLIT** is `3501–4200`, **SPLIT SOON** is `4201–5000`, and **HARD
FAIL** is `>5000`. Five thousand is a split boundary, not a normal target. Keep normal task routes
narrow by splitting downward through indexes and leaves; never delete durable information to meet a
budget.

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
