# LIVE15 agent guide

This is the canonical, compact entry point for coding agents working in LIVE15. Read this
file first, then follow the pointers below. The repository is the durable source of engineering
context; chat history is not.

## Architecture and ownership

```text
external kalshi-sdk==12.0.0
  -> LIVE15 KalshiGateway / immutable adapter
  -> Reliability
  -> authoritative Recorder / RecorderStore
  -> Materializer / Dataset / Paper
  -> Model / Decision / Hard Risk / Execution / Control Center
```

The SDK owns Kalshi transport, authentication, typed subscriptions, SID routing,
reconnect/resubscribe, and generic REST/order primitives. LIVE15 owns the domain boundary,
15-minute universe/window identity, reliability and fail-closed policy, persistence, lifecycle
and settlement semantics, features/datasets/models, Paper, Risk, and UI. The Recorder provider
is SDK-authoritative; the legacy WebSocket is `LEGACY_ROLLBACK_ONLY`.

## Data and model truth

- Only Kalshi finalized settlement with an official `yes`/`no` result is terminal label truth.
- Predictive feeds never manufacture settlement labels.
- Decision inputs obey strict as-of timestamps; missing, stale, unsynchronized, or gapped data
  fails closed. Do not forward-fill, interpolate, or use future rows.
- Dataset v1 final test is frozen. Never tune vNext on it.
- Use chronological/event-grouped validation, not random row splits. Do not add features without
  an ablation. The current v2 baseline remains the baseline until fresh forward Challenger
  evidence exists.
- Research coverage comes from the typed Research Data Authority, never from a Dataset v1/v2
  partition. Keep decision-time feature freshness, development-history recency, and post-spec
  forward OOS freshness separate; a 15-minute horizon is not a two-day history limit. See
  `docs/research_data_authority.md`.

## Safety and high-risk zones

Hard Risk is independent and must not be changed without explicit human approval. Production
writes remain disabled unless the user explicitly authorizes them. Agents must not autonomously
change risk caps, position sizing, execution permissions, settlement labels, or reconciliation.
Treat these as elevated-review zones:

- authoritative Recorder writes, gap/quarantine, and resync;
- settlement labeling and dataset split boundaries;
- model targets/artifacts and training logic;
- Hard Risk, sizing, execution, and Production account writes.

Inspect and propose changes in these areas, but do not silently alter them while fixing another
problem.

## Upstream Reuse First — mandatory engineering policy

LIVE15 is **not** the default place to reimplement mature generic infrastructure, model code,
runtime behavior, or bug fixes that already exist upstream.

Before implementing a non-trivial bug fix or generic subsystem, search in this order:

1. official documentation and release notes;
2. pinned dependency source, tests, examples, and changelog;
3. upstream GitHub Issues, Pull Requests, Discussions, and merged fixes;
4. mature, actively maintained, license-compatible GitHub projects that already implement the
   required behavior;
5. broader authoritative web sources;
6. only then local reproduction and a LIVE15-specific implementation.

Use exact error text, API/function names, OS/runtime/dependency versions, and observed topology in
searches. For known upstream/platform behavior, prefer the canonical upstream solution over a
LIVE15 workaround.

When a suitable mature implementation exists, reuse priority is:

`dependency -> pinned dependency/fork -> vendored upstream module -> narrow attributed port -> local reimplementation`

Do **not** merely study a mature GitHub project and rewrite the same subsystem locally. Prefer the
mature implementation plus a thin LIVE15 adapter. A local reimplementation requires a documented
reason why dependency/fork/vendor/port reuse is unsuitable. Respect licenses and attribution.

Repeated special-case patching is not an acceptable substitute for upstream reuse or refactoring.
If a fix would introduce a third/fourth mode, duplicated transition path, contradictory invariant,
or another nested exception branch, first consolidate around an existing shared/upstream
abstraction. Optimize for fewer code paths and clearer ownership, not for making one failing test
pass.

## Working protocol

### Protected `main` governance

