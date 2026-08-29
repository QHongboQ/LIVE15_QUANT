---
name: diagnosing-bugs
description: Disciplined diagnosis for LIVE15 bugs and performance regressions.
---

# Diagnosing LIVE15 bugs

Read `AGENTS.md` and the relevant domain docs first. Preserve fail-closed semantics and never log
secrets.

## Upstream Reuse First — mandatory

For every non-trivial bug, regression, platform incompatibility, or missing generic capability,
**search upstream before inventing a LIVE15 fix**. Use the exact exception/error text, API name,
platform/runtime version, and dependency version where possible.

Search in this order:

1. official documentation and release notes;
2. pinned dependency source, tests, examples, and changelog;
3. upstream GitHub Issues, Pull Requests, Discussions, and merged fixes;
4. mature, actively maintained, license-compatible GitHub projects that already solve the same
   problem;
5. broader authoritative web sources;
6. only then local reproduction and a LIVE15-specific implementation.

If a mature implementation exists, prefer reuse in this order:

`dependency -> pinned dependency/fork -> vendored upstream module -> narrow attributed port -> local reimplementation`

Do **not** merely read a mature project and then rewrite the same subsystem from scratch. Reuse the
mature implementation and place a thin LIVE15 adapter around it whenever practical. Any local
reimplementation must record why dependency/fork/vendor/port reuse was unsuitable.

Respect upstream licenses and attribution. Never copy code whose license does not permit the
intended use.

## Diagnosis and repair

After upstream research, build a deterministic feedback loop: reproduce the exact symptom, minimise
the input, classify ownership (`LIVE15`, third-party, configuration, environment/operator, test,
data, or upstream platform), rank falsifiable hypotheses, and instrument one boundary at a time.

If the cause is an operator/test invocation error, correct the operation; do not weaken product code
to accommodate the mistake.

For a genuine LIVE15 defect, add a failing regression test and make the smallest architecture-
consistent change. Prefer an existing shared primitive or upstream abstraction over another special
case. If a proposed fix would add a third/fourth mode, duplicated transition path, or nested patch
pile, stop patching and refactor/reuse the mature abstraction instead.

Then run targeted checks, the relevant broader suite, Independent Checker, and CI. A fix is not
complete if it only makes the new test green while increasing duplicated logic or contradictory
invariants.
