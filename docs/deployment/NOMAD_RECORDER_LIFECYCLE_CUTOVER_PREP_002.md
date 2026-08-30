# NOMAD-RECORDER-LIFECYCLE-CUTOVER-PREP-002

STATUS = PREPARED / NO_PRODUCTION_CUTOVER
AUDITED_MAIN_SHA = `034a34c2fd53506db99e7c96b7c2b7d3815fee98`
RECORDER_LIFECYCLE_TO_NOMAD = PROCEED
IDENTITY_GATE = BLOCKED / OPERATOR_ACCESS_PROOF_PENDING
PYTH_CONFIG_RECONCILIATION = PASS
PYTH_ENABLED = true
PYTH_BASE_URL = `https://pyth.dourolabs.app/hermes`
PYTH_API_KEY_PATH_REQUIRED = YES
PYTH_API_KEY_PATH_REFERENCE = `D:\LIVE15_QUANT\.secrets\pyth-api-key.txt`
RECORDER_RUNTIME = CANONICAL_LIVE15_PRODUCTION_RUNTIME
NEW_RECORDER_RUNTIME_REQUIRED = NO
HEALTH_BRIDGE_REQUIRED_FOR_CUTOVER = NO
CHECK_RESTART_USED = NO
CONSUL_USED = NO
LIVE_RECORDER_MUTATED = NO
WIN_SW_RECORDER_STOPPED = NO
NOMAD_RECORDER_STARTED = NO
GAP002_EXECUTED = NO
ARTIFACT_BINDING = PREPARED

This preparation defines one future Nomad service task only. It does not submit
the job, stop WinSW, start a Nomad allocation, read credentials, or access the
Production RecorderStore. The existing WinSW definition remains the sole
owner until a separately authorized cutover and bounded acceptance.

## Candidate jobspec

`deploy/nomad/live15-recorder.nomad.hcl` uses the already installed Nomad
agent and requires every host-specific path and release identity as an explicit
operator variable. The application root must be an immutable, verified
`releases/<release-id>/app`; the working directory and RecorderStore/health/
control/PID paths remain external mutable state. No `artifact` download is used,
so this does not create a second package/provenance mechanism.

The task invokes the existing `live15_quant.cli:recorder_main` entrypoint through
`CANONICAL_LIVE15_PRODUCTION_RUNTIME` in isolated mode. The task inherits the
Windows SCM Nomad agent identity (the verified LocalService agent topology); no
new task-user, wrapper, supervisor, or permission repair path is added.

The identity-gate helper `deploy/nomad/live15-recorder-identity-preflight.nomad.hcl`
reuses the verified ControlCenter batch/raw_exec pattern. It only verifies the
protected runtime, imports `live15_quant` from the reviewed immutable app root,
opens each credential reference without emitting contents, and creates/deletes
uniquely named probes in the required mutable directories. It never imports or
invokes `recorder_main`, opens the RecorderStore, or changes ACLs. It is prepared
but not run because no reviewed immutable Recorder release candidate and exact
operator path set is currently available.

## Identity gate findings

Read-only host facts:

- `LIVE15Recorder` is the current sole owner and runs as `LocalSystem` (`sc.exe
  qc/queryex LIVE15Recorder`). Its existing working directory resolves to
  `D:\LIVE15_QUANT`. The installed WinSW XML resolves its credential
  references to `D:\SDK_ID.txt` and `D:\SDK.txt`; the tracked repository XML
  retains those values as `%LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH%` and
  `%LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH%` placeholders.
- The installed XML enables Pyth and references
  `D:\LIVE15_QUANT\.secrets\pyth-api-key.txt`; the running health receipt
  shows the Pyth worker healthy. The reviewed source accepts the same
  `LIVE15_ENABLE_PYTH_UNDERLYING`, `LIVE15_PYTH_HERMES_BASE_URL`, and
  `LIVE15_PYTH_API_KEY_PATH` settings, and current official Pyth guidance
  requires an API key for the upgraded Hermes endpoint. The candidate therefore
  carries this behavior explicitly; it does not read or log the key contents.
- The Nomad Windows service runs as `NT AUTHORITY\LocalService`
  (`sc.exe qc/query nomad`).
- `D:\LIVE15_QUANT\active-release.json` points to
  `legacy-unproven-08989b3efd7d19f6`; this is application-release provenance,
  not a runtime identity. It remains unsuitable as the future immutable
  Recorder application candidate, while the separately verified canonical
  Python runtime remains valid.
- With the current frozen defaults, Recorder's mutable surface is:
  `data/live15.sqlite3` plus SQLite WAL/SHM siblings; `data/health.json`,
  `data/recorder-control.json`, `data/recorder.pid`; `data/ws_archive/` and
  `data/ws_archive_manifest.sqlite3`; and
  `data/adaptive-retention.sqlite3` / `data/adaptive-retention.json`.

Required LocalService access is therefore:

