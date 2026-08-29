$observer = Join-Path $PSScriptRoot '..\Invoke-NomadLifecycleCycle.ps1'
. $observer

Describe 'Wait-NomadHealthyAllocation' {
    It 'waits through a transient empty allocation snapshot before accepting health' {
        $state = [pscustomobject]@{ Reads = 0; Checks = 0 }
        $reader = {
            $state.Reads++
            if ($state.Reads -eq 1) {
                return @()
            }
            return @([pscustomobject]@{
                    ID = 'allocation-after-convergence'
                    ClientStatus = 'running'
                    DesiredStatus = 'run'
                    ModifyIndex = 2
                })
        }
        $checker = {
            param($AllocationId)
            $state.Checks++
            $AllocationId | Should Be 'allocation-after-convergence'
            $true
        }

        $result = Wait-NomadHealthyAllocation -ReadAllocations $reader -CheckAllocation $checker -TimeoutSeconds 2 -PollSeconds 1 -Pause { param($Seconds) }

        $result.Allocation.ID | Should Be 'allocation-after-convergence'
        $result.EmptySnapshots | Should Be 1
        $state.Checks | Should Be 1
    }

    It 'never returns a running allocation whose desired state is stop' {
        $allocation = Select-NomadRunningAllocation -Allocations @(
            [pscustomobject]@{ ID = 'stopping'; ClientStatus = 'running'; DesiredStatus = 'stop'; ModifyIndex = 2 },
            [pscustomobject]@{ ID = 'active'; ClientStatus = 'running'; DesiredStatus = 'run'; ModifyIndex = 1 }
        )

        $allocation.ID | Should Be 'active'
    }
    It 'requires the configured consecutive healthy observations before returning' {
        $state = [pscustomobject]@{ Checks = 0 }
        $reader = {
            @([pscustomobject]@{
                    ID = 'stable-allocation'
                    ClientStatus = 'running'
                    DesiredStatus = 'run'
                    ModifyIndex = 2
                })
        }
        $checker = {
            param($AllocationId)
            $state.Checks++
            $true
        }

        $result = Wait-NomadHealthyAllocation -ReadAllocations $reader -CheckAllocation $checker -TimeoutSeconds 2 -PollSeconds 1 -RequiredConsecutiveHealthyObservations 2 -Pause { param($Seconds) }

        $result.ConsecutiveHealthyObservations | Should Be 2
        $state.Checks | Should Be 2
    }
    It 'fails closed when a native Nomad command reports error' {
        $threw = $false
        try {
            Assert-NomadCommandSucceeded -Operation 'job restart' -ExitCode 1
        }
        catch {
            $threw = $true
        }
        $threw | Should Be $true
    }

    It 'rejects a healthy check when Nomad reports a task that is not running' {
        $allocation = [pscustomobject]@{
            ClientStatus = 'running'
            DesiredStatus = 'run'
            TaskStates = [pscustomobject]@{
                fixture = [pscustomobject]@{ State = 'pending' }
            }
        }

        (Test-NomadAllocationRunning -Allocation $allocation) | Should Be $false
    }
    It 'rejects an allocation when any Nomad service check is not successful' {
        $checks = [pscustomobject]@{
            passing = [pscustomobject]@{ Status = 'success' }
            failing = [pscustomobject]@{ Status = 'failure' }
        }

        (Test-NomadCheckResults -Checks $checks) | Should Be $false
    }

}
