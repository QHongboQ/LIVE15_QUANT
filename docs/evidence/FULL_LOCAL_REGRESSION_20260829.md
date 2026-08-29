# Full local regression — 2026-08-29

Classification: `LOCAL_VALIDATION_PASS`.

## Reproducible result

- source revision: `4d088930cc83634faf807188fba386f7a7a34bea`;
- executed from the clean isolated `agent/full-local-regression-001`
  worktree;
- command: `D:\LIVE15_QUANT\.venv\Scripts\python.exe -m pytest -o cache_dir=runtime/tmp/full-pytest-cache -q`;
- completed: `2026-08-29T12:22:18.1895064Z`;
- result: **1,176 passed, 14 skipped, 0 failed in 88.11s**;
- pytest cache was explicitly confined to the isolated worktree's
  `runtime/tmp` subtree.

The 14 skips are existing opt-in external smoke tests. No smoke credentials,
network live-test flags, service lifecycle operation, Production access,
holdout access or trading write was enabled. This suite result is local code
evidence only; it does not prove deployment or runtime verification.
