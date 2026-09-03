# Recorder immutable-release deployment helper

`tools/deploy_live15_recorder_nomad.ps1` is the repository-owned thin deployment adapter for the existing Nomad-owned Recorder. Nomad remains the lifecycle/restart/rollback owner; the helper only prepares immutable application/runtime identities, preserves the already-registered mutable paths and credential path references, validates/plans the jobspec, submits it with a Nomad check-index, and verifies the resulting Recorder heartbeat.

The helper never starts WinSW, never repairs ACLs, never moves a Python virtual environment, and never implements a second restart or rollback state machine.

## Runtime selection

With no `-RuntimePython`, a release-only deployment keeps the runtime Python already registered on the live/stopped Nomad job. To roll a Recorder deployment onto a newly prepared runtime, first build that immutable revision with `tools/prepare_live15_production_runtime.ps1`, then pass its `Scripts\python.exe` explicitly.

```powershell
$repo = 'D:\LIVE15_QUANT'
$sha = '<40-character reviewed commit reachable from origin/main>'
$runtime = 'C:\Program Files\LIVE15\CanonicalRuntimeRevisions\runtime-py3.13.15-<lock-sha>\Scripts\python.exe'

& "$repo\tools\deploy_live15_recorder_nomad.ps1" -Repository $repo -GitSha $sha -RuntimePython $runtime -Preview
& "$repo\tools\deploy_live15_recorder_nomad.ps1" -Repository $repo -GitSha $sha -RuntimePython $runtime -Apply
```

A new target runtime must carry `live15-runtime-manifest.json` under the approved immutable revision root. The helper verifies the manifest, actual Python SHA-256, CPython 3.13.15 identity, and the current `requirements.production.lock` SHA-256. There is no hard-coded active Runtime SHA.

## Writer state and health

Before submission, zero or one running Recorder allocation is valid; more than one is always rejected. Zero is a legitimate maintenance/stopped state, not a duplicate-writer failure. After submission the deployment must converge to exactly one running Recorder allocation.

The helper also requires a heartbeat newer than the pre-submit `observed_at`, synchronized Kalshi WS, a nonzero synchronized-market count, fresh WS/persistence progress, bounded queue depth, and no dropped-event regression before reporting success. The existing jobspec keeps Nomad `auto_revert = true` for failed deployments.

## Native Nomad lifecycle

A stopped Recorder with no release/runtime change is restarted with Nomad's native command, not this helper:

```powershell
nomad job start -address=http://127.0.0.1:4646 live15-recorder
```

Deployment rollback is owned by Nomad job version history. The deployment receipt records `previous_job_version`; use the reviewed prior version with Nomad's native revert command:

```powershell
nomad job history -address=http://127.0.0.1:4646 -p live15-recorder
nomad job revert -address=http://127.0.0.1:4646 live15-recorder <previous-job-version>
```

Do not add `-purge` for normal stop/start or rollback operations. WinSW remains rollback-only historical infrastructure and must never run concurrently with the Nomad Recorder.
