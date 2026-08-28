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
release into the stable `bootstrap/` location via same-directory replacement
and writes `bootstrap/bootstrap-manifest.json`. `verify-bootstrap` requires
that receipt, the active release ID/manifest hash, and both runner hashes to
agree. A future release or rollback stages its matching bootstrap before the
separately authorized service restart; a mismatch fails closed.

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
immutable `app` directory, prepends `app/src` to `sys.path`, and writes a small
runtime receipt with component, its parent WinSW PID, interpreter, release ID,
Git SHA, manifest hash, working directory, and module root. It does not start
another process or manage service lifecycle.

`verify_runtime_provenance` cross-checks the WinSW service PID, installed XML
bootstrap command, runner child receipt, active pointer, bootstrap hash receipt,
manifest hash, module location under the active release, and requested Git SHA.
A service that is merely running, a stale receipt, a parent-PID mismatch, or a
module/working-directory path outside the active immutable release fails.

## Approved future sequence

1. Freeze a reviewed protected SHA and create a clean source worktree.
2. Build and verify its release under the Production root's `releases/`.
3. With separate human approval, capture the legacy rollback artifact if needed,
   stage the matching bootstrap, render/install the reviewed WinSW XML, and
   perform controlled activation and service restart.
4. Verify package, active pointer, process provenance, ownership, health, and
   bounded runtime behavior; emit bounded pre/post deployment receipts.

DEP-PKG-001 performs only step 1's offline simulation and no Production action.
