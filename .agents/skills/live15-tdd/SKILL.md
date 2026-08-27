---
name: tdd
description: Test behavior changes through vertical red-green slices.
---

# LIVE15 TDD

For behavioral fixes or new logic, write one failing regression test at the highest public seam,
implement only enough to pass, then repeat for the next behavior. Tests must describe observable
behavior and use independent expected values. Prefer real domain boundaries over internal mocks.
Skip artificial tests for documentation, metadata, and audit-only tasks. For settlement, gaps,
datasets, models, Risk, and execution, regression tests are strongly preferred before code edits.
