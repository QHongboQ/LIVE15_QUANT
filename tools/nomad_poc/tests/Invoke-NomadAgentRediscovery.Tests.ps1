$observer = Join-Path $PSScriptRoot '..\Invoke-NomadAgentRediscovery.ps1'
. $observer

Describe 'Assert-IsolatedNomadProcess' {
    It 'accepts only a process identified as nomad' {
        Assert-IsolatedNomadProcess -Process ([pscustomobject]@{ Id = 4646; ProcessName = 'nomad' })
    }

    It 'refuses a different process name before a stop action' {
        $threw = $false
        try {
            Assert-IsolatedNomadProcess -Process ([pscustomobject]@{ Id = 4646; ProcessName = 'pwsh' })
        }
        catch {
            $threw = $true
        }
        $threw | Should Be $true
    }
    It 'blocks the known-unsafe forced restart before touching the POC runtime' {
        $threw = $false
        try {
            & $observer -Run
        }
        catch {
            $threw = $_.Exception.Message -match 'Forced Nomad process restart is blocked'
        }

        $threw | Should Be $true
    }
}
