[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$WinSWPath = "",
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$serviceId = "LIVE15RuntimeSupervisor"
$serviceRoot = Join-Path $ProjectRoot ".local-tools\winsw"
$sourceXml = Join-Path $ProjectRoot "deploy\windows\live15-runtime-supervisor.xml"
$sourceWinSW = if ($WinSWPath) { $WinSWPath } else { Join-Path $serviceRoot "WinSW-x64.exe" }
$serviceExe = Join-Path $serviceRoot "$serviceId.exe"
$serviceXml = Join-Path $serviceRoot "$serviceId.xml"

if (-not (Test-Path -LiteralPath $sourceXml -PathType Leaf)) {
    throw "Runtime Supervisor service configuration is missing."
}
if (-not (Test-Path -LiteralPath $sourceWinSW -PathType Leaf)) {
    throw "Verified WinSW binary is missing; run tools\bootstrap_winsw.ps1 first."
}

$metadata = Get-Content -LiteralPath (Join-Path $ProjectRoot "deploy\windows\winsw-v2.12.0.json") -Raw |
    ConvertFrom-Json
$actualHash = (Get-FileHash -LiteralPath $sourceWinSW -Algorithm SHA256).Hash.ToUpperInvariant()
if ($actualHash -ne $metadata.sha256.ToUpperInvariant()) {
    throw "WinSW SHA256 mismatch; refusing to stage the service."
}

New-Item -ItemType Directory -Force -Path $serviceRoot | Out-Null
Copy-Item -LiteralPath $sourceWinSW -Destination $serviceExe -Force
Copy-Item -LiteralPath $sourceXml -Destination $serviceXml -Force

if ($WhatIf) {
    Write-Output "READY: $serviceId package staged."
    exit 0
}

& $serviceExe install
if ($LASTEXITCODE -ne 0) {
    throw "WinSW failed to install $serviceId (exit code $LASTEXITCODE)."
}
