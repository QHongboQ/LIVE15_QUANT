# CI-AUTO-001 — Deterministic CI repair foundation

CI-AUTO-001 is a bounded, event-triggered watcher for the `CI` workflow in
`QHongboQ/LIVE15_QUANT`. Normal operation is deterministic and does not call an Agent, an LLM, or
an external model. It may run once (`--once`) or poll no faster than every 60 seconds
(`--watch --interval N`). `--dry-run` records detection and classification without changing a
worktree.

## State and failure flow

The watcher stores restart-safe JSON under `.agents/state/runs/`, which is ignored by Git. A
completed failed or cancelled run is identified by run ID and a SHA-256 fingerprint of its stable
metadata, not log noise. A processed fingerprint is not retried on the next poll.

```text
CI failure -> run/job/step/log observation -> deterministic class
  -> safe autofix -> repo-wide validation -> feature branch -> PR
  -> known blocker / Agent-required / human-required / unknown (stop)
```

The GitHub token is read only from the host-provided `GITHUB_TOKEN` or `GH_TOKEN` environment
variable. It is never written to state, logs, source, a remote URL, or a PR body. Missing auth
returns `CI_AUTOMATION_GITHUB_AUTH_UNAVAILABLE`.

## Allowed deterministic classes

* `RUFF_FORMAT`: format only confirmed failing paths, then run the repository-wide format check.
* `RUFF_SAFE_LINT`: use `ruff check --fix` only for confirmed paths; unsafe fixes are not used.
* `JSON_FORMAT`: classification is supported, but execution requires the project's canonical
  JSON formatter; without one the run stops rather than inventing a serializer.

Credential/external-service failures are `KNOWN_NON_CODE_BLOCKER`. Test/behavioral, concurrency,
unknown, and semantic failures are `AGENT_REQUIRED` or `UNKNOWN`. Paths or logs touching dataset,
labels, settlement, Hard Risk, Production, execution authority, model promotion, or governance
are always `HUMAN_REQUIRED`.

## Repair and governance

An applied repair requires a clean isolated worktree off `main`, a bounded maximum of five changed
files, no protected-boundary path, and these exact repository-wide gates:

```text
ruff check .
ruff format --check .
pytest
git diff --check
```

The runner creates a branch named `agent/ci-auto/<class>-<fingerprint>`, commits only the checked
diff, pushes that feature branch, and opens a PR against protected `main`. It never pushes or
merges `main`, force-pushes, weakens CI, deletes tests, or retries a fingerprint more than twice.
No runtime, Recorder, Paper, Production, Dataset, settlement, model-promotion, or Hard Risk path
is modified by this foundation. Complex failures are serialized in the handoff object for the
future `CI-AUTO-002 — Agent Repair Broker`; that task is not invoked here.
