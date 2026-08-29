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
$expectedProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$resolvedProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
if (-not [string]::Equals($resolvedProjectRoot, $expectedProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "source root must match the staging script checkout"
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
$evidenceRoot = Join-Path $resolvedStagingRoot "evidence"
$receiptPath = Join-Path $evidenceRoot "staging-receipt.json"
$stagedArtifactPath = Join-Path $artifactRoot "live15-control-center-shadow.ps1"
$stagedJobspecPath = Join-Path $configRoot "live15-control-center-shadow.nomad.hcl"

if (-not $Run) {
    Write-Output "READY: source artifact SHA-256 $artifactSha256"
    Write-Output "READY: sealed staging target $resolvedStagingRoot"
    Write-Output "READY: run with -Run to stage only this non-Production shadow artifact"
    exit 0
}

$artifactRootExisted = Test-Path -LiteralPath $artifactRoot -PathType Container
$configRootExisted = Test-Path -LiteralPath $configRoot -PathType Container
$logsRootExisted = Test-Path -LiteralPath $logsRoot -PathType Container
foreach ($path in @($artifactRoot, $configRoot, $logsRoot, $evidenceRoot)) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}

function Copy-OrVerify-SealedSource([string]$Source, [string]$Destination, [string]$ExpectedHash) {
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        $existingHash = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToUpperInvariant()
        if ($existingHash -ne $ExpectedHash) {
            throw "sealed staged file differs from the checked source: $Destination"
        }
        return
    }
    Copy-Item -LiteralPath $Source -Destination $Destination
}

$sourceJobspecSha256 = (Get-FileHash -LiteralPath $sourceJobspec -Algorithm SHA256).Hash.ToUpperInvariant()
Copy-OrVerify-SealedSource $sourceArtifact $stagedArtifactPath $artifactSha256
Copy-OrVerify-SealedSource $sourceJobspec $stagedJobspecPath $sourceJobspecSha256

$stagedArtifactSha256 = (Get-FileHash -LiteralPath $stagedArtifactPath -Algorithm SHA256).Hash.ToUpperInvariant()
$stagedJobspecSha256 = (Get-FileHash -LiteralPath $stagedJobspecPath -Algorithm SHA256).Hash.ToUpperInvariant()
if ($stagedArtifactSha256 -ne $artifactSha256 -or $stagedJobspecSha256 -ne $sourceJobspecSha256) {
    throw "post-copy shadow hash verification failed"
}

if (Test-Path -LiteralPath $receiptPath -PathType Leaf) {
    Write-Output "ALREADY_STAGED: $receiptPath"
    exit 0
}

# Keep this handle open while the evidence directory is sealed so the
# post-seal ACL read-back and copied hashes are recorded atomically in the receipt.
$receiptHandle = [IO.File]::Open($receiptPath, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
try {
    if (-not $artifactRootExisted) {
        & icacls $artifactRoot /inheritance:r /grant:r "BUILTIN\Administrators:(OI)(CI)F" "NT AUTHORITY\SYSTEM:(OI)(CI)F" "NT AUTHORITY\LOCAL SERVICE:(OI)(CI)RX" "BUILTIN\Users:(OI)(CI)RX"
        if ($LASTEXITCODE -ne 0) { throw "failed to seal shadow artifact ACL" }
    }
    if (-not $configRootExisted) {
        & icacls $configRoot /inheritance:r /grant:r "BUILTIN\Administrators:(OI)(CI)F" "NT AUTHORITY\SYSTEM:(OI)(CI)F" "NT AUTHORITY\LOCAL SERVICE:(OI)(CI)RX" "BUILTIN\Users:(OI)(CI)RX"
        if ($LASTEXITCODE -ne 0) { throw "failed to seal shadow configuration ACL" }
    }
    if (-not $logsRootExisted) {
        & icacls $logsRoot /inheritance:r /grant:r "BUILTIN\Administrators:(OI)(CI)F" "NT AUTHORITY\SYSTEM:(OI)(CI)F" "NT AUTHORITY\LOCAL SERVICE:(OI)(CI)M" "BUILTIN\Users:(OI)(CI)RX"
        if ($LASTEXITCODE -ne 0) { throw "failed to seal shadow log ACL" }
    }
    & icacls $evidenceRoot /inheritance:r /grant:r "BUILTIN\Administrators:(OI)(CI)F" "NT AUTHORITY\SYSTEM:(OI)(CI)F" "NT AUTHORITY\LOCAL SERVICE:(OI)(CI)RX" "BUILTIN\Users:(OI)(CI)RX"
    if ($LASTEXITCODE -ne 0) { throw "failed to seal shadow evidence ACL" }

    $receipt = [ordered]@{
        schema_version = "1"
        staged_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        scope = "nomad_non_production_control_center_shadow"
        artifact_path = $stagedArtifactPath
        artifact_sha256 = $stagedArtifactSha256
        jobspec_path = $stagedJobspecPath
        jobspec_sha256 = $stagedJobspecSha256
        acl_readback = [ordered]@{
            artifact = @(& icacls $artifactRoot)
            config = @(& icacls $configRoot)
            logs = @(& icacls $logsRoot)
            evidence = @(& icacls $evidenceRoot)
        }
        production = $false
        credentials_present = $false
        recorder_started = $false
        execution_enabled = $false
    }
    $receiptBytes = [Text.UTF8Encoding]::new($false).GetBytes(($receipt | ConvertTo-Json -Depth 6))
    $receiptHandle.Write($receiptBytes, 0, $receiptBytes.Length)
    $receiptHandle.Flush($true)
}
finally {
    $receiptHandle.Dispose()
}

Write-Output "STAGED: $receiptPath"