| Path class | Required access |
| --- | --- |
| protected `recorder_runtime_python`, immutable `recorder_app_root` | Read/execute |
| Kalshi API-key ID and Kalshi private key reference files | Read/open only; contents never logged |
| Pyth API-key reference file | Read/open only; contents never logged |
| Recorder working directory | Read/write/create/delete as required by the entrypoint; must not be the mutable source checkout |
| RecorderStore directory | Read/write/create/delete for SQLite, WAL, and SHM siblings |
| health/control/PID parent directories | Read/write/create/delete |
| archive directory and manifest parent | Read/write/create/delete |
| adaptive-retention state/status parent directories | Read/write/create/delete |

The canonical runtime identity/hash and Recorder production imports were
verified read-only using the installed runtime; no ACL mutation was made. Until
a reviewed immutable Recorder release and exact LocalService path-permission
evidence are supplied, the identity gate remains blocked and the current
`LocalSystem` application root must not be used as the Nomad candidate.

## Final pre-cutover gate status

The protected release root currently contains a previously verified release for
`b1e1894c7666e9763b3994cc8135ad0d7727698e`, not the current protected
`origin/main` SHA `034a34c2fd53506db99e7c96b7c2b7d3815fee98`. The existing
DEP-PKG-001 build/verify procedure was run against the current SHA in a
temporary non-Production root and passed, producing candidate identity
`live15-034a34c2fd53-44e161f9e838` with source tree
`44e161f9e838a11ca0ee89d4908fec952515a550`, requirements-lock SHA
`4521a9151c00797b004cd6aeb12a054dd5759bd211333d012736ced3e635a67e`,
artifact-manifest SHA `a4637a57c6367e74cfddea98d694b44f5cac5592818d225da044f8362dbd1534`,
and release-manifest SHA
`1173401b48b3224ba817963c29777aa20584fa96d742efa905aa242f8110d248`.
The candidate is not yet a protected Production release, so `RELEASE_VERIFY`
remains `PENDING_OPERATOR` and no identity preflight has been run.

The operator must perform the existing DEP-PKG-001 build/verify procedure under
the protected release root, then supply the resulting release identity and
exact path set to the already-prepared identity preflight. No service or
allocation mutation is part of this gate.

## Upstream stanza receipt

