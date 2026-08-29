# DEP-PKG-001 auditable SHA-pinned release pipeline

## Purpose and boundary

This is the release mechanism required before DEP-001 can deploy a reviewed
protected `origin/main` commit. It builds and validates application releases;
it never starts, stops, restarts, installs, or deploys a Windows service.
WinSW remains the sole owner and recovery authority for Recorder, Control
Center, and RuntimeSupervisor.

## Existing deployment reality (read-only map)

| Component | Classification | Current arrangement | Release-pipeline disposition |
| --- | --- | --- | --- |
| Application root | UNSAFE_FOR_PROVENANCE | `D:\LIVE15_QUANT` working tree is the WinSW working directory. | Do not package it; releases come only from a clean requested Git commit. |
| Python/venv | PARTIAL | Each service uses the root `.venv\Scripts\python.exe`. | Remains external mutable host tooling; release code is loaded from `releases/<id>/app/src`. |
| WinSW executables/XML | REUSABLE | `%BASE%` wrappers reside in `.local-tools\winsw`; XML is rendered locally from tracked templates. | XML invokes `bootstrap\release_runner.py`, staged and hash-bound from the selected release; it does not acquire service ownership. |
| `data/`, archive, WAL/SHM | REUSABLE external mutable state | Recorder data lives outside application code. | Never included in release archives or overwritten by build/stage/activation. |
| Secrets/config | REUSABLE external mutable state | XML contains only credential-path references; secret contents remain outside Git/release payloads. | Never serialized to manifests. |
| Release identity/provenance | MISSING before DEP-PKG-001 | No installed package SHA marker or rollback artifact existed. | Versioned immutable release directory, manifest, active/previous pointer, and runner receipt. |

## Release identity and layout

`python -m live15_quant.release_pipeline build --git-sha <40-char-SHA>` accepts
only a resolvable commit from a clean repository. It creates a temporary
detached worktree, verifies that checkout is clean and at the requested SHA,
then packages `git archive <SHA>`. Consequently no dirty or untracked source
can enter a release.

```text
<production-root>/
  releases/<release-id>/app/                 immutable Git-archive payload
  releases/<release-id>/release-manifest.json
  active-release.json                        atomically replaced pointer
  previous-release.json                      verified rollback target
  data/, runtime/, logs/, .secrets/          external mutable state; never packaged
```

The manifest schema records `release_id`, `git_commit_sha`, Git tree identity,
lockfile SHA-256, Python and builder versions, creation time, deterministic
file inventory, and inventory SHA-256. It contains neither credentials nor
runtime data. `verify-package` hashes every payload file and fails closed on a
missing/corrupt file, manifest mismatch, or lockfile mismatch.

## Stage, activate, rollback

Build stages beneath `releases/.<id>.staging` and replaces it once with the
final immutable directory. A failed build leaves no active pointer change.
`activate` verifies the candidate first, writes the previous verified identity,
then atomically replaces `active-release.json` using same-directory
`os.replace`. Thus no half-copied release is ever active. `rollback` verifies
the previous release before atomically restoring its identity. `--dry-run`
does not mutate either pointer.

`stage-bootstrap` copies only `app/tools/release_runner.py` from a verified
modern release into the stable `bootstrap/` location via same-directory
replacement and writes `bootstrap/bootstrap-manifest.json`. This is a stable
bootstrap **control plane**: its receipt records its source release ID/source
manifest hash and its own SHA-256, independently from the active application
pointer. `verify-bootstrap` verifies that source package and both runner hashes
without requiring it to equal the active application release.

That separation is required for the first audited deployment. A legacy capture
is an immutable `LEGACY_UNPROVEN_ROLLBACK_ARTIFACT`, including no injected
bootstrap file. After a modern bootstrap is staged, the active application may
atomically roll back to that legacy payload; the bootstrap remains verified
infrastructure while the application's Git SHA remains exactly `UNPROVEN`.
Bootstrap corruption, source-manifest mismatch, or an invalid active payload
fails closed. The runner disables application bytecode writes so imports cannot
alter an immutable release inventory.

The first audited deployment may capture the existing code with
`capture-legacy-unproven`. Its manifest is explicitly
`LEGACY_UNPROVEN_ROLLBACK_ARTIFACT`, records its installation path and content
inventory, and has `git_commit_sha = UNPROVEN`; it can never manufacture a Git
SHA. Capturing the live Production root is a future separately approved DEP-001
operation, not part of DEP-PKG-001.

## Runtime provenance

The stable `bootstrap/release_runner.py` is launched by each existing WinSW
service definition. It is copied only by `stage-bootstrap` from the selected
verified release. It validates the active pointer and manifest, changes into the
mutable Production root so relative `data/`, `runtime/`, and log paths remain
outside the immutable payload, and prepends immutable `app/src` to `sys.path`.
Its runtime receipt records the component, process PID/parent PID, interpreter,
base interpreter, application release ID/Git SHA/manifest hash, mutable working
directory, immutable module root, and distinct bootstrap source
release/manifest/hash fields. A modern receipt is valid only for either a
direct `WinSW -> runner` process chain or the one verified Windows venv shape
`WinSW -> configured venv redirector -> base-Python runner`; arbitrary
intermediaries are rejected. It does not start another process or manage
service lifecycle. A legacy rollback receipt therefore proves
`deployment_git_sha = UNPROVEN` while retaining separately verified bootstrap
provenance.

