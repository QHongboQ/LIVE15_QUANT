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

## Working protocol

### Protected `main` governance

The GitHub `main` branch is protected. Mutating work must use an isolated worktree and an
`agent/<task-id>` branch, followed by Maker validation, independent Checker validation, a
feature-branch push, and a pull request. Review conversations must be resolved and a human must
approve the change before `main` is updated with an allowed Squash or Rebase merge. Do not push
directly to `main`, force-push, bypass branch protection, or auto-merge. Required status checks are
not currently enforced; GOV-002 may add a canonical required check. Host/unsandboxed Git is an
explicit boundary for approved Git operations only and is not a general shell escape hatch.

1. Read this guide and the relevant project docs.
2. Inspect actual repository/runtime truth before forming a theory.
3. Define ownership, scope, acceptance criteria, and a bounded change budget.
4. For bugs, build a reproducible signal before hypothesising; classify whether the cause is
   LIVE15, third-party, configuration, environment, test, or data quality.
5. For behavior changes, use a failing regression test, then the smallest implementation.
6. Run targeted checks and relevant broader checks proportional to risk.
7. Report evidence, changed files, validation, and remaining uncertainty.
8. Stop on a real blocker or on success; do not widen the task opportunistically.

Use the local adapted skills in `.agents/skills/` for diagnosis, TDD, and alignment. Their
provenance and LIVE15-specific adaptations are recorded in `.agents/skills-manifest.json`.

## Durable context pointers

- Architecture and SDK boundary: `docs/kalshi-sdk-v12-migration.md`, `docs/kalshi_native_architecture.md`
- Dataset/model contracts: `docs/training_dataset.md`, `docs/model_artifact_lineage.md`, `README.md`
- Runtime/recovery: `docs/continuous_recorder.md`, `README.md`, `scripts/start_runtime_supervisor.cmd`
- Third-party dependency policy: `third_party_manifest.json`
- Debug routing and change protocol: `docs/agents/debug-routing.md`, `docs/agents/change-protocol.md`

Do not create competing project instruction systems or duplicate the full project history in
another Markdown file.
