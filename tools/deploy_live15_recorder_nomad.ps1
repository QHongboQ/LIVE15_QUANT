[CmdletBinding()]
param(
    [ValidatePattern('^[0-9a-fA-F]{40}$')][string]$GitSha,
    [Parameter(Mandatory)][string]$Repository,
    [string]$ReleaseRoot = 'C:\Program Files\LIVE15\ControlCenterReleases',
    [string]$Jobspec,
    [string]$NomadPath = 'D:\LIVE15_NOMAD_POC\bin\nomad.exe',
    [string]$NomadAddress = 'http://127.0.0.1:4646',
    [string]$RuntimePython = 'C:\Program Files\LIVE15\ControlCenterRuntime\Scripts\python.exe',
    [int]$HealthTimeoutSeconds = 120,
    [switch]$Apply,
    [switch]$Preview,
    [switch]$Rollback,
    [string]$ReceiptPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$JobId = 'live15-recorder'
$ExpectedRuntimeSha256 = '72B29481593C5DA37C99248C82777FBFB56217EA7809B771BC760D0A9ECB179B'

function Assert-LocalNomad {
    if ($NomadAddress -ne 'http://127.0.0.1:4646') {
        throw 'NomadAddress must be the isolated local control plane: http://127.0.0.1:4646.'
    }
}

function Invoke-Git([string[]]$Arguments) {
    $value = & git -C $Repository @Arguments
    if ($LASTEXITCODE) { throw "git $($Arguments -join ' ') failed." }
    return ($value | Out-String).Trim()
}

function Invoke-ReleasePipeline([string[]]$Arguments) {
    # release_pipeline writes normal operational messages to stdout. Send those
    # messages to the host so a caller such as Get-Identity returns only its
    # documented structured object while failures still remain terminating.
    & $RuntimePython (Join-Path $Repository 'src\live15_quant\release_pipeline.py') @Arguments | Out-Host
    if ($LASTEXITCODE) { throw 'Immutable release pipeline failed.' }
}

function Resolve-Jobspec([string]$Candidate) {
    if ([string]::IsNullOrWhiteSpace($Candidate)) {
        return Join-Path $Repository 'deploy\nomad\live15-recorder.nomad.hcl'
    }
    return $Candidate
}

function Get-LiveJob {
    $job = Invoke-RestMethod -Uri "$NomadAddress/v1/job/$JobId"
    $group = @($job.TaskGroups | Where-Object Name -eq 'recorder')
    $task = @($group[0].Tasks | Where-Object Name -eq 'recorder')
    if ($job.ID -ne $JobId -or $group.Count -ne 1 -or $task.Count -ne 1 -or $null -eq $job.JobModifyIndex) {
        throw 'Live Recorder job shape is unsafe.'
    }
    return [pscustomobject]@{ JobModifyIndex=[int64]$job.JobModifyIndex; Task=$task[0]; Job=$job }
}

function Assert-OneRecorderWriter {
    $allocations = @(Invoke-RestMethod -Uri "$NomadAddress/v1/job/$JobId/allocations")
    $writers = @($allocations | Where-Object { $_.ClientStatus -eq 'running' -and $_.TaskGroup -eq 'recorder' })
    if ($writers.Count -ne 1) {
        throw "Duplicate or missing Recorder writer is unsafe (running allocations=$($writers.Count))."
    }
}

function Get-Identity([string]$ReleaseId) {
    if ([string]::IsNullOrWhiteSpace($ReleaseId)) { throw 'Release ID is required.' }
    $manifestPath = Join-Path $ReleaseRoot "releases\$ReleaseId\release-manifest.json"
    $app = Join-Path $ReleaseRoot "releases\$ReleaseId\app"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or -not (Test-Path -LiteralPath $app -PathType Container)) {
        throw "Verified release identity is missing: $ReleaseId"
    }
    Invoke-ReleasePipeline @('verify-package', '--release-root', $ReleaseRoot, '--release-id', $ReleaseId)
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    if ($manifest.release_id -ne $ReleaseId -or [string]::IsNullOrWhiteSpace([string]$manifest.git_commit_sha)) {
        throw "Verified release identity is invalid: $ReleaseId"
    }
    return [pscustomobject]@{
        ReleaseId = $ReleaseId
        AppRoot = $app
        Manifest = $manifest
        ManifestSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $manifestPath).Hash
    }
}

