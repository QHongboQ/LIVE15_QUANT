[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$CandidatePath,
    [string]$NomadPath = "D:\\LIVE15_NOMAD_POC\\bin\\nomad.exe",
    [string]$NomadAddress = "http://127.0.0.1:4646",
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ControlCenterJobId = "live15-control-center"
$CredentialVariableNames = @(
    "NOMAD_VAR_kalshi_production_api_key_id_path",
    "NOMAD_VAR_kalshi_production_private_key_path"
)
$previousEnvironment = @{}
foreach ($name in $CredentialVariableNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

function Assert-LocalNomadAddress {
    param([Parameter(Mandatory)][string]$Address)

    $uri = [Uri]$Address
    if ($uri.Scheme -ne "http" -or $uri.Host -ne "127.0.0.1" -or $uri.Port -ne 4646) {
        throw "NomadAddress must be the local Nomad API http://127.0.0.1:4646."
    }
}

function Assert-ControlCenterCandidate {
    param([Parameter(Mandatory)][string]$Path)

    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "CandidatePath must resolve to a jobspec file."
    }

    $jobspec = Get-Content -Raw -LiteralPath $resolved
    if ($jobspec -notmatch '(?m)^\s*job\s+"live15-control-center"\s*\{') {
        throw "Candidate jobspec must target only live15-control-center."
    }
    if ($jobspec -match 'live15-recorder') {
        throw "Candidate jobspec must not target live15-recorder."
    }
    return $resolved
}

function Resolve-ReadableLeafPath {
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$Label
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Label is missing from the live ControlCenter job."
    }
    $resolved = (Resolve-Path -LiteralPath $Value -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "$Label must resolve to an existing readable file."
    }

    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $resolved,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
    }
    catch {
        throw "$Label must resolve to an existing readable file."
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
    return $resolved
}

function Get-LiveControlCenterJob {
    param([Parameter(Mandatory)][string]$Address)

    $job = Invoke-RestMethod -Method Get -Uri "$Address/v1/job/$ControlCenterJobId"
    if ($job.ID -ne $ControlCenterJobId) {
        throw "Nomad API did not return the expected ControlCenter job."
    }
    if ($null -eq $job.JobModifyIndex) {
        throw "Live ControlCenter job has no JobModifyIndex."
    }

    $groups = @($job.TaskGroups | Where-Object { $_.Name -eq "control-center" })
    if ($groups.Count -ne 1) {
        throw "Live ControlCenter job does not have exactly one control-center group."
    }
    $tasks = @($groups[0].Tasks | Where-Object { $_.Name -eq "control-center" })
    if ($tasks.Count -ne 1) {
        throw "Live ControlCenter job does not have exactly one control-center task."
    }

    return [pscustomobject]@{
        JobModifyIndex = [int64]$job.JobModifyIndex
        KeyIdPath = $tasks[0].Env.LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH
        PrivateKeyPath = $tasks[0].Env.LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH
    }
}

$exitCode = 0
try {
    Assert-LocalNomadAddress -Address $NomadAddress
    $candidate = Assert-ControlCenterCandidate -Path $CandidatePath
    $nomad = (Resolve-Path -LiteralPath $NomadPath -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $nomad -PathType Leaf)) {
        throw "NomadPath must resolve to the approved Nomad executable."
    }

    $plannedJob = Get-LiveControlCenterJob -Address $NomadAddress
    $keyIdPath = Resolve-ReadableLeafPath `
        -Value $plannedJob.KeyIdPath `
        -Label "LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH"
    $privateKeyPath = Resolve-ReadableLeafPath `
        -Value $plannedJob.PrivateKeyPath `
        -Label "LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH"

    [Environment]::SetEnvironmentVariable(
        "NOMAD_VAR_kalshi_production_api_key_id_path", $keyIdPath, "Process"
    )
    [Environment]::SetEnvironmentVariable(
        "NOMAD_VAR_kalshi_production_private_key_path", $privateKeyPath, "Process"
    )

    & $nomad job validate "-address=$NomadAddress" $candidate
    if ($LASTEXITCODE -ne 0) {
        throw "Nomad job validate failed (exit=$LASTEXITCODE)."
    }

    & $nomad job plan "-address=$NomadAddress" $candidate
    $planExitCode = $LASTEXITCODE
    if ($planExitCode -notin @(0, 1)) {
        throw "Nomad job plan failed (exit=$planExitCode)."
    }

    if (-not $Apply) {
        if ($planExitCode -eq 0) {
            Write-Output "PLAN_ONLY = PASS (no changes)"
        }
        else {
            Write-Output "PLAN_ONLY = PASS (changes present)"
        }
    }
    else {
        $currentJob = Get-LiveControlCenterJob -Address $NomadAddress
        if ($currentJob.JobModifyIndex -ne $plannedJob.JobModifyIndex) {
            throw "ControlCenter JobModifyIndex changed after planning; re-plan is required."
        }

        & $nomad job run "-address=$NomadAddress" "-check-index=$($plannedJob.JobModifyIndex)" $candidate
        if ($LASTEXITCODE -ne 0) {
            throw "Nomad job run failed (exit=$LASTEXITCODE)."
        }
        Write-Output "CONTROLCENTER_ROLLOUT = PASS"
    }
}
catch {
    $exitCode = 1
    [Console]::Error.WriteLine("DEPLOYMENT_ERROR: $($_.Exception.Message)")
}
finally {
    foreach ($name in $CredentialVariableNames) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], "Process")
    }
}

if ($exitCode -ne 0) {
    exit $exitCode
}
