# CI-EFFICIENCY-001 baseline and policy

## Measured baseline

GitHub Actions samples from 2026-08-28 used one serial `quality` job for every
push and pull request.  The job installed the complete locked dependency set
before Ruff, imports, and the entire test suite.

| Change sample | Event | Wall time | Locked install | Pytest |
| --- | --- | ---: | ---: | ---: |
| tracker-only PR, run 33192533102 | pull_request | 3m 42s | 45s | 146s |
| release payload PR, run 33191721004 | pull_request | 3m 54s | 41s | 168s |
| release runtime PR, run 33160794986 | pull_request | 3m 50s | 47s | 149s |
| EOL code PR, run 33155849994 | pull_request | 5m 16s | 46s | 243s |
| post-merge main, run 33192673367 | push | 4m 48s | 102s | 153s |

The tracker-only and release PR samples each also had a same-SHA feature-branch
`push` run.  That duplicate consumes a second complete test run without adding
an independent authority boundary.

## Change-aware policy

`live15_quant.ci_plan` is the sole path classifier.  It returns a tier, test
groups, and whether the full suite is mandatory.  Empty, unknown, CI/workflow,
dependency, shared-core, release/runtime, execution/risk, settlement, and
shared-fixture paths fail closed to the full suite.  Main pushes and manual
workflow dispatch also always require the full suite.

Only explicit docs/project-brain paths select `governance`; explicit
Control Center, recorder/WS, data/storage, and research/model paths select
their named test groups.  Multiple recognised groups run independently.  A
stable `CI Gate` checks that static checks and every planned job explicitly
succeeded; skipped or missing planned jobs fail the gate.

## Efficiency controls

- Pull requests validate on `pull_request`; feature pushes no longer duplicate
  CI.  Push validation remains authoritative on protected `main`.
- PR concurrency is keyed by PR number and cancels only superseded PR runs.
  Main runs are never cancelled by this setting.
- Static checks install only the lock-pinned Ruff version.  Code test jobs use
  the existing full `requirements.lock` environment and retain the import
  surface check.
- The full suite remains serial.  No xdist or sharding was introduced because
  this task did not establish repeatable isolation evidence for the repository's
  process, port, and filesystem-sensitive tests.

The measured baseline proves duplicate-run removal and the work avoided by
skipping the 146--243 second full pytest stage for explicit governance changes.
It does not claim an unmeasured post-change wall-clock speedup.
