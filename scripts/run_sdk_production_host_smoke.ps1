[CmdletBinding()]
param(
    [ValidateRange(60, 120)]
    [int]$DurationSeconds = 75
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$launcher = Join-Path $PSScriptRoot 'run_sdk_production_host_smoke.py'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "LIVE15 Python 3.13 virtual environment is unavailable"
}

# The managed Production child receives these exact variables through
# production_runtime_environment().  A manually invoked isolated smoke must
# hydrate its process from the same persistent external configuration, never
# from an in-repository credential copy or a Demo fallback.
foreach ($name in @(
    'LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH',
    'LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH'
)) {
    if (-not [Environment]::GetEnvironmentVariable($name, 'Process')) {
        $value = [Environment]::GetEnvironmentVariable($name, 'User')
        if (-not $value) {
            $value = [Environment]::GetEnvironmentVariable($name, 'Machine')
        }
        if (-not $value) {
            throw "Production credential path variable is missing: $name"
        }
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

& $python -u $launcher --duration-seconds $DurationSeconds
$exitCode = $LASTEXITCODE
Write-Output "PROCESS_EXIT_CODE=$exitCode"
exit $exitCode