`verify_runtime_provenance` cross-checks the WinSW service PID, installed XML
bootstrap command, runner child receipt, active pointer, bootstrap hash receipt,
manifest hash, module location under the active release, and requested Git SHA.
A service that is merely running, a stale receipt, a parent-PID mismatch, or a
module/working-directory path outside the active immutable release fails.

## DEP-SERVICE-RESTART-001: canonical restart gate

`live15_quant.deployment_restart` is the single service-control authority for
an authorized DEP-001 deployment **and its rollback**. `release_pipeline`
intentionally remains package/pointer-only; deployment orchestration must not
use ad-hoc `Restart-Service`, `sc start`, or `winsw restart` commands outside
this gate.

Rollback first inspects SCM state. For `RUNNING` with a positive PID it calls
`restart_service_verified`; for `STOPPED` with PID `0` it calls
`recover_service_verified`, which skips the redundant stop request. Both are
public entry points to the same internal transition, generation-binding, audit,
and provenance machinery. Any other precheck state fails closed.

For each independently WinSW-owned service (Recorder, Control Center, and
RuntimeSupervisor), the gate records an atomic non-empty pre-transition
receipt. A running service requires this exact sequence:

```text
PRECHECK -> STOP_REQUESTED -> STOPPED_CONFIRMED -> OLD_PID_GONE
-> START_REQUESTED -> RUNNING_CONFIRMED -> NEW_PID_CONFIRMED
-> WINSW_SERVICE_MODE_START_CONFIRMED
-> RELEASE_RUNNER_RECEIPT_CONFIRMED -> PROVENANCE_CONFIRMED
```

A stopped-service recovery records `STOPPED_PRECHECK`, then begins at
`START_REQUESTED`; it must still produce a new PID, a fresh WinSW service-mode
entry, a fresh runner receipt, and valid provenance. Once `NEW_PID_CONFIRMED`
is recorded, every later gate re-observes SCM and requires the same `RUNNING`
WinSW PID. A stopped service is `SERVICE_GENERATION_LOST`; a different PID is
`SERVICE_GENERATION_CHANGED`, and neither may satisfy a later receipt or
provenance check.

The precheck binds the SCM ImagePath-derived WinSW executable to its adjacent
same-basename XML, checks the installed XML hash, and captures a wrapper-log
cursor.  A stop/start command's return code is only a request acknowledgement:
SCM state and PID observations are the evidence.  The post-start WinSW wrapper
log must contain a *new* `Starting WinSW in service mode` entry after the
cursor; console-mode output is invalid.

### Candidate sidecar credential binding

Release XML deliberately carries symbolic Kalshi credential-path placeholders;
it never carries credential material.  Before replacing an installed sidecar,
the deployment authority must use
`render_candidate_winsw_sidecar` to retain the installed sidecar's two existing
external, absolute credential-path references.  It rejects a missing,
placeholder, or relative installed reference before service control, and never
reads credential-file contents or writes the path values to deployment
evidence.  This is necessary because a LocalSystem WinSW service does not
inherit a deploy user's environment variables.  Copying a release XML directly
into a service sidecar is prohibited.

The normal delegated deploy account may hold the service-control ACE while a
LocalSystem WinSW process rejects `OpenProcess` with access denied.  Native
full-path inspection remains preferred.  Only for that exact access-denied
case, the gate reads the documented read-only `Win32_Process` PID, parent PID,
creation time, and image basename; the basename must still match the
independently SCM/ImagePath- and parsed-XML-bound executable.  This preserves
PID-reuse and direct/one-redirector checks without granting process ACLs or
accepting an arbitrary ancestor.  `Win32_Process` documents both the
creation/parent identity fields and their PID-reuse caveat.[^win32-process]

For a modern active release, the gate rejects a missing or stale
`runtime/release-runtime-<component>.json`, parent-PID mismatch, or any failed
`verify_runtime_provenance` binding.  A legacy rollback invokes the exact same
gate with expected Git SHA `UNPROVEN`; its provenance remains explicitly
`LEGACY_UNPROVEN`.  Both success and failure persist an atomic non-empty
`runtime/deployment-evidence/<deployment-id>/service-restart-<component>-<transition-kind>-<transition_id>.json`.
`transition-kind` is the explicit modern/legacy and restart/recover-stopped
state-machine mode. Before any service operation, the gate atomically reserves
that exact transition identity with a non-empty receipt; a collision fails
closed. Candidate and rollback (or a stopped-service recovery) therefore
cannot overwrite one another's evidence. If that audit write fails, the gate
fails closed.

## Approved future sequence

1. Freeze a reviewed protected SHA and create a clean source worktree.
2. Build and verify its release under the Production root's `releases/`.
3. With separate human approval, capture the legacy rollback artifact if needed,
   stage the matching bootstrap, render/install the reviewed WinSW XML, and
   use the SCM-state-selected canonical transition for each approved service.
   Never mark the restart stage complete merely because a service still reports
   Running.
4. Verify package, active pointer, process provenance, ownership, health, and
   bounded runtime behavior.  On rollback, use the same verified restart gate
   against the restored legacy identity.

DEP-PKG-001 performs only step 1's offline simulation and no Production action.

[^win32-process]: [Microsoft: Win32_Process class](https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-process)
