<#
.SYNOPSIS
Re-observes one Nomad lifecycle restart without treating a transient allocation
gap as a workload failure.

.DESCRIPTION
Nomad owns scheduling and allocation convergence.  This observer uses the
documented job/allocation/check interfaces and waits, within a fixed deadline,
for a running allocation with a passing Nomad service check.  It does not
implement a LIVE15 restart or recovery path.
#>
[CmdletBinding()]
param(
    [string]$Root = 'D:\LIVE15_NOMAD_POC',
    [string]$JobId = 'live15-nomad-generic',
    [string]$Address = 'http://127.0.0.1:4646',
    [ValidateRange(1, 300)][int]$TimeoutSeconds = 60,
    [ValidateRange(1, 30)][int]$PollSeconds = 2,
    [switch]$Run
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-NomadCommandSucceeded {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Operation,
        [Parameter(Mandatory)][int]$ExitCode
    )

    if ($ExitCode -ne 0) {
        throw "Nomad $Operation failed with exit_code=$ExitCode"
    }
}

function Select-NomadRunningAllocation {
    [CmdletBinding()]
    param([AllowEmptyCollection()][object[]]$Allocations)

    $candidates = @($Allocations |
            Where-Object { $_.ClientStatus -eq 'running' -and $_.DesiredStatus -eq 'run' } |
            Sort-Object -Property ModifyIndex -Descending |
            Select-Object -First 1)
    if ($candidates.Count -eq 0) {
        return $null
    }
    $candidates[0]
}

function Get-NomadAllocations {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$NomadExe,
        [Parameter(Mandatory)][string]$NomadAddress,
        [Parameter(Mandatory)][string]$NomadJobId
    )

    $job = & $NomadExe job status -json "-address=$NomadAddress" $NomadJobId | ConvertFrom-Json
    Assert-NomadCommandSucceeded -Operation 'job status' -ExitCode $LASTEXITCODE
    @($job.Allocations)
}

function Test-NomadCheckResults {
    [CmdletBinding()]
    param([Parameter(Mandatory)][object]$Checks)

    $checkValues = @($Checks.PSObject.Properties.Value)
    $checkValues.Count -gt 0 -and @($checkValues | Where-Object { $_.Status -ne 'success' }).Count -eq 0
}

function Test-NomadAllocationCheck {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$NomadExe,
        [Parameter(Mandatory)][string]$NomadAddress,
        [Parameter(Mandatory)][string]$AllocationId
    )

    $checks = & $NomadExe alloc checks -json "-address=$NomadAddress" $AllocationId | ConvertFrom-Json
    Assert-NomadCommandSucceeded -Operation 'alloc checks' -ExitCode $LASTEXITCODE
    Test-NomadCheckResults -Checks $checks
}

function Wait-NomadHealthyAllocation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][scriptblock]$ReadAllocations,
        [Parameter(Mandatory)][scriptblock]$CheckAllocation,
        [ValidateRange(1, 300)][int]$TimeoutSeconds = 60,
        [ValidateRange(1, 30)][int]$PollSeconds = 2,
        [scriptblock]$Pause = { param([int]$Seconds) Start-Sleep -Seconds $Seconds }
    )

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    $attempts = 0
    $emptySnapshots = 0
    $lastAllocationId = $null

    do {
        $attempts++
        $allocation = Select-NomadRunningAllocation -Allocations @(& $ReadAllocations)
        if ($null -eq $allocation) {
            $emptySnapshots++
        }
        else {
            $lastAllocationId = $allocation.ID
            try {
                if (& $CheckAllocation $allocation.ID) {
                    return [pscustomobject]@{
                        Allocation = $allocation
                        Attempts = $attempts
                        EmptySnapshots = $emptySnapshots
                    }
                }
            }
            catch {
                # The allocation may have changed between the job and check
                # reads; continue to observe Nomad's next authoritative state.
            }
        }

        if ([DateTimeOffset]::UtcNow -lt $deadline) {
            & $Pause $PollSeconds
        }
    } while ([DateTimeOffset]::UtcNow -lt $deadline)

    throw "Nomad did not report a healthy running allocation within $TimeoutSeconds seconds (attempts=$attempts empty_snapshots=$emptySnapshots last_allocation=$lastAllocationId)"
}

function Write-ReconciliationStatus {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Status,
        [Parameter(Mandatory)][string]$Phase,
        [Parameter(Mandatory)][string]$Result,
        [Parameter(Mandatory)][string]$Reason
    )

    @"
status=$Status
phase=$Phase
runner_pid=$PID
updated_utc=$([DateTimeOffset]::UtcNow.ToString('o'))
completed_phases=generic-crash-recovery,health-failure-recovery,workload-lifecycle-stop-start
next_phase=nomad-agent-restart-rediscovery
result=$Result
reason=$Reason
"@ | Set-Content -LiteralPath $Path -Encoding utf8
}

if ($Run) {
    $nomad = Join-Path $Root 'bin\nomad.exe'
    $poc = Join-Path $Root 'generic-poc'
    $lifecycleLog = Join-Path $poc 'logs\lifecycle-matrix.log'
    $reconciliationLog = Join-Path $poc 'logs\lifecycle-cycle5-reconciliation.log'
    $statusPath = Join-Path $Root 'state\validation-status.txt'
    $checkpointPath = Join-Path $Root 'state\nomad-migration-checkpoint.txt'
    $stamp = { [DateTimeOffset]::UtcNow.ToString('o') }

    try {
        "$(& $stamp) lifecycle-cycle5-reconciliation-request" | Add-Content -LiteralPath $reconciliationLog -Encoding utf8
        & $nomad job restart "-address=$Address" -yes $JobId | Out-Null
        Assert-NomadCommandSucceeded -Operation 'job restart' -ExitCode $LASTEXITCODE
        $observation = Wait-NomadHealthyAllocation `
            -ReadAllocations { Get-NomadAllocations -NomadExe $nomad -NomadAddress $Address -NomadJobId $JobId } `
            -CheckAllocation { param($AllocationId) Test-NomadAllocationCheck -NomadExe $nomad -NomadAddress $Address -AllocationId $AllocationId } `
            -TimeoutSeconds $TimeoutSeconds `
            -PollSeconds $PollSeconds
        $reason = "cycle5 reconciliation passed allocation=$($observation.Allocation.ID) attempts=$($observation.Attempts) empty_snapshots=$($observation.EmptySnapshots)"
        "$(& $stamp) lifecycle-cycle5-reconciliation-pass $reason" | Add-Content -LiteralPath $reconciliationLog -Encoding utf8
        "$(& $stamp) lifecycle-pass cycle=5-reconciled allocation=$($observation.Allocation.ID)" | Add-Content -LiteralPath $lifecycleLog -Encoding utf8
        Write-ReconciliationStatus -Path $statusPath -Status 'WAITING_FOR_CODEX' -Phase 'nomad-agent-restart-rediscovery' -Result 'WAITING_FOR_CODEX' -Reason $reason
        @"
stage=lifecycle-cycle5-reconciled
updated_utc=$(& $stamp)
poc_root=$poc
next_action=run checkpointed nomad-agent-restart-rediscovery validation
"@ | Set-Content -LiteralPath $checkpointPath -Encoding utf8
    }
    catch {
        $reason = $_.Exception.Message
        "$(& $stamp) lifecycle-cycle5-reconciliation-failed reason=$reason" | Add-Content -LiteralPath $reconciliationLog -Encoding utf8
        Write-ReconciliationStatus -Path $statusPath -Status 'FAILED' -Phase 'workload-lifecycle-stop-start' -Result 'FAIL' -Reason $reason
        throw
    }
}
