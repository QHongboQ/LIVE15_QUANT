# DEP-001 Phase A read-only preflight — 2026-08-29

## Scope and authority

This is a read-only preflight of the protected Windows checkout and its existing
WinSW service metadata. It is not a deployment receipt, restart authorization,
Production configuration change, or approval to read secrets. No file, service,
pointer, process, account, or runtime state was changed.

## Observations at preflight time

| Check | Observation |
| --- | --- |
| Protected checkout | `D:\LIVE15_QUANT`, branch `main`, HEAD `c2ded1d4fc172b184db4e0fb6faf6b5d6d0100e0` |
| Then-current protected main | `origin/main=4d088930cc83634faf807188fba386f7a7a34bea`; checkout was 37 commits behind |
| Dirty state | Tracked modification: `deploy/windows/live15-recorder.xml`; untracked `active-release.json`, `bootstrap/`, `docs/sqlite-process-idempotency-practices.md`, `previous-release.json`, and `releases/`; one checker-temp directory was not readable |
| Services | `LIVE15Recorder`, `LIVE15ControlCenter`, and `LIVE15RuntimeSupervisor` were all `Running` under `LocalSystem` |
| Active pointer | `active-release.json` resolves to `legacy-unproven-08989b3efd7d19f6`; its manifest declares `git_commit_sha=UNPROVEN` and its SHA-256 matches the pointer |
| Modern prior release | `live15-13fcc4e7fd73-baa2e33725fd` exists with Git SHA `13fcc4e7fd733bc2f35fc111a0eb2bedb61e5606` |
| Current Recorder receipt | The non-secret receipt points at the modern release module root, but this does not override the `UNPROVEN` active pointer or prove installed-service provenance |
| Tracked WinSW templates | The three templates invoke the mutable root `.venv\Scripts\python.exe` with the root working directory; they do not themselves establish an immutable current-main release-runner binding |

## Result

`PHASE_A_PREFLIGHT_NOT_READY`.

The protected root must first be reconciled to a clean, reviewed current-main
release and its immutable provenance re-established by a separately authorized
deployment task. The running services were not restarted, and no deployment or
Production mutation was attempted. The required deployment/restart gate remains
`DEP001_DEPLOY_APPROVED`.

## Evidence boundary

This document records only non-secret identities, service state, and paths needed
for the gate. It does not expose credential contents, holdout data, trading
state, or Production writes. Hosted CI remains `CI_DEFERRED_QUOTA`.
