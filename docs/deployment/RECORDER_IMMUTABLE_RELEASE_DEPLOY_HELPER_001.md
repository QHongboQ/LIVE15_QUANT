# Recorder immutable-release deployment helper

`tools/deploy_live15_recorder_nomad.ps1` is the repository-owned administrator helper for the existing Nomad-owned Recorder. It does not deploy automatically and it does not alter Recorder/archive behavior outside the existing `deploy/nomad/live15-recorder.nomad.hcl` contract.

The administrator must run it from an elevated PowerShell context that can write the existing immutable release root and contact the existing local Nomad API. It intentionally does not grant permissions or change ACLs.

```powershell
$repo = 'D:\LIVE15_DEV\worktrees\recorder-immutable-release-deploy-helper-001'
$sha = '<40-character commit SHA reachable from origin/main>'
& "$repo\tools\deploy_live15_recorder_nomad.ps1" -Repository $repo -GitSha $sha -Preview
& "$repo\tools\deploy_live15_recorder_nomad.ps1" -Repository $repo -GitSha $sha -Apply
```

The helper requires a clean repository, an exact 40-character commit reachable from `origin/main`, the existing protected runtime SHA-256 (`72B29481593C5DA37C99248C82777FBFB56217EA7809B771BC760D0A9ECB179B`), one running `live15-recorder` allocation, successful package verification, Nomad validation/plan, and an unchanged Nomad job modify index. It carries forward only the live mutable paths and existing credential path references; it never reads credential contents.

The applied command writes a receipt under `C:\Program Files\LIVE15\ControlCenterReleases\runtime\deployment-evidence`. Roll back the exact receipt only after the active Recorder release matches its `new_release_id`:

```powershell
& "$repo\tools\deploy_live15_recorder_nomad.ps1" -Repository $repo -Rollback -ReceiptPath 'C:\Program Files\LIVE15\ControlCenterReleases\runtime\deployment-evidence\recorder-nomad-<release-id>.json' -Apply
```

Post-submit, the helper requires a single running allocation plus synchronized Kalshi WS, a nonzero synchronized-market count, fresh `kalshi_ws` and `kalshi_ws_persistence` progress, bounded queue depth, and no dropped-event regression before it reports success. If any gate fails, it exits nonzero and does not write a success receipt.
