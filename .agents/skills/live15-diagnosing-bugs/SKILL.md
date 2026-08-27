---
name: diagnosing-bugs
description: Disciplined diagnosis for LIVE15 bugs and performance regressions.
---

# Diagnosing LIVE15 bugs

Build a deterministic feedback loop before theorising. Reproduce the exact symptom, minimise
the input, classify ownership (LIVE15, third-party, configuration, environment, test, or data),
rank falsifiable hypotheses, instrument one boundary at a time, then make the smallest fix and
add a regression test. Read `AGENTS.md` and relevant domain docs first. For Kalshi SDK issues,
inspect pinned upstream source/examples/docs before writing replacement transport logic. Preserve
fail-closed semantics and never log secrets.
