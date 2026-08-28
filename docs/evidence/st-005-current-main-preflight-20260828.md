# ST-005 current-main 60-minute catch-up proof preflight — 2026-08-28

Classification: `ST_005_PROOF_BLOCKED_PENDING_DEPLOYMENT`.

This is a bounded, read-only preflight. No 60-minute observation window was
started because the running service version and required proof instrumentation
could not be established. No service was restarted, no Production database was
read or mutated, no configuration or retention eligibility changed, and
Production writes remained zero.

## Observation identity

- preflight completed: `2026-08-28T08:02:44.3480788Z`;
- authoritative `origin/main`:
  `dc3b8b82a86696b715b8aa5487547e7ede7dae0a`;
- local repository checkout: `c2ded1d4fc172b184db4e0fb6faf6b5d6d0100e0`
  (17 commits behind `origin/main` at preflight);
- local checkout `native_recorder.py` blob:
  `17bc94ab02a8897520d19bec2eb6688c02b8a119`;
- current-main `native_recorder.py` blob:
  `dfae55cc9982d7fd27aea6439cbb5f96a401444a`;
- services reported running: `LIVE15ControlCenter` PID 5984,
  `LIVE15Recorder` PID 21472, and `LIVE15RuntimeSupervisor` PID 6016;
- service executable paths resolve under `D:\LIVE15_QUANT\.local-tools\winsw`;
  service-owned process start times were not readable through the available
  read-only service query;
- health receipt path: `D:\LIVE15_QUANT\data\health.json`, last written
  `2026-08-28T08:00:52Z`.

The installed/running package SHA was not exposed by the runtime. The root
checkout and the current-main recorder blob differ, so a current-main runtime
identity cannot be inferred from the repository or service paths.

## Missing proof evidence

The active health receipt exposes legacy archive estimates (for example raw WS
growth and archive throughput proxies), but not the merged current-main
comparable proof fields:

- committed raw WS ingress rows, bytes, rate, and observation duration;
- archive/purge effective rows, bytes, rate, and observation duration;
- a bounded comparable catch-up ratio; and
- a runtime provenance field binding those measurements to current main.

Consequently, a new 60-minute interval would still lack the evidence required
to calculate a valid catch-up conclusion. Proxy metrics cannot be substituted
for the current-main proof contract.

## Required next action

A separately human-authorized current-main deployment with a verifiable runtime
SHA and the merged ST-005 instrumentation is required. After that deployment,
run a fresh read-only preflight and, only if it passes, a single continuous
approximately 60-minute proof window. Do not stitch this preflight to a later
observation.
