[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{40}$')][string]$GitSha,
    [Parameter(Mandatory)][string]$Repository,
    [string]$ReleaseRoot = 'C:\Program Files\LIVE15\ControlCenterReleases',
    [string]$Jobspec,
    [string]$NomadPath = 'D:\LIVE15_NOMAD_POC\bin\nomad.exe',
    [string]$NomadAddress = 'http://127.0.0.1:4646',
    [string]$RuntimePython,
    [string]$RuntimeRevisionRoot = 'C:\Program Files\LIVE15\CanonicalRuntimeRevisions',
    [int]$HealthTimeoutSeconds = 120,
    [switch]$Apply,
    [switch]$Preview
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$JobId = 'live15-recorder'
$RuntimeManifestName = 'live15-runtime-manifest.json'
$PythonVersionExpression = "import sys; print('.'.join(map(str, sys.version_info[:3])))"
$ProductionLock = Join-Path $Repository 'requirements.production.lock'

function Assert-LocalNomad {
    if ($NomadAddress -ne 'http://127.0.0.1:4646') {
        throw 'NomadAddress must be the isolated local control plane: http://127.0.0.1:4646.'
    }
}

function Get-FileSha256([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($stream)
        return ([System.BitConverter]::ToString($hash)).Replace('-', '')
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

function Invoke-Git([string[]]$Arguments) {
    $value = & git -C $Repository @Arguments
    if ($LASTEXITCODE) { throw "git $($Arguments -join ' ') failed." }
    return ($value | Out-String).Trim()
}

function Invoke-ReleasePipeline([string]$Python, [string[]]$Arguments) {
    & $Python (Join-Path $Repository 'src\live15_quant\release_pipeline.py') @Arguments | Out-Host
    if ($LASTEXITCODE) { throw 'Immutable release pipeline failed.' }
}

function Resolve-Jobspec([string]$Candidate) {
    if ([string]::IsNullOrWhiteSpace($Candidate)) {
        return Join-Path $Repository 'deploy\nomad\live15-recorder.nomad.hcl'
    }
    return $Candidate
}

function Get-LiveJob {
    $job = Invoke-RestMethod -Method Get -ErrorAction Stop -Uri "$NomadAddress/v1/job/$JobId"
    $group = @($job.TaskGroups | Where-Object { $_.Name -eq 'recorder' })
    if ($group.Count -ne 1) { throw 'Live Recorder job shape is unsafe.' }
    $task = @($group[0].Tasks | Where-Object { $_.Name -eq 'recorder' })
    if ($job.ID -ne $JobId -or $task.Count -ne 1 -or $null -eq $job.JobModifyIndex -or $null -eq $job.Version) {
        throw 'Live Recorder job shape is unsafe.'
    }
    return [pscustomobject]@{
        JobModifyIndex = [int64]$job.JobModifyIndex
        Version = [int64]$job.Version
        Task = $task[0]
        Job = $job
    }
}

function Get-RecorderWriterCount {
    $allocations = Invoke-RestMethod -Method Get -ErrorAction Stop -Uri "$NomadAddress/v1/job/$JobId/allocations"
    $count = 0
    foreach ($allocation in @($allocations)) {
        if ($null -eq $allocation) { continue }
        $status = $allocation.PSObject.Properties['ClientStatus']
        $group = $allocation.PSObject.Properties['TaskGroup']
        if ($null -eq $status -or $null -eq $group) { throw 'Nomad allocation response shape is unsafe.' }
        if ([string]$status.Value -eq 'running' -and [string]$group.Value -eq 'recorder') { $count++ }
    }
    return [int]$count
}

function Assert-AtMostOneRecorderWriter {
    $count = Get-RecorderWriterCount
    if ($count -gt 1) { throw "Duplicate Recorder writers are unsafe (running allocations=$count)." }
    return $count
}

function Wait-OneRecorderWriter {
    $deadline = (Get-Date).ToUniversalTime().AddSeconds($HealthTimeoutSeconds)
    do {
        $count = Get-RecorderWriterCount
        if ($count -gt 1) { throw "Duplicate Recorder writers are unsafe (running allocations=$count)." }
        if ($count -eq 1) { return }
        Start-Sleep -Seconds 1
    } while ((Get-Date).ToUniversalTime() -lt $deadline)
    throw 'Recorder deployment did not converge to one running writer before timeout.'
}

function Get-ReleaseIdentity([string]$ReleaseId, [string]$ReleasePython) {
    if ([string]::IsNullOrWhiteSpace($ReleaseId)) { throw 'Release ID is required.' }
    $manifestPath = Join-Path $ReleaseRoot "releases\$ReleaseId\release-manifest.json"
    $app = Join-Path $ReleaseRoot "releases\$ReleaseId\app"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or -not (Test-Path -LiteralPath $app -PathType Container)) {
        throw "Verified release identity is missing: $ReleaseId"
    }
    Invoke-ReleasePipeline $ReleasePython @('verify-package', '--release-root', $ReleaseRoot, '--release-id', $ReleaseId)
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    if ($manifest.release_id -ne $ReleaseId -or [string]::IsNullOrWhiteSpace([string]$manifest.git_commit_sha)) {
        throw "Verified release identity is invalid: $ReleaseId"
    }
    return [pscustomobject]@{
        ReleaseId = $ReleaseId
        AppRoot = $app
        Manifest = $manifest
        ManifestSha256 = Get-FileSha256 $manifestPath
    }
}

function Get-PythonVersion([string]$Python) {
    $version = (& $Python -c $PythonVersionExpression | Out-String).Trim()
    if ($LASTEXITCODE) { throw "Python version query failed: $Python" }
    return $version
}

function Get-RuntimeIdentity([string]$Python, [string]$LivePython) {
    $resolved = (Resolve-Path -LiteralPath $Python -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) { throw "Runtime Python is unavailable: $resolved" }
    $version = Get-PythonVersion $resolved
    if ($version -ne '3.13.15') { throw 'Recorder runtime must use CPython 3.13.15.' }
    $sha = Get-FileSha256 $resolved
    $runtimeRoot = Split-Path -Parent (Split-Path -Parent $resolved)
    $manifestPath = Join-Path $runtimeRoot $RuntimeManifestName
    $liveResolved = (Resolve-Path -LiteralPath $LivePython -ErrorAction Stop).Path

    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        if ($resolved -ne $liveResolved) {
            throw 'A new target runtime must be a prepared immutable LIVE15 runtime revision with a manifest.'
        }
        return [pscustomobject]@{
            Python = $resolved
            PythonSha256 = $sha
            RuntimeRoot = $runtimeRoot
            RuntimeMode = 'LIVE_LEGACY'
            ManifestPath = $null
        }
    }

    $revisionRootResolved = (Resolve-Path -LiteralPath $RuntimeRevisionRoot -ErrorAction Stop).Path
    $prefix = $revisionRootResolved.TrimEnd('\') + '\'
    if (-not $runtimeRoot.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Prepared runtime manifest exists outside the approved immutable revision root.'
    }
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    $lockSha = Get-FileSha256 $ProductionLock
    if ([string]$manifest.runtime_root -ne $runtimeRoot -or
        [string]$manifest.python -ne $resolved -or
        [string]$manifest.python_version -ne $version -or
        [string]$manifest.python_sha256 -ne $sha -or
        [string]$manifest.production_lock_sha256 -ne $lockSha -or
        [string]::IsNullOrWhiteSpace([string]$manifest.dependency_identity)) {
        throw 'Prepared runtime manifest does not match the target runtime or current Production lock.'
    }
    return [pscustomobject]@{
        Python = $resolved
        PythonSha256 = $sha
        RuntimeRoot = $runtimeRoot
        RuntimeMode = 'IMMUTABLE_REVISION'
        ManifestPath = $manifestPath
        DependencyIdentity = [string]$manifest.dependency_identity
    }
}

function Assert-LiveIdentityBinding($Live, $ReleaseIdentity, $RuntimeIdentity) {
    $expected = [ordered]@{
        release_id = [string]$ReleaseIdentity.ReleaseId
        release_git_sha = [string]$ReleaseIdentity.Manifest.git_commit_sha
        release_manifest_sha256 = [string]$ReleaseIdentity.ManifestSha256
        artifact_manifest_sha256 = [string]$ReleaseIdentity.Manifest.artifact_manifest_sha256
        requirements_lock_sha256 = [string]$ReleaseIdentity.Manifest.requirements_lock_sha256
        runtime_python_sha256 = [string]$RuntimeIdentity.PythonSha256
    }
    foreach ($name in $expected.Keys) {
        $property = $Live.Task.Meta.PSObject.Properties[$name]
        $actual = if ($null -eq $property) { '' } else { [string]$property.Value }
        if ([string]::IsNullOrWhiteSpace($actual)) {
            throw "Live Recorder metadata is missing identity binding: $name"
        }
        if (-not [string]::Equals($actual, [string]$expected[$name], [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Live Recorder metadata mismatch for $name."
        }
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

function Get-ObservedAt($Health) {
    if ($null -eq $Health) { return [DateTimeOffset]::MinValue }
    $property = $Health.PSObject.Properties['observed_at']
    if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value)) { return [DateTimeOffset]::MinValue }
    return [DateTimeOffset]::Parse([string]$property.Value)
}

function Assert-HealthyRecorder([int64]$BeforeDropped, [DateTimeOffset]$BeforeObservedAt, [string]$HealthPath) {
    $deadline = (Get-Date).ToUniversalTime().AddSeconds($HealthTimeoutSeconds)
    do {
        $health = Get-RecorderHealth $HealthPath
        if ($null -ne $health) {
            $observedAt = Get-ObservedAt $health
            $wsAge = Get-HealthAge $health 'kalshi_ws'
            $persistenceAge = Get-HealthAge $health 'kalshi_ws_persistence'
            $dropped = Get-HealthInteger $health 'kalshi_ws_queue_dropped'
            $fresh = $observedAt -gt $BeforeObservedAt -and
                $health.kalshi_ws_connection_state -eq 'synchronized' -and
                (Get-HealthInteger $health 'kalshi_ws_synchronized_count') -gt 0 -and
                $wsAge -le 15 -and $persistenceAge -le 15 -and
                (Get-HealthInteger $health 'kalshi_ws_queue_depth') -le (Get-HealthInteger $health 'kalshi_ws_queue_capacity')
            if ($fresh -and $dropped -le $BeforeDropped) { return }
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date).ToUniversalTime() -lt $deadline)
    throw 'Recorder did not return to a newer synchronized/fresh/no-drop-regression heartbeat before timeout.'
}

function Set-NomadVariables($Live, $ReleaseIdentity, $RuntimeIdentity) {
    $env:NOMAD_VAR_recorder_runtime_python = $RuntimeIdentity.Python
    $env:NOMAD_VAR_recorder_app_root = $ReleaseIdentity.AppRoot
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
    $env:NOMAD_VAR_release_id = $ReleaseIdentity.ReleaseId
    $env:NOMAD_VAR_release_git_sha = $ReleaseIdentity.Manifest.git_commit_sha
    $env:NOMAD_VAR_release_manifest_sha256 = $ReleaseIdentity.ManifestSha256
    $env:NOMAD_VAR_artifact_manifest_sha256 = $ReleaseIdentity.Manifest.artifact_manifest_sha256
    $env:NOMAD_VAR_requirements_lock_sha256 = $ReleaseIdentity.Manifest.requirements_lock_sha256
    $env:NOMAD_VAR_runtime_python_sha256 = $RuntimeIdentity.PythonSha256
}

function Write-Receipt($Previous, $Next, $PreviousRuntime, $NextRuntime, [int64]$PreviousVersion, [int64]$NewVersion) {
    $directory = Join-Path $ReleaseRoot 'runtime\deployment-evidence'
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $runtimeShort = $NextRuntime.PythonSha256.Substring(0, 12)
    $path = Join-Path $directory ("recorder-nomad-$($Next.ReleaseId)-$runtimeShort.json")
    [ordered]@{
        previous_job_version = $PreviousVersion
        new_job_version = $NewVersion
        previous_release_id = $Previous.ReleaseId
        new_release_id = $Next.ReleaseId
        previous_runtime_python = $PreviousRuntime.Python
        new_runtime_python = $NextRuntime.Python
        new_runtime_python_sha256 = $NextRuntime.PythonSha256
        nomad_job_id = $JobId
        recorded_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $path -Encoding utf8
    return $path
}

function Assert-ReviewedCommit {
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
$jobSubmitted = $false
$rollbackVersion = $null

try {
    if ($Apply -and $Preview) { throw 'Specify either Apply or Preview, not both.' }
    Assert-LocalNomad
    $Jobspec = Resolve-Jobspec $Jobspec
    foreach ($path in @($Repository, $Jobspec, $NomadPath, $ProductionLock)) {
        if (-not (Test-Path -LiteralPath $path)) { throw "Required path is unavailable: $path" }
    }
    Assert-ReviewedCommit
    $live = Get-LiveJob
    $rollbackVersion = $live.Version
    $preWriterCount = Assert-AtMostOneRecorderWriter
    $liveRuntimePython = [string]$live.Task.Config.command
    if ([string]::IsNullOrWhiteSpace($liveRuntimePython)) { throw 'Live Recorder job does not declare a runtime Python command.' }
    $targetRuntimePython = if ([string]::IsNullOrWhiteSpace($RuntimePython)) { $liveRuntimePython } else { $RuntimePython }
    $previousRuntime = Get-RuntimeIdentity $liveRuntimePython $liveRuntimePython
    $nextRuntime = Get-RuntimeIdentity $targetRuntimePython $liveRuntimePython

    $currentId = [string]$live.Task.Meta.release_id
    $previous = Get-ReleaseIdentity $currentId $liveRuntimePython
    Assert-LiveIdentityBinding $live $previous $previousRuntime
    $tree = Invoke-Git @('rev-parse', "$GitSha^{tree}")
    $releaseId = "live15-$($GitSha.Substring(0, 12))-$($tree.Substring(0, 12))"
    $existingManifest = Join-Path $ReleaseRoot "releases\$releaseId\release-manifest.json"
    if ($Apply) {
        if (-not (Test-Path -LiteralPath $existingManifest -PathType Leaf)) {
            Invoke-ReleasePipeline $liveRuntimePython @('build', '--repo', $Repository, '--git-sha', $GitSha, '--release-root', $ReleaseRoot)
        }
        $next = Get-ReleaseIdentity $releaseId $liveRuntimePython
    } else {
        $next = [pscustomobject]@{
            ReleaseId = $releaseId
            AppRoot = Join-Path $ReleaseRoot "releases\$releaseId\app"
            Manifest = [pscustomobject]@{ git_commit_sha=$GitSha; artifact_manifest_sha256='PREVIEW'; requirements_lock_sha256='PREVIEW' }
            ManifestSha256 = 'PREVIEW'
        }
    }

    Set-NomadVariables $live $next $nextRuntime
    & $NomadPath job validate "-address=$NomadAddress" $Jobspec
    if ($LASTEXITCODE) { throw 'Nomad validation failed.' }
    & $NomadPath job plan "-address=$NomadAddress" $Jobspec
    if ($LASTEXITCODE -notin @(0, 1)) { throw "Nomad plan failed (exit=$LASTEXITCODE)." }

    if (-not $Apply) {
        [pscustomobject]@{
            mode = 'PREVIEW'
            previous_release = $previous.ReleaseId
            next_release = $next.ReleaseId
            previous_runtime = $previousRuntime.Python
            target_runtime = $nextRuntime.Python
            pre_running_allocations = $preWriterCount
            job_modify_index = $live.JobModifyIndex
            rollback_authority = 'Nomad job version history / nomad job revert'
            mutation = 'NONE'
        } | ConvertTo-Json -Depth 4
        exit 0
    }

    $fresh = Get-LiveJob
    if ($fresh.JobModifyIndex -ne $live.JobModifyIndex) { throw 'Nomad job changed after plan; re-plan is required.' }
    $freshWriterCount = Assert-AtMostOneRecorderWriter
    if ($freshWriterCount -ne $preWriterCount) { throw 'Recorder writer state changed after planning; re-plan is required.' }
    $healthPath = [string]$live.Task.Env.LIVE15_RECORDER_HEALTH_PATH
    $healthBefore = Get-RecorderHealth $healthPath
    $droppedBefore = if ($null -eq $healthBefore) { [int64]0 } else { Get-HealthInteger $healthBefore 'kalshi_ws_queue_dropped' }
    $observedBefore = Get-ObservedAt $healthBefore

    & $NomadPath job run "-address=$NomadAddress" "-check-index=$($live.JobModifyIndex)" $Jobspec
    if ($LASTEXITCODE) { throw "Nomad job submission failed (exit=$LASTEXITCODE)." }
    $jobSubmitted = $true
    Wait-OneRecorderWriter
    Assert-HealthyRecorder $droppedBefore $observedBefore $healthPath
    $after = Get-LiveJob
    $receipt = Write-Receipt $previous $next $previousRuntime $nextRuntime $live.Version $after.Version
    Write-Output "RECORDER_DEPLOYMENT = PASS receipt=$receipt rollback='nomad job revert -address=$NomadAddress $JobId $($live.Version)'"
} catch {
    $message = "RECORDER_DEPLOYMENT_ERROR: $($_.Exception.Message)"
    if ($jobSubmitted -and $null -ne $rollbackVersion) {
        $message += " rollback='nomad job revert -address=$NomadAddress $JobId $rollbackVersion'"
    }
    [Console]::Error.WriteLine($message)
    exit 1
} finally {
    foreach ($name in $nomadVariableNames) { [Environment]::SetEnvironmentVariable("NOMAD_VAR_$name", $previousEnvironment[$name], 'Process') }
}