[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$serviceExe = Join-Path $ProjectRoot ".local-tools\winsw\LIVE15Recorder.exe"
if (-not (Test-Path -LiteralPath $serviceExe -PathType Leaf)) {
    throw "LIVE15Recorder WinSW executable is missing."
}
if ($WhatIf) {
    Write-Output "READY: would uninstall LIVE15Recorder"
    exit 0
}

& $serviceExe uninstall
if ($LASTEXITCODE -ne 0) {
    throw "WinSW failed to uninstall LIVE15Recorder (exit code $LASTEXITCODE)."
}