| Nomad stanza | Official source | Official mechanism | Why required here |
| --- | --- | --- | --- |
| `datacenters = ["dc1"]` | [Job specification](https://developer.hashicorp.com/nomad/docs/job-specification/job) | Restrict placement to the agent datacenter named by the existing cluster topology. | Keeps this declarative candidate on the already-proven local Nomad agent; no new discovery system is introduced. |
| `type = "service"` | [Job types](https://developer.hashicorp.com/nomad/docs/job-specification/job#job-types) | Declare a long-lived service workload. | Recorder is continuously running and must be owned by one Nomad allocation. |
| `driver = "raw_exec"` | [Raw Fork/Exec driver](https://developer.hashicorp.com/nomad/docs/job-declare/task-driver/raw_exec) | Run an existing absolute host executable; Windows system-service agents can use a lower-privilege service identity. | Recorder is a pre-provisioned Windows executable workload; no container or new runtime is introduced. |
| `config.command`, `args` | [Raw Fork/Exec driver](https://developer.hashicorp.com/nomad/docs/job-declare/task-driver/raw_exec) | Host binaries require an absolute command; arguments are passed directly. | Binds the protected Python runtime to the existing Recorder entrypoint and immutable app source. |
| `config.work_dir` | [Raw Fork/Exec driver](https://developer.hashicorp.com/nomad/docs/job-declare/task-driver/raw_exec) | Working directory must be absolute. | Preserves relative external `data/` behavior while imports come only from the immutable app root. |
| `restart` | [Restart block](https://developer.hashicorp.com/nomad/docs/job-specification/restart) | Restarts failed tasks locally according to attempts, delay, interval and mode. | Transfers generic restart-on-exit ownership from WinSW without changing the Recorder fatal/exit predicate. |
| `reschedule` | [Reschedule block](https://developer.hashicorp.com/nomad/docs/job-specification/reschedule) | `attempts = 0`, `unlimited = false` disables allocation rescheduling. | Prevents an unbounded service reschedule storm after the finite restart budget is exhausted. |
| `update.health_check`, `auto_revert` | [Update block](https://developer.hashicorp.com/nomad/docs/job-specification/update) | Task-state deployment health and native failed-update revert. | Keeps update/revert ownership in Nomad without requiring a Recorder health bridge. |
| `kill_timeout` | [Task block](https://developer.hashicorp.com/nomad/docs/job-specification/task) | Bounded graceful shutdown before force kill. | Preserves the existing 15-second Recorder graceful-stop bound; no custom shutdown manager. |
| Windows shutdown behavior | [Task block](https://developer.hashicorp.com/nomad/docs/job-specification/task) | `raw_exec` uses the documented Windows `CTRL_BREAK_EVENT` default before the kill timeout. | Lets the existing Recorder handle graceful `KeyboardInterrupt` shutdown without a wrapper or custom signal manager. |
| `env` | [Task block](https://developer.hashicorp.com/nomad/docs/job-specification/task) | Declarative environment passed to the task. | Maps existing provider, read-only Kalshi mode, external credential references, and external Recorder paths; secret contents are excluded. |
| `meta` | [Task/job metadata](https://developer.hashicorp.com/nomad/docs/job-specification/meta) | User metadata attached to the job/task. | Carries release and runtime hashes for auditable cutover evidence without changing application behavior. |
| Allocation logging | [Windows job tutorial](https://developer.hashicorp.com/nomad/tutorials/job-specifications/job-spec-java-windows) | Allocation stdout/stderr is inspected with `nomad alloc logs`; no task `logs` stanza is required. | Keeps logging on Nomad's native allocation surface and avoids a second LIVE15 log manager. |

No `service`, `check`, `check_restart`, `consul`, `template`, `artifact`,
`shutdown_delay`, `kill_signal`, or `logs` stanza is included. The Recorder has
no truthful HTTP/TCP listener; `/api/health` belongs to ControlCenter and is
observation-only. Nomad's native allocation stdout/stderr logs remain available
through `nomad alloc logs`, so a second log manager is unnecessary.

## Restart and ownership semantics

Current:

```text
existing Recorder fatal/critical condition
  -> Recorder process exits
  -> WinSW bounded restart policy
```

Prepared target:

```text
same existing Recorder fatal/critical condition
  -> Recorder task exits
  -> Nomad restart policy: 3 attempts / 5m / 15s delay / mode=fail
  -> no reschedule after the restart budget
```

`degraded` and alive-but-stalled WS state remain observation/domain states, not
restart signals. No `check_restart` or new restart predicate is introduced.

## Future cutover operator gate (not executed here)

The future operator must supply a reviewed release identity and perform these
actions only after explicit human authorization:

1. Read-only capture: `sc.exe queryex LIVE15Recorder`; verify the WinSW PID,
   current `data/health.json`, RecorderStore identity, and that
   `nomad job status live15-recorder` is absent/stopped.
2. Validate and plan the jobspec with the exact protected variables:
   `nomad job validate -var-file=<operator-only-vars> deploy/nomad/live15-recorder.nomad.hcl`
   and `nomad job plan -var-file=<operator-only-vars> ...`. Never commit the
   variable file or secret contents.
3. Stop the old owner with native SCM (`Stop-Service LIVE15Recorder`), confirm
   `sc.exe queryex LIVE15Recorder` is `STOPPED`, the old PID is gone, and no
   second Recorder writer exists.
4. Run the reviewed job (`nomad job run -var-file=<operator-only-vars> ...`),
   then verify allocation/task state, process identity, exact release hashes,
   credential-path resolution without reading contents, RecorderStore/health
   paths, graceful shutdown behavior, and current domain health truth.
5. Retain the unchanged WinSW definition as rollback until bounded acceptance
   passes. Do not run both owners concurrently.

Rollback is the reverse single-owner sequence: stop the Nomad job with
`nomad job stop live15-recorder`, confirm allocation/task/process exit and no
Recorder writer remains, then start the retained service with
`Start-Service LIVE15Recorder` and re-verify its PID, release provenance,
RecorderStore, health, and single-owner state. Do not purge the job or delete
WinSW during the acceptance window.

## Validation performed

- Official Nomad documentation, maintained Windows workload tutorial, and
  pinned v2.0.5 source/tests were retrieved at task time; no prompt-copied
  vendor procedure was used as authority.
- Jobspec was reviewed stanza-by-stanza against official mechanisms above.
- No service, allocation, credential, ACL, runtime, Recorder, Nomad, or
  Production state was mutated.
- `nomad job validate` with redacted operator variable values: PASS
  (`Job validation successful`).
- `nomad job plan` with the same redacted values: PASS as a read-only scheduler
  dry-run (`All tasks successfully allocated`; no submission). The non-zero CLI
  status represented a create diff, not a validation or scheduling error.
- No long Nomad POC soak, crash recovery, auto-revert burn-in, service restart,
  or GAP002 episode was repeated.
- Existing canonical runtime compatibility check: PASS; runtime hash matched
  the recorded identity and required Recorder imports, including
  `kalshi-sdk==12.0.0`, loaded successfully.
- Identity preflight: NOT RUN; running against the current `legacy-unproven`
  active application release would not prove the required immutable candidate
  boundary. The canonical runtime compatibility check itself passed.
- `ARTIFACT_BINDING` is `PREPARED`, not `PASS`: the future gate must prove
  actual protected files/manifests == supplied hash variables == allocation
  metadata before runtime acceptance.