function Get-RecorderHealth([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
}

function Get-HealthInteger($Health, [string]$Name) {
    $value = $Health.PSObject.Properties[$Name]
    if ($null -eq $value -or $null -eq $value.Value) { return [int64]0 }
    return [int64]$value.Value
}

function Get-HealthAge($Health, [string]$Name) {
    $ages = $Health.PSObject.Properties['worker_progress_age_seconds']
    if ($null -eq $ages -or $null -eq $ages.Value) { return [double]::PositiveInfinity }
    $value = $ages.Value.PSObject.Properties[$Name]
    if ($null -eq $value -or $null -eq $value.Value) { return [double]::PositiveInfinity }
    return [double]$value.Value
}

function Assert-HealthyRecorder([int64]$BeforeDropped) {
    $path = [string]$live.Task.Env.LIVE15_RECORDER_HEALTH_PATH
    $deadline = (Get-Date).ToUniversalTime().AddSeconds($HealthTimeoutSeconds)
    do {
        $health = Get-RecorderHealth $path
        if ($null -ne $health) {
            $wsAge = Get-HealthAge $health 'kalshi_ws'
            $persistenceAge = Get-HealthAge $health 'kalshi_ws_persistence'
            $dropped = Get-HealthInteger $health 'kalshi_ws_queue_dropped'
            $fresh = $health.kalshi_ws_connection_state -eq 'synchronized' -and
                (Get-HealthInteger $health 'kalshi_ws_synchronized_count') -gt 0 -and
                $wsAge -le 15 -and $persistenceAge -le 15 -and
                (Get-HealthInteger $health 'kalshi_ws_queue_depth') -le (Get-HealthInteger $health 'kalshi_ws_queue_capacity')
            if ($fresh -and $dropped -le $BeforeDropped) { return }
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date).ToUniversalTime() -lt $deadline)
    throw 'Recorder did not return to synchronized/fresh/no-drop-regression health before timeout.'
}

function Set-NomadVariables($Live, $Identity) {
    $env:NOMAD_VAR_recorder_runtime_python = $RuntimePython
    $env:NOMAD_VAR_recorder_app_root = $Identity.AppRoot
    $env:NOMAD_VAR_recorder_work_dir = [string]$Live.Task.Config.work_dir
    if ([string]::IsNullOrWhiteSpace($env:NOMAD_VAR_recorder_work_dir)) { throw 'Live Recorder configuration is missing work_dir.' }
    $preserved = @{
        recorder_data_path = 'LIVE15_RECORDER_DATA_PATH'
        recorder_health_path = 'LIVE15_RECORDER_HEALTH_PATH'
        recorder_control_path = 'LIVE15_RECORDER_CONTROL_PATH'
        recorder_pid_path = 'LIVE15_RECORDER_PID_PATH'
        kalshi_api_key_id_path = 'LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH'
        kalshi_private_key_path = 'LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH'
        pyth_api_key_path = 'LIVE15_PYTH_API_KEY_PATH'
    }
    foreach ($variable in $preserved.Keys) {
        $value = [string]$Live.Task.Env.($preserved[$variable])
        if ([string]::IsNullOrWhiteSpace($value)) { throw "Live Recorder configuration is missing $($preserved[$variable])." }
        [Environment]::SetEnvironmentVariable("NOMAD_VAR_$variable", $value, 'Process')
    }
    $env:NOMAD_VAR_release_id = $Identity.ReleaseId
    $env:NOMAD_VAR_release_git_sha = $Identity.Manifest.git_commit_sha
    $env:NOMAD_VAR_release_manifest_sha256 = $Identity.ManifestSha256
    $env:NOMAD_VAR_artifact_manifest_sha256 = $Identity.Manifest.artifact_manifest_sha256
    $env:NOMAD_VAR_requirements_lock_sha256 = $Identity.Manifest.requirements_lock_sha256
    $env:NOMAD_VAR_runtime_python_sha256 = $ExpectedRuntimeSha256
}

function Write-Receipt($Previous, $Next, [int64]$Version) {
    $directory = Join-Path $ReleaseRoot 'runtime\deployment-evidence'
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $path = Join-Path $directory ("recorder-nomad-$($Next.ReleaseId).json")
    [ordered]@{
        previous_release_id = $Previous.ReleaseId
        previous_recorder_app_root = $Previous.AppRoot
        new_release_id = $Next.ReleaseId
        new_recorder_app_root = $Next.AppRoot
        nomad_job_id = $JobId
        nomad_job_modify_index = $Version
        recorded_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $path -Encoding utf8
    return $path
}

function Assert-ReviewedCommit {
    if ([string]::IsNullOrWhiteSpace($GitSha)) { throw 'GitSha is required unless Rollback is selected.' }
    if ((Invoke-Git @('status', '--porcelain'))) { throw 'Repository must be clean.' }
    $resolved = Invoke-Git @('rev-parse', '--verify', "$GitSha^{commit}")
    if ($resolved -ne $GitSha.ToLower()) { throw 'Git SHA is not an exact commit.' }
    & git -C $Repository merge-base --is-ancestor $GitSha origin/main
    if ($LASTEXITCODE) { throw 'Git SHA is not a reviewed commit reachable from origin/main.' }
}

$nomadVariableNames = @(
    'recorder_runtime_python', 'recorder_app_root', 'recorder_work_dir', 'recorder_data_path',
    'recorder_health_path', 'recorder_control_path', 'recorder_pid_path', 'kalshi_api_key_id_path',
    'kalshi_private_key_path', 'pyth_api_key_path', 'release_id', 'release_git_sha',
    'release_manifest_sha256', 'artifact_manifest_sha256', 'requirements_lock_sha256', 'runtime_python_sha256'
)
$previousEnvironment = @{}
foreach ($name in $nomadVariableNames) { $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable("NOMAD_VAR_$name", 'Process') }

try {
    if ($Apply -and $Preview) { throw 'Specify either Apply or Preview, not both.' }
    Assert-LocalNomad
    $Jobspec = Resolve-Jobspec $Jobspec
    foreach ($path in @($Repository, $Jobspec, $NomadPath, $RuntimePython)) {
        if (-not (Test-Path -LiteralPath $path)) { throw "Required path is unavailable: $path" }
    }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $RuntimePython).Hash -ne $ExpectedRuntimeSha256) {
        throw 'Canonical runtime SHA-256 mismatch.'
    }
    $live = Get-LiveJob
    Assert-OneRecorderWriter
    $currentId = [string]$live.Task.Meta.release_id
    $previous = Get-Identity $currentId
    if ($Rollback) {
        if ([string]::IsNullOrWhiteSpace($ReceiptPath) -or -not (Test-Path -LiteralPath $ReceiptPath -PathType Leaf)) {
            throw 'Rollback requires an existing deployment ReceiptPath.'
        }
        $receipt = Get-Content -Raw -LiteralPath $ReceiptPath | ConvertFrom-Json
        if ($receipt.nomad_job_id -ne $JobId -or $receipt.new_release_id -ne $previous.ReleaseId) {
            throw 'Rollback receipt does not describe the live Recorder release.'
        }
        $next = Get-Identity ([string]$receipt.previous_release_id)
    } else {
        Assert-ReviewedCommit
        $tree = Invoke-Git @('rev-parse', "$GitSha^{tree}")
        $releaseId = "live15-$($GitSha.Substring(0, 12))-$($tree.Substring(0, 12))"
        $existingManifest = Join-Path $ReleaseRoot "releases\$releaseId\release-manifest.json"
        if ($Apply) {
            if (-not (Test-Path -LiteralPath $existingManifest -PathType Leaf)) {
                Invoke-ReleasePipeline @('build', '--repo', $Repository, '--git-sha', $GitSha, '--release-root', $ReleaseRoot)
            }
            $next = Get-Identity $releaseId
        } else {
            $next = [pscustomobject]@{ ReleaseId=$releaseId; AppRoot=(Join-Path $ReleaseRoot "releases\$releaseId\app"); Manifest=[pscustomobject]@{ git_commit_sha=$GitSha; artifact_manifest_sha256='PREVIEW'; requirements_lock_sha256='PREVIEW' }; ManifestSha256='PREVIEW' }
        }
    }
    Set-NomadVariables $live $next
    & $NomadPath job validate "-address=$NomadAddress" $Jobspec
    if ($LASTEXITCODE) { throw 'Nomad validation failed.' }
    & $NomadPath job plan "-address=$NomadAddress" $Jobspec
    if ($LASTEXITCODE -notin @(0, 1)) { throw "Nomad plan failed (exit=$LASTEXITCODE)." }
    if (-not $Apply) {
        [pscustomobject]@{ mode='PREVIEW'; previous_release=$previous.ReleaseId; next_release=$next.ReleaseId; job_modify_index=$live.JobModifyIndex; mutation='NONE' } | ConvertTo-Json
        exit 0
    }
    $fresh = Get-LiveJob
    if ($fresh.JobModifyIndex -ne $live.JobModifyIndex) { throw 'Nomad job changed after plan; re-plan is required.' }
    Assert-OneRecorderWriter
    $healthBefore = Get-RecorderHealth ([string]$live.Task.Env.LIVE15_RECORDER_HEALTH_PATH)
    if ($null -eq $healthBefore) { throw 'Recorder health file is unavailable before deployment.' }
    $droppedBefore = Get-HealthInteger $healthBefore 'kalshi_ws_queue_dropped'
    & $NomadPath job run "-address=$NomadAddress" "-check-index=$($live.JobModifyIndex)" $Jobspec
    if ($LASTEXITCODE) { throw "Nomad job submission failed (exit=$LASTEXITCODE)." }
    Assert-OneRecorderWriter
    Assert-HealthyRecorder $droppedBefore
    $receipt = Write-Receipt $previous $next $live.JobModifyIndex
    Write-Output "RECORDER_DEPLOYMENT = PASS receipt=$receipt"
} catch {
    [Console]::Error.WriteLine("RECORDER_DEPLOYMENT_ERROR: $($_.Exception.Message)")
    exit 1
} finally {
    foreach ($name in $nomadVariableNames) { [Environment]::SetEnvironmentVariable("NOMAD_VAR_$name", $previousEnvironment[$name], 'Process') }
}
