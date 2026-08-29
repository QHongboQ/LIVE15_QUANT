<#!
.SYNOPSIS
Proves isolated Nomad agent restart and durable workload rediscovery.
#>
[CmdletBinding()]
param(
    [string]$Root = 'D:\LIVE15_NOMAD_POC',
    [ValidateRange(1, 10)][int]$Cycles = 5,
    [ValidateRange(15, 300)][int]$TimeoutSeconds = 90,
    [switch]$Run,
    [switch]$AllowUnsafeForceRestart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$execute = $Run
$requestedRoot = $Root
$requestedCycles = $Cycles
$requestedTimeoutSeconds = $TimeoutSeconds
if ($execute -and -not $AllowUnsafeForceRestart) {
    throw 'Forced Nomad process restart is blocked after raw_exec reattachment/HTTP.sys orphan evidence. Use a supported Windows-service boundary for the next controlled validation.'
}
. (Join-Path $PSScriptRoot 'Invoke-NomadLifecycleCycle.ps1') -Run:$false
$Root = $requestedRoot
$Cycles = $requestedCycles
$TimeoutSeconds = $requestedTimeoutSeconds

function Assert-IsolatedNomadProcess {
    [CmdletBinding()]
    param([Parameter(Mandatory)][object]$Process)

    if ($Process.ProcessName -ne 'nomad') {
        throw "Refusing to stop non-Nomad process pid=$($Process.Id) name=$($Process.ProcessName)"
    }
}

function Get-IsolatedNomadProcess {
    [CmdletBinding()]
    param([int]$Port = 4646)

    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
    if ($listeners.Count -ne 1) {
        throw "Expected exactly one isolated Nomad listener on port $Port; found $($listeners.Count)"
    }
    $process = Get-Process -Id $listeners[0].OwningProcess -ErrorAction Stop
    Assert-IsolatedNomadProcess -Process $process
    $process
}

function Wait-IsolatedNomadListener {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][int]$ExpectedProcessId,
        [ValidateRange(15, 300)][int]$TimeoutSeconds = 90
    )

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $process = Get-IsolatedNomadProcess
            if ($process.Id -eq $ExpectedProcessId) {
                return $process
            }
        }
        catch {
            # The old listener may still be exiting or the new one may not yet bind.
        }
        Start-Sleep -Seconds 1
    } while ([DateTimeOffset]::UtcNow -lt $deadline)

    throw "Nomad agent pid=$ExpectedProcessId did not become the isolated port-4646 listener within $TimeoutSeconds seconds"
}

if ($execute) {
    $nomad = Join-Path $Root 'bin\nomad.exe'
    $poc = Join-Path $Root 'generic-poc'
    $agentConfig = Join-Path $poc 'nomad-agent.hcl'
    $log = Join-Path $poc 'logs\agent-restart-rediscovery-matrix.log'
    $checkpoint = Join-Path $Root 'state\nomad-migration-checkpoint.txt'

    for ($cycle = 1; $cycle -le $Cycles; $cycle++) {
        $old = Get-IsolatedNomadProcess
        $stamp = [DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssffffZ')
        $stdout = Join-Path $poc "logs\agent-restart-$cycle-$stamp.stdout.log"
        $stderr = Join-Path $poc "logs\agent-restart-$cycle-$stamp.stderr.log"
        "$([DateTimeOffset]::UtcNow.ToString('o')) agent-restart-request cycle=$cycle old_pid=$($old.Id)" | Add-Content -LiteralPath $log -Encoding utf8
        Stop-Process -Id $old.Id -Force
        Wait-Process -Id $old.Id -ErrorAction SilentlyContinue
        $new = Start-Process -FilePath $nomad -ArgumentList @('agent', "-config=$agentConfig") -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
        Wait-IsolatedNomadListener -ExpectedProcessId $new.Id -TimeoutSeconds $TimeoutSeconds | Out-Null
        $observation = Wait-NomadHealthyAllocation `
            -ReadAllocations { try { Get-NomadAllocations -NomadExe $nomad -NomadAddress 'http://127.0.0.1:4646' -NomadJobId 'live15-nomad-generic' } catch { @() } } `
            -CheckAllocation { param($AllocationId) Test-NomadAllocationCheck -NomadExe $nomad -NomadAddress 'http://127.0.0.1:4646' -AllocationId $AllocationId } `
            -TimeoutSeconds $TimeoutSeconds `
            -PollSeconds 5 `
            -RequiredConsecutiveHealthyObservations 2
        "$([DateTimeOffset]::UtcNow.ToString('o')) agent-restart-pass cycle=$cycle old_pid=$($old.Id) new_pid=$($new.Id) allocation=$($observation.Allocation.ID) attempts=$($observation.Attempts) empty_snapshots=$($observation.EmptySnapshots)" | Add-Content -LiteralPath $log -Encoding utf8
    }

    @"
stage=nomad-agent-restart-rediscovery-complete
updated_utc=$([DateTimeOffset]::UtcNow.ToString('o'))
poc_root=$poc
agent_restart_rediscovery=PASS cycles=$Cycles evidence=$log
next_action=prepare native update-revert validation
"@ | Set-Content -LiteralPath $checkpoint -Encoding utf8
}
