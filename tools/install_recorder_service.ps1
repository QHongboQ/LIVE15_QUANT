[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$WinSWPath = "",
    [string]$KalshiApiKeyIdPath = $env:LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH,
    [string]$KalshiPrivateKeyPath = $env:LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$serviceId = "LIVE15Recorder"
$serviceRoot = Join-Path $ProjectRoot ".local-tools\winsw"
$sourceXml = Join-Path $ProjectRoot "deploy\windows\live15-recorder.xml"
$sourceWinSW = if ($WinSWPath) { $WinSWPath } else { Join-Path $serviceRoot "WinSW-x64.exe" }
$serviceExe = Join-Path $serviceRoot "$serviceId.exe"
$serviceXml = Join-Path $serviceRoot "$serviceId.xml"

if (-not (Test-Path -LiteralPath $sourceXml -PathType Leaf)) {
    throw "Recorder service configuration is missing: $sourceXml"
}
if (-not (Test-Path -LiteralPath $sourceWinSW -PathType Leaf)) {
    throw "Verified WinSW binary is missing; run tools\bootstrap_winsw.ps1 first."
}
if ([string]::IsNullOrWhiteSpace($KalshiApiKeyIdPath) -or
    [string]::IsNullOrWhiteSpace($KalshiPrivateKeyPath)) {
    throw "Recorder credential references are not configured; set both Kalshi production path variables."
}
if (-not (Test-Path -LiteralPath $KalshiApiKeyIdPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $KalshiPrivateKeyPath -PathType Leaf)) {
    throw "Recorder credential reference does not resolve to readable files."
}

$xml = Get-Content -LiteralPath $sourceXml -Raw
$xml = $xml.Replace(
    "%LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH%",
    [System.Security.SecurityElement]::Escape(([IO.Path]::GetFullPath($KalshiApiKeyIdPath)))
)
$xml = $xml.Replace(
    "%LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH%",
    [System.Security.SecurityElement]::Escape(([IO.Path]::GetFullPath($KalshiPrivateKeyPath)))
)

New-Item -ItemType Directory -Force -Path $serviceRoot | Out-Null
Copy-Item -LiteralPath $sourceWinSW -Destination $serviceExe -Force
[IO.File]::WriteAllText($serviceXml, $xml, (New-Object Text.UTF8Encoding($false)))

if ($WhatIf) {
    Write-Output "READY: $serviceId package staged at $serviceRoot"
    exit 0
}

& $serviceExe install
if ($LASTEXITCODE -ne 0) {
    throw "WinSW failed to install $serviceId (exit code $LASTEXITCODE)."
}
