[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path "$PSScriptRoot\..\").Path,
    [string]$WinSWPath = "",
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$serviceId = "LIVE15ControlCenter"
$serviceRoot = Join-Path $ProjectRoot ".local-tools\winsw"
$serviceExe = if ($WinSWPath) { $WinSWPath } else { Join-Path $serviceRoot "$serviceId.exe" }

if (-not (Test-Path -LiteralPath $serviceExe -PathType Leaf)) {
    throw "Control Center WinSW executable is missing."
}
if ($WhatIf) {
    Write-Output "READY: $serviceId package is removable."
    exit 0
}

& $serviceExe uninstall
if ($LASTEXITCODE -ne 0) {
    throw "WinSW failed to uninstall $serviceId (exit code $LASTEXITCODE)."
}


