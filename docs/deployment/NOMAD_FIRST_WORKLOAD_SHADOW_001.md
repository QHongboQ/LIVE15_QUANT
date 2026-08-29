# NOMAD-FIRST-WORKLOAD-SHADOW-001

**Status:** PLANNED / isolated shadow only.

This task defines the first non-Production workload migration after the verified
Nomad v2.0.5 POC. The selected workload is the read-only `LIVE15ControlCenter`;
the critical Recorder and all Production services remain out of scope.

## Objective

Exercise the upstream Nomad Windows-service model with one isolated Control
Center artifact, while preserving the existing Kalshi data/API truth boundary.
Nomad and Windows SCM own process scheduling, restart, health, deployment and
rollback. LIVE15 contributes only configuration, typed read-only projections,
and evidence validation.

## Hard boundaries

- All files, configuration, data, logs, ports and credentials are confined to a
  separately prepared non-Production staging root under `D:\LIVE15_NOMAD_POC`.
- `D:\LIVE15_QUANT` is protected and must not be read for runtime data,
  modified, restarted, or used as an artifact source.
- No Recorder, settlement, Hard Risk, execution, holdout, training or trading
  path is started or changed.
- No Production credential is copied, rendered, or accessed by the shadow.
- The existing generic POC evidence is reused; the completed soak, rollback and
  service burn-in are not rerun.

## Upstream contract

1. The Nomad agent is installed and owned by Windows SCM using the official
   service model and runs as `LocalService`.
2. The shadow job uses an absolute, integrity-verified artifact path and the
   explicit `provider = "nomad"` service discovery setting.
3. Native Nomad allocation state, service checks, deployment status and native
   update/revert are the lifecycle truth. No LIVE15 supervisor, PID restart
   controller or rollback state machine is introduced.
4. The shadow exposes only read-only Control Center health/market projections;
   a missing, stale, unsynchronised or unscoped input fails closed.

## Acceptance gate

The task is complete only when a Maker and Independent Checker have reviewed the
official Nomad v2.0.5 behavior and the exact diff, then local validation proves:

- the staged artifact hash and ACL are recorded and non-user-writable;
- `nomad job plan`/`run` produce one isolated allocation with a passing native
  check and no Production endpoint or credential reference;
- a Nomad-native task restart and one controlled Windows-service restart recover
  the allocation and preserve the read-only health response;
- bad update input is rejected or natively auto-reverted without a custom
  controller;
- allocation logs, config/data access and a fixed evidence receipt stay within
  the isolated staging root; and
- no change is made to `D:\LIVE15_QUANT` or any trading/risk/holdout path.

The next implementation step is to identify or build a sealed, non-Production
Control Center artifact and jobspec in this task branch. Until that artifact and
its staging root are available, no service installation or workload deployment
is claimed.
