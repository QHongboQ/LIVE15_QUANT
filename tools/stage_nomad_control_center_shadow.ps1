[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$StagingRoot = "D:\LIVE15_NOMAD_POC\control-center-shadow",
    [switch]$Run
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$pocRoot = "D:\LIVE15_NOMAD_POC"
$resolvedStagingRoot = [IO.Path]::GetFullPath($StagingRoot)
if (-not $resolvedStagingRoot.StartsWith("$pocRoot\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "staging root must remain under $pocRoot"
}

$sourceRoot = Join-Path $ProjectRoot "deploy\nomad\control-center-shadow"
$sourceArtifact = Join-Path $sourceRoot "live15-control-center-shadow.ps1"
$sourceJobspec = Join-Path $sourceRoot "live15-control-center-shadow.nomad.hcl"
foreach ($path in @($sourceArtifact, $sourceJobspec)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "required shadow source is missing: $path"
    }
}

$artifactSha256 = (Get-FileHash -LiteralPath $sourceArtifact -Algorithm SHA256).Hash.ToUpperInvariant()
$jobspecText = Get-Content -LiteralPath $sourceJobspec -Raw
if ($jobspecText -notmatch [regex]::Escape($artifactSha256)) {
    throw "jobspec does not pin the source artifact SHA-256"
}
if ($jobspecText -match "LIVE15_KALSHI_PRODUCTION|LIVE15_QUANT") {
    throw "jobspec references a prohibited Production source"
}

$artifactRoot = Join-Path $resolvedStagingRoot "artifact"
$configRoot = Join-Path $resolvedStagingRoot "config"
$logsRoot = Join-Path $resolvedStagingRoot "logs"
$receiptPath = Join-Path $configRoot "staging-receipt.json"

if (-not $Run) {
    Write-Output "READY: source artifact SHA-256 $artifactSha256"
    Write-Output "READY: sealed staging target $resolvedStagingRoot"
    Write-Output "READY: run with -Run to stage only this non-Production shadow artifact"
    exit 0
}

foreach ($path in @($artifactRoot, $configRoot, $logsRoot)) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}

Copy-Item -LiteralPath $sourceArtifact -Destination (Join-Path $artifactRoot "live15-control-center-shadow.ps1") -Force
Copy-Item -LiteralPath $sourceJobspec -Destination (Join-Path $configRoot "live15-control-center-shadow.nomad.hcl") -Force

$receipt = [ordered]@{
    schema_version = "1"
    staged_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    scope = "nomad_non_production_control_center_shadow"
    artifact_path = (Join-Path $artifactRoot "live15-control-center-shadow.ps1")
    artifact_sha256 = $artifactSha256
    jobspec_path = (Join-Path $configRoot "live15-control-center-shadow.nomad.hcl")
    jobspec_sha256 = (Get-FileHash -LiteralPath $sourceJobspec -Algorithm SHA256).Hash.ToUpperInvariant()
    production = $false
    credentials_present = $false
    recorder_started = $false
    execution_enabled = $false
}
[IO.File]::WriteAllText($receiptPath, ($receipt | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))

& icacls $artifactRoot /inheritance:r /grant:r "BUILTIN\Administrators:(OI)(CI)F" "NT AUTHORITY\SYSTEM:(OI)(CI)F" "NT AUTHORITY\LOCAL SERVICE:(OI)(CI)RX" "BUILTIN\Users:(OI)(CI)RX"
if ($LASTEXITCODE -ne 0) { throw "failed to seal shadow artifact ACL" }
& icacls $configRoot /inheritance:r /grant:r "BUILTIN\Administrators:(OI)(CI)F" "NT AUTHORITY\SYSTEM:(OI)(CI)F" "NT AUTHORITY\LOCAL SERVICE:(OI)(CI)RX" "BUILTIN\Users:(OI)(CI)RX"
if ($LASTEXITCODE -ne 0) { throw "failed to seal shadow configuration ACL" }
& icacls $logsRoot /inheritance:r /grant:r "BUILTIN\Administrators:(OI)(CI)F" "NT AUTHORITY\SYSTEM:(OI)(CI)F" "NT AUTHORITY\LOCAL SERVICE:(OI)(CI)M" "BUILTIN\Users:(OI)(CI)RX"
if ($LASTEXITCODE -ne 0) { throw "failed to seal shadow log ACL" }

Write-Output "STAGED: $receiptPath"
