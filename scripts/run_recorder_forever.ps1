param(
    [ValidateRange(1, 300)]
    [int]$RestartDelaySeconds = 5,
    [ValidateRange(1, 20)]
    [int]$MaxConsecutiveRestarts = 5,
    [ValidateRange(5, 900)]
    [int]$MaxRestartDelaySeconds = 60,
    [ValidateRange(30, 3600)]
    [int]$StableRunSeconds = 300
)

$ErrorActionPreference = "Stop"
$repository = Split-Path -Parent $PSScriptRoot
$recorder = Join-Path $repository ".venv\Scripts\live15-record.exe"

if (-not (Test-Path -LiteralPath $recorder -PathType Leaf)) {
    throw "Recorder entry point is missing. Reinstall the project into .venv."
}

Push-Location $repository
$consecutiveRestarts = 0
try {
    while ($true) {
        $startedAt = [DateTimeOffset]::UtcNow
        & $recorder
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) {
            break
        }
        $runSeconds = ([DateTimeOffset]::UtcNow - $startedAt).TotalSeconds
        if ($runSeconds -ge $StableRunSeconds) {
            $consecutiveRestarts = 0
        }
        $consecutiveRestarts += 1
        if ($consecutiveRestarts -gt $MaxConsecutiveRestarts) {
            throw "Recorder crash loop detected after $consecutiveRestarts non-zero exits; last exit code $exitCode."
        }
        $delay = [Math]::Min(
            $MaxRestartDelaySeconds,
            $RestartDelaySeconds * [Math]::Pow(2, $consecutiveRestarts - 1)
        )
        Write-Warning "Recorder exited with code $exitCode after $([Math]::Round($runSeconds, 1))s; restart $consecutiveRestarts/$MaxConsecutiveRestarts in $delay seconds."
        Start-Sleep -Seconds $delay
    }
}
finally {
    Pop-Location
}
