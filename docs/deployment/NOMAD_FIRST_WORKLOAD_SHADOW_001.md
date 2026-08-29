# NOMAD-FIRST-WORKLOAD-SHADOW-001

**Status:** LOCAL_ACCEPTANCE_COMPLETE / REVIEW_PENDING / isolated shadow only.

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

- Shadow-owned artifact, configuration, evidence and runtime-log files are
  confined to `D:\LIVE15_NOMAD_POC\control-center-shadow`. Nomad-owned
  allocation stdout/stderr remains in the existing agent's
  `D:\LIVE15_NOMAD_POC\generic-poc\agent-data\alloc` root; both are inside
  the single isolated `D:\LIVE15_NOMAD_POC` POC envelope. The job does not
  create an alternate allocation-log manager.
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

- the staged artifact hash, trusted non-user owner and ACL are recorded and
  non-user-writable;
- `nomad job plan`/`run` produce one isolated allocation with a passing native
  check and no Production endpoint or credential reference;
- the verified generic POC evidence covers native task recovery, Windows-service
  recovery, allocation rediscovery and native auto-revert; those long platform
  checks are deliberately not replayed for this shadow;
- allocation logs, config/data access and a fixed evidence receipt stay within
  the isolated staging root; and
- no change is made to `D:\LIVE15_QUANT` or any trading/risk/holdout path.

## Implemented non-Production artifact

The existing Control Center Windows-service package is intentionally not reused:
it receives production credential paths and exposes bounded Recorder-control
routes. The replacement artifact is therefore a separately sealed, stdlib-only
PowerShell listener at
`deploy/nomad/control-center-shadow/live15-control-center-shadow.ps1`.

- It binds only the Nomad job's `127.0.0.1:18081` port and verifies its own
  SHA-256 before listening.
- `/_nomad/healthz` is process liveness only and returns `production=false` and
  `read_only=true`.
- `/api/health` and `/api/markets` deliberately return `503` with
  `fail_closed=true` until a separately authorized non-Production projection
  source exists. All non-GET methods return `405`.
- It has no Kalshi, Recorder, execution, credential, storage, or external
  network dependency. It does not start or control another component.

`tools/stage_nomad_control_center_shadow.ps1` is the thin staging adapter. It
rejects targets outside `D:\LIVE15_NOMAD_POC`, verifies the jobspec-pinned
artifact hash, accepts only the checkout containing the staging script as its
source, and writes a post-seal no-secret staging receipt. The receipt records
post-copy hashes, owner and recursive ACL read-back for artifact,
configuration, and logs. It requires every root and child to be owned by
BUILTIN\Administrators, with no child-specific ACL override. The adapter
applies a read/execute ACL to artifact/configuration plus a
LocalService-write-only log ACL. It is not a service manager, supervisor,
registry, restart manager, or rollback controller.

The minimal job is
`deploy/nomad/control-center-shadow/live15-control-center-shadow.nomad.hcl`.
It reuses the verified `raw_exec`, loopback host network, `provider = "nomad"`,
native HTTP check, restart policy, and health-gated native update/auto-revert
mechanisms. Nomad and SCM remain the lifecycle owners.

## Historical receipt and current acceptance — 2026-08-29

- Historical staging receipt (artifact-identity provenance only; not current
  owner/DACL acceptance evidence):
  `D:\LIVE15_NOMAD_POC\control-center-shadow\evidence\staging-receipt.json`.
  It records post-copy artifact/jobspec hashes and ACL read-back alongside
  `production=false`, `credentials_present=false`, `recorder_started=false`,
  and `execution_enabled=false`.
- Artifact SHA-256:
  `4D06F9641BA468D4C351190AB5F4E8D1D5F5BEB1463FFF85985190F46662127B`.
  Its artifact and configuration directories are read/execute only for Users
  and LocalService; only the isolated log directory grants LocalService modify.
  However, its root owners are the interactive user, so that receipt is not
  trusted for acceptance: an owner can modify a DACL.
- `nomad job validate` passed. `nomad job plan` allocated one task, then the
  checked `nomad job run -check-index 0` created allocation
  `63252e8b-948e-d73f-e67e-7e35d9f36342`.
- Deployment `c2928a5f` completed successfully. The native
  `nomad-liveness` check reported `success`/HTTP 200, and the direct loopback
  liveness response recorded `production=false` and `read_only=true`.
- The artifact's independent end-to-end test passed: liveness is 200, both
  data projections are fail-closed 503, and a Recorder-control URL is 405.
- A bounded negative ACL regression created a temporary POC-only root with an
  extra `Everyone:(M)` ACE. The stager rejected its unexpected access rule;
  the verified temporary root was then removed.
