[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path "$PSScriptRoot\..\").Path,
    [string]$WinSWPath = "",
    [string]$KalshiApiKeyIdPath = $env:LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH,
    [string]$KalshiPrivateKeyPath = $env:LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$serviceId = "LIVE15ControlCenter"
$serviceRoot = Join-Path $ProjectRoot ".local-tools\winsw"
$sourceXml = Join-Path $ProjectRoot "deploy\windows\live15-control-center.xml"
$sourceWinSW = if ($WinSWPath) { $WinSWPath } else { Join-Path $serviceRoot "WinSW-x64.exe" }
$serviceExe = Join-Path $serviceRoot "$serviceId.exe"
$serviceXml = Join-Path $serviceRoot "$serviceId.xml"

if (-not (Test-Path -LiteralPath $sourceXml -PathType Leaf)) {
    throw "Control Center service configuration is missing."
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

function Resolve-CredentialPath([string]$Value, [string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name is not configured; set the existing host credential reference."
    }
    $resolved = (Resolve-Path -LiteralPath $Value -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "$Name does not resolve to a readable file."
    }
    return [IO.Path]::GetFullPath($resolved)
}

$keyIdPath = Resolve-CredentialPath $KalshiApiKeyIdPath "LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH"
$privateKeyPath = Resolve-CredentialPath $KalshiPrivateKeyPath "LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH"
$xml = Get-Content -LiteralPath $sourceXml -Raw
$xml = $xml.Replace(
    "%LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH%",
    [System.Security.SecurityElement]::Escape($keyIdPath)
)
$xml = $xml.Replace(
    "%LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH%",
    [System.Security.SecurityElement]::Escape($privateKeyPath)
)

New-Item -ItemType Directory -Force -Path $serviceRoot | Out-Null
Copy-Item -LiteralPath $sourceWinSW -Destination $serviceExe -Force
[IO.File]::WriteAllText($serviceXml, $xml, (New-Object Text.UTF8Encoding($false)))

if ($WhatIf) {
    Write-Output "READY: $serviceId package staged."
    exit 0
}

& $serviceExe install
if ($LASTEXITCODE -ne 0) {
    throw "WinSW failed to install $serviceId (exit code $LASTEXITCODE)."
}


