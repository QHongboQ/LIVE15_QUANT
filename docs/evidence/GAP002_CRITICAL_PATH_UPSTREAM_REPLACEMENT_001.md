# GAP002 critical-path upstream replacement resolution

`AUDIT = COMPLETE`

`CURRENT_WS_BUG_FIXED = NO`

`LIVE_RECORDER_MUTATED = NO`

`GAP002_EXECUTED = NO`

## Resolution

| Responsibility | Classification | Resolution |
| --- | --- | --- |
| Recorder workload lifecycle: process ownership, restart, health-driven recovery, logs, update/revert, and deployment state | **REPLACE_BEFORE_GAP** | Move `LIVE15Recorder` from WinSW ownership to HashiCorp Nomad under Windows SCM. Nomad owns generic allocation lifecycle, checks, task restart, update, and native revert; LIVE15 retains the existing health truth and external credential-path references. The existing Nomad POC is the selected platform evidence. Required proof: protected artifact identity, LocalSystem/least-privilege service identity, read-only credential references, existing health truth bound to a Nomad-native check, single-owner/rollback proof, and bounded acceptance without a second control plane. Expected reduction: per-workload WinSW wrapper/failure policy and ad-hoc lifecycle ownership freeze, then retirement only after verified rollback. |
| Kalshi transport, authentication, subscription, SID, reconnect/resubscribe | **ALREADY_UPSTREAM** | Keep SDK ownership. Kalshi's official WebSocket guidance requires client lifecycle handling and fresh snapshots plus deltas, but does not provide a documented bounded session-replacement/no-progress facility. LIVE15 must not reimplement transport. |
| SDK version | **NOT_WORTH_REPLACING** | `kalshi-sdk==12.0.0` remains pinned for this path. PyPI lists `kalshi-sdk==13.0.0`, but it is not official Kalshi provenance and no compatible native stall fix was established. An upgrade is a separate compatibility/closure task, not a pre-GAP incident fix. |
| `SdkProductionRecorderHost` / Gateway boundary | **LIVE15_KEEP** | Keep a thin immutable DTO, exact-ticker, session-to-domain, and fail-closed boundary. It must not grow into a transport/retry controller; the observed stall is not repaired here by this resolution. |
| Provider/consumer batching | **NOT_WORTH_REPLACING** | The bounded in-process queue and 128-event/one-second durable writer are narrow coordination around Recorder truth. No measured buffering/backpressure requirement justifies NATS JetStream; do not introduce it. |
| SQLite connection/plumbing | **NOT_WORTH_REPLACING** | No measured generic persistence bottleneck is on this acceptance path. SQLite/RecorderStore stays; analytics engines are not Recorder truth replacements. |
| RecorderStore, sequence/snapshot validity, synchronization, quarantine, typed GAP OPEN/RECOVERED, provenance, settlement | **LIVE15_KEEP** | These are authoritative domain truth and are not candidates for generic replacement. |

## Current upstream evidence

- Nomad supports native Windows SCM installation, file logging, service checks, health-driven task
  restart, deployment health, and `auto_revert`:
  [Windows service](https://developer.hashicorp.com/nomad/docs/deploy/production/windows-service),
  [checks](https://developer.hashicorp.com/nomad/docs/job-specification/check),
  [check restart](https://developer.hashicorp.com/nomad/docs/job-declare/failure/check-restart), and
  [updates](https://developer.hashicorp.com/nomad/docs/job-specification/update).
- Kalshi's official [SDK overview](https://docs.kalshi.com/sdks/overview) says the API specifications
  are the production source of truth. Its [WebSocket quick start](https://docs.kalshi.com/getting_started/quick_start_websockets)
  requires reconnect/backoff and fresh snapshots, but does not document the observed no-progress
  session replacement behavior. The official starter repository is
  [Kalshi/kalshi-starter-code-python](https://github.com/Kalshi/kalshi-starter-code-python).
- The current `kalshi-sdk` release listing shows `13.0.0`, but its provenance is not the official
  Kalshi SDK path and it does not establish a compatible native fix for this incident:
  [PyPI](https://pypi.org/project/kalshi-sdk/13.0.0/).
- Task-time pinned-upstream review covered the package repository's
  [source/releases](https://github.com/TexasCoding/kalshi-python-sdk), its
  [reconnect issue search](https://github.com/TexasCoding/kalshi-python-sdk/issues?q=is%3Aissue%20reconnect),
  and closed [resubscribe recovery issue #254](https://github.com/TexasCoding/kalshi-python-sdk/issues/254).
  They establish that upstream owns WebSocket implementation and has repaired a resubscribe case,
  but do not document a compatible bounded no-progress/reconnecting session replacement. No
  official Kalshi GitHub source, test, issue, PR, or Discussion exposing such a drop-in behavior
  was found; the official starter repository and WebSocket documentation are the available
  first-party sources. Therefore `SDK_NATIVE_STALL_FIX_FOUND = NO` for the frozen pin, rather than
  treating an untested 13.0.0 upgrade as a fix.

## Required migration order

1. `RECORDER_LIFECYCLE_TO_NOMAD`: prepare and prove the upstream-native Recorder workload migration
   with one owner, retained rollback, and no change to domain truth.
2. Freeze the resulting Nomad-owned critical-path artifact/config/health baseline.
3. Re-run the Phase-4A readiness preflight against that baseline.
4. Only then authorize one bounded `WS-RESYNC-001 + GAP-002` episode.

`REPLACE_BEFORE_GAP_SET = {RECORDER_LIFECYCLE_TO_NOMAD}`

`FIRST_MIGRATION_TASK = NOMAD-RECORDER-LIFECYCLE-CUTOVER-PREP-001`

This is a decision receipt only. It authorizes neither a Recorder migration nor a GAP002 run.