- The allocation's stdout/stderr are Nomad agent-owned files under
  `D:\LIVE15_NOMAD_POC\generic-poc\agent-data\alloc\63252e8b-948e-d73f-e67e-7e35d9f36342`;
  shadow-owned logs/config/artifact remain in the separate child root stated
  above. This is an explicit POC-envelope boundary, not a custom log path.

### Historical task-side owner gate (superseded)

Microsoft's documented icacls /setowner owner /T mechanism is required for
applying the trusted owner recursively. A bounded POC-only test on 2026-08-29
attempted to set a newly created directory owner to BUILTIN\Administrators
from the current non-elevated session. It failed with Access is denied (exit
code 5); the exact temporary test root was removed.

After read-only checks confirmed the fixed staging tree had no reparse point,
external link, Production/credential reference, or non-trusted writable
executable ACE, Nomad natively stopped
live15-control-center-shadow allocation 63252e8b-948e-d73f-e67e-7e35d9f36342.
The one authorized UAC command was then run exactly once:

    icacls.exe "D:\LIVE15_NOMAD_POC\control-center-shadow" /setowner "BUILTIN\Administrators" /T /C

It returned Access is denied for all 10 target entries and processed zero
files. The artifact SHA-256 remained
4D06F9641BA468D4C351190AB5F4E8D1D5F5BEB1463FFF85985190F46662127B,
the jobspec SHA-256 remained
C789ACB201349AD3FB85E41A830F1EE6BEB6A1976F3939A6C6E044C771B1A062,
every recorded owner remained DESKTOP-IG1RJUJ\1, and the Nomad job remained
dead (stopped). No second UAC request, DACL modification, alternate
owner-changing mechanism, allocation restore, or long POC validation was
attempted.

A later explicitly authorized single-operation invocation was stopped before
the native ACL operation began: the host PowerShell command parser split the
fixed Windows command at the unescaped inheritance parentheses and reported
that OI was not a command. The immediate read-only recheck confirmed every
owner, the top-level DACL, and both hashes were unchanged. It was not retried;
this task must not request another UAC operation.

Those failed task-side attempts are historical operator-gate evidence only.
They do not provide acceptance and do not authorize a repair subsystem. The
environment administrator subsequently completed the approved native owner and
top-level DACL initialization outside this task.

### Current read-only acceptance receipt

The current receipt is the output of the read-only stager validation plus the
bounded Nomad allocation observation, recorded here because the sealed POC
evidence directory is intentionally non-writable to the task user:

- all ten target objects, including roots and descendants, are owned by
  BUILTIN\Administrators;
- root, artifact, config and evidence each have the protected four-ACE
  Administrators/SYSTEM full-control plus LocalService/Users RX policy;
  logs has the same policy except LocalService Modify;
- no Everyone, Authenticated Users, or named-user write ACE exists, and no
  reparse point, external link, Production reference or credential reference
  was found in the fixed POC tree;
- artifact SHA-256 is
  4D06F9641BA468D4C351190AB5F4E8D1D5F5BEB1463FFF85985190F46662127B and
  jobspec SHA-256 is
  C789ACB201349AD3FB85E41A830F1EE6BEB6A1976F3939A6C6E044C771B1A062;
- the stager returned VALIDATED_STAGED in read_only_validation mode without
  creating, copying, changing owner, granting DACLs or writing a receipt.

Nomad plan safely reported the stopped job would change only Stop from true to
false, and the checked run created allocation
2eb3bf4a-e47b-62be-2ca7-675e3b07eb9e under deployment
a27379da-5bb6-c479-acb4-c2c7bba88534. Native allocation status reported
running, deployment health healthy, nomad-liveness success, fixed
127.0.0.1:18081 mapping, and zero task restarts. Direct loopback health was
HTTP 200 with production=false and read_only=true. Nomad stdout/stderr were
empty; the POC runtime log recorded the new start with the pinned artifact
hash. No two-hour soak, native auto-revert, crash-recovery, or service-lifecycle
burn-in was replayed.

The completed generic POC's crash recovery, native auto-revert, agent-service
restart/rediscovery, and two-hour soak were deliberately not replayed for this
shadow. They remain capability evidence only, not a claim that this exact
artifact completed those long-running checks. No Production service, Recorder,
Hard Risk, execution, holdout, training, or trading path was accessed or
changed.

## Upstream basis

- [Nomad Windows service model](https://developer.hashicorp.com/nomad/docs/deploy/production/windows-service)
- [Nomad update block](https://developer.hashicorp.com/nomad/docs/job-specification/update)
- [Nomad service block](https://developer.hashicorp.com/nomad/docs/job-specification/service)
- [Nomad native service discovery](https://developer.hashicorp.com/nomad/docs/job-declare/service-discovery)
- [Nomad v2.0.5 release](https://github.com/hashicorp/nomad/releases/tag/v2.0.5)