The GitHub `main` branch is protected. Mutating work must use an isolated worktree and an
`agent/<task-id>` branch, followed by Maker validation, independent Checker validation, a
feature-branch push, and a pull request. Do not push directly to `main`, force-push, or bypass
branch protection.

The user has granted standing authority for **ordinary repo-local engineering bug fixes and
maintenance** to be changed, optimized, reviewed, and merged autonomously after the required
Upstream Reuse First review, regression coverage, Maker/Checker validation, and green CI. This
standing authority does **not** apply to the elevated-review zones above, Production trading
writes, holdout access, training/promotion gates, Hard Risk changes, or irreversible policy
changes; those still require the relevant explicit human approval.

1. Read this guide and the relevant project docs.
2. Inspect actual repository/runtime truth before forming a theory.
3. Define ownership, scope, acceptance criteria, and a bounded change budget.
4. For bugs, execute the Upstream Reuse First sequence before inventing a local fix; then build a
   reproducible signal and classify whether the cause is LIVE15, third-party, configuration,
   environment/operator, test, data quality, or upstream platform behavior.
5. For behavior changes, use a failing regression test, then the smallest architecture-consistent
   implementation or upstream reuse/adaptation.
6. Run targeted checks and relevant broader checks proportional to risk.
7. Report evidence, changed files, validation, and remaining uncertainty.
8. Stop on a real blocker or on success; do not widen the task opportunistically.

Use the local adapted skills in `.agents/skills/` for diagnosis, TDD, and alignment. Their
provenance and LIVE15-specific adaptations are recorded in `.agents/skills-manifest.json`.

## Agent skills and project brain

Use `setup-matt-pocock-skills` only to change the configured workflow. The
project uses GitHub issues and a single-context documentation layout; see
`docs/agents/issue-tracker.md` and `docs/agents/domain.md`.

For normal work, load only the relevant skill from `.agents/skills/`, then the
normal shared-brain bootstrap order:

`AGENTS.md` → `PROJECT_CHARTER.md` → `CONTEXT.md` → `CURRENT_STATE.md` →
`PROJECT_PROGRESS.md` → only the relevant ADR, `BUG_REGISTRY.md`, or evidence.

Git Project Brain is the shared external brain for ChatGPT and Codex; chat
history is never durable project memory. Then use the relevant pointer:

- strategy/authority → `PROJECT_CHARTER.md`;
- vocabulary/architecture routing → `CONTEXT.md`;
- durable decision → `docs/adr/README.md`;
- transient workstream orientation → `CURRENT_STATE.md`;
- guarded regression → `BUG_REGISTRY.md`.

The `live15-*` skills preserve project-specific safety adaptations. The
standard names are pinned upstream workflow skills; see
`docs/agents/skills-installation.md`. Do not load every skill, ADR, or domain
document at session start.

## Durable context pointers

- Architecture and SDK boundary: `docs/kalshi-sdk-v12-migration.md`, `docs/kalshi_native_architecture.md`
- Dataset/model contracts: `docs/training_dataset.md`, `docs/model_artifact_lineage.md`, `README.md`
- Runtime/recovery: `docs/continuous_recorder.md`, `README.md`, `scripts/start_runtime_supervisor.cmd`
- Third-party dependency policy: `third_party_manifest.json`
- Debug routing and change protocol: `docs/agents/debug-routing.md`, `docs/agents/change-protocol.md`

Do not create competing project instruction systems or duplicate the full project history in
another Markdown file.

## Durable task closeout

Before closing an important task, decide whether it changed durable project
state. Record only the authority that changed: task status/result/next action
in `PROJECT_PROGRESS.md`; whole-project phase in `CURRENT_STATE.md`; a durable
bug in `BUG_REGISTRY.md`; a strategy or architecture decision in the charter or
ADR; and vocabulary/routing in `CONTEXT.md`. Complex LIVE15 task specifications
normally state Terra/High, goal, authority, prohibitions, acceptance,
validation, and return format. Use the mandatory Upstream Reuse First sequence in the relevant
diagnosis skill before inventing a local fix.
