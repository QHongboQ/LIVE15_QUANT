# Nomad Recorder health bridge — upstream decision receipt

STATUS = DECISION_ONLY / RESOLUTION_COMPLETE
AUDITED_MAIN_SHA = `327f5693a4872e8a750fcaadbd67fdd3c4df9aee`
RESEARCH_DATE = 2026-08-30

Scope is read-only upstream resolution for the existing file-backed Recorder health truth. No
runtime, service, Nomad, Consul, ACL, credential, or application mutation was made.

## Existing LIVE15 authority and predicate

The authoritative heartbeat is `data/health.json`; process/lifecycle ownership is
`WinSW:LIVE15Recorder` (`deploy/windows/runtime-ownership.json`). `native_recorder.py` derives
current health from source failures, stale sources, stale workers, and storage integrity:
`_aggregate_current_health` returns `degraded` when those issues exist. A transport callback marks
the Kalshi WS unsynchronized and advances its worker, while `_record_kalshi_ws` isolates ordinary
transport errors and retries the SDK session. This path does not terminate the Recorder.

The existing restart-worthy predicate is narrower: a fatal task/error or an explicitly critical
worker exhaustion (for example `PythWorkerUnhealthyError` after bounded recovery) causes Recorder
termination; WinSW then applies its existing bounded restart policy. `degraded` is an observation
state, not a restart command. The tests assert stale WS produces `degraded`, not process exit
(`tests/test_native_recorder.py::test_current_reconnect_or_stale_worker_degrades_health`).

The actual SDK host path is also bounded only by session completion or exception: in
`kalshi_gateway/production_recorder_host.py`, `_run_session` awaits the SDK/provider tasks and
`run` starts a replacement session only after `_run_session` returns or raises. There is no host
watchdog that turns SDK `reconnecting` plus absent authoritative progress into session replacement
or process exit. Thus the supplied live condition (alive, WS `reconnecting`, no synchronized
progress) would not trigger the existing restart-worthy predicate. Mapping that condition to
restart would add a new domain policy, not merely expose an upstream lifecycle primitive.

## Upstream resolution

| Candidate | Finding | Decision |
| --- | --- | --- |
| Nomad native check | Official Nomad provider supports only `http` and `tcp`; no file check and no native `script`. | Not available for `data/health.json`. |
| Nomad + Consul script check | Consul supports Windows script checks and Nomad `check_restart` can consume an unhealthy Consul check. It requires a separately installed/reachable Consul agent, an executable check, protected Consul state, and local-only script-check configuration to avoid remote-execution risk. | Technically supported, but disproportionate and not adopted; Consul is not installed. |
| Retain WinSW temporarily | Reuses the existing health truth and restart authority without adding a control plane or translating `degraded` into an unapproved restart policy. | **Selected.** |

Consul TTL would require an application heartbeat to the Consul agent; the current file writer does
not provide that interface. Consul `os_service` reports Windows service state and cannot express
the Recorder domain semantics in `health.json`. No custom HTTP health server is required or added.

## Required answers

NOMAD_NATIVE_FILE_OR_SCRIPT_CHECK = NO
CONSUL_SCRIPT_CHECK_SUPPORTED = YES
CONSUL_WINDOWS_SUPPORTED = YES
EXISTING_RESTART_WORTHY_HEALTH_PREDICATE = fatal task/error or bounded critical-worker exhaustion causing Recorder exit, then WinSW restart
DEGRADED_MEANS_RESTART = NO
OBSERVED_WS_STALL_WOULD_TRIGGER_EXISTING_PREDICATE = NO
SELECTED_HEALTH_BRIDGE = NONE (retain WinSW temporarily)
CONSUL_ADOPTION_JUSTIFIED = NO
CUSTOM_HTTP_HEALTH_SERVER_REQUIRED = NO
THIN_ADAPTER = NONE selected; no new code
EXPECTED_NEW_LIVE15_CODE = NONE

## Dependency and next-step boundary

The smallest current path is: `data/health.json` writer → existing Recorder health aggregation →
WinSW service state/restart. SDK transport remains responsible for socket reconnect/resubscribe;
LIVE15 remains responsible for synchronization, freshness, fail-closed semantics, and persistence.
Nomad cannot consume this file directly. A future Nomad Recorder migration therefore needs either
an explicitly approved thin, read-only bridge plus a separately approved restart predicate, or a
decision to retain WinSW. This task does not choose a new domain restart policy.

NEXT_TASK = HUMAN_GATE: decide whether to approve a new bounded WS-stall restart predicate and a
thin upstream-supported bridge; otherwise retain WinSW and do not start Nomad Recorder prep.

## Evidence pointers

- `deploy/windows/runtime-ownership.json`
- `docs/runtime_ownership_and_self_healing.md`
- `docs/continuous_recorder.md`
- `src/live15_quant/native_recorder.py` (`_aggregate_current_health`, `health`,
  `_record_kalshi_ws`, `_on_sdk_ws_transport_state`, `PythWorkerUnhealthyError`)
- `src/live15_quant/kalshi_gateway/production_recorder_host.py` (`SdkProductionRecorderHost`,
  `_run_session`, `run`; no stale-progress watchdog)
- `tests/test_native_recorder.py::test_current_reconnect_or_stale_worker_degrades_health`
- `tests/test_production_recorder_host.py` (session replacement and host boundary coverage)
- `docs/project-brain/capabilities/records/recorder/truth.md`
- Official Nomad check and `check_restart` docs:
  <https://developer.hashicorp.com/nomad/docs/job-specification/check>,
  <https://developer.hashicorp.com/nomad/docs/job-specification/check_restart>
- Official Nomad/Consul integration and Windows service docs:
  <https://developer.hashicorp.com/nomad/docs/networking/consul>,
  <https://developer.hashicorp.com/nomad/docs/deploy/production/windows-service>
- Official Consul script/TTL/Windows checks and security configuration:
  <https://developer.hashicorp.com/consul/docs/register/health-check/vm>,
  <https://developer.hashicorp.com/consul/docs/reference/agent/configuration-file/general>,
  <https://developer.hashicorp.com/consul/commands/agent>
- Official Consul source:
  <https://github.com/hashicorp/consul/blob/main/agent/checks/check.go>,
  <https://github.com/hashicorp/consul/blob/main/agent/config/runtime.go>

LIVE_RECORDER_MUTATED = NO
CONSUL_INSTALLED = NO
NOMAD_RECORDER_STARTED = NO
CURRENT_WS_BUG_FIXED = NO
GAP002_EXECUTED = NO
