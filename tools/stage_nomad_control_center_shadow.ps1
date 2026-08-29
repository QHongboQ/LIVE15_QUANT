[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$StagingRoot = "D:\LIVE15_NOMAD_POC\control-center-shadow",
    [switch]$Run
)

$ErrorActionPreference = "Stop"
$expectedStagingRoot = "D:\LIVE15_NOMAD_POC\control-center-shadow"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$expectedProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$resolvedProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
if (-not [string]::Equals($resolvedProjectRoot, $expectedProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "source root must match the staging script checkout"
}
$resolvedStagingRoot = [IO.Path]::GetFullPath($StagingRoot)
if (-not [string]::Equals($resolvedStagingRoot, $expectedStagingRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "staging root must be the fixed isolated POC target"
}

$sourceRoot = Join-Path $expectedProjectRoot "deploy\nomad\control-center-shadow"
$sourceArtifact = Join-Path $sourceRoot "live15-control-center-shadow.ps1"
$sourceJobspec = Join-Path $sourceRoot "live15-control-center-shadow.nomad.hcl"
$artifactRoot = Join-Path $resolvedStagingRoot "artifact"
$configRoot = Join-Path $resolvedStagingRoot "config"
$logsRoot = Join-Path $resolvedStagingRoot "logs"
$evidenceRoot = Join-Path $resolvedStagingRoot "evidence"
$receiptPath = Join-Path $evidenceRoot "staging-receipt.json"
$stagedArtifactPath = Join-Path $artifactRoot "live15-control-center-shadow.ps1"
$stagedJobspecPath = Join-Path $configRoot "live15-control-center-shadow.nomad.hcl"

if (-not $Run) {
    Write-Output "READY: read-only sealed preflight for $resolvedStagingRoot"
    Write-Output "READY: run with -Run to validate the externally provisioned POC artifact"
    exit 0
}

foreach ($path in @(
    $sourceArtifact, $sourceJobspec, $artifactRoot, $configRoot, $logsRoot,
    $evidenceRoot, $receiptPath, $stagedArtifactPath, $stagedJobspecPath
)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "required externally provisioned sealed input is missing: $path"
    }
}

function Get-ExpectedAccessRules([string]$LocalServicePermission) {
    return @{
        "NT AUTHORITY\SYSTEM" = [Security.AccessControl.FileSystemRights]::FullControl
        "BUILTIN\Administrators" = [Security.AccessControl.FileSystemRights]::FullControl
        "NT AUTHORITY\LOCAL SERVICE" = if ($LocalServicePermission -eq "M") {
            [Security.AccessControl.FileSystemRights]::Modify -bor [Security.AccessControl.FileSystemRights]::Synchronize
        }
        else {
            [Security.AccessControl.FileSystemRights]::ReadAndExecute -bor [Security.AccessControl.FileSystemRights]::Synchronize
        }
        "BUILTIN\Users" = [Security.AccessControl.FileSystemRights]::ReadAndExecute -bor [Security.AccessControl.FileSystemRights]::Synchronize
    }
}

function Get-ValidatedRootAcl([string]$Path, [string]$LocalServicePermission) {
    $acl = Get-Acl -LiteralPath $Path
    if ($acl.Owner -ne "BUILTIN\Administrators") {
        throw "staged ACL owner is not the trusted BUILTIN\Administrators principal: $Path"
    }
    if (-not $acl.AreAccessRulesProtected) {
        throw "staged ACL inherits entries instead of using the externally sealed DACL: $Path"
    }
    $expectedRules = Get-ExpectedAccessRules $LocalServicePermission
    $accessRules = @($acl.Access)
    if ($accessRules.Count -ne $expectedRules.Count) {
        throw "staged ACL has an unexpected access rule: $Path"
    }
    foreach ($accessRule in $accessRules) {
        $identity = $accessRule.IdentityReference.Value
        if (
            -not $expectedRules.ContainsKey($identity) -or
            $accessRule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $accessRule.IsInherited -or
            $accessRule.InheritanceFlags -ne ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit) -or
            $accessRule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None -or
            $accessRule.FileSystemRights -ne $expectedRules[$identity]
        ) {
            throw "staged ACL is not the exact externally sealed DACL: $Path"
        }
    }
    return [ordered]@{
        path = $Path
        owner = $acl.Owner
        sddl = $acl.Sddl
    }
}

function Assert-SealedDescendants([string]$Path, [string]$LocalServicePermission) {
    $expectedRules = Get-ExpectedAccessRules $LocalServicePermission
    $descendants = @()
    foreach ($entry in @(Get-ChildItem -LiteralPath $Path -Force -Recurse)) {
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "staged tree has a reparse point: $($entry.FullName)"
        }
        $acl = Get-Acl -LiteralPath $entry.FullName
        if ($acl.Owner -ne "BUILTIN\Administrators") {
            throw "staged child owner is not the trusted BUILTIN\Administrators principal: $($entry.FullName)"
        }
        if ($acl.AreAccessRulesProtected) {
            throw "staged child ACL blocks inheritance from its sealed root: $($entry.FullName)"
        }
        $accessRules = @($acl.Access)
        if ($accessRules.Count -ne $expectedRules.Count) {
            throw "staged child ACL has an unexpected access rule: $($entry.FullName)"
        }
        foreach ($accessRule in $accessRules) {
            $identity = $accessRule.IdentityReference.Value
            if (
                -not $expectedRules.ContainsKey($identity) -or
                $accessRule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
                -not $accessRule.IsInherited -or
                $accessRule.FileSystemRights -ne $expectedRules[$identity]
            ) {
                throw "staged child ACL is not the exact inherited sealed policy: $($entry.FullName)"
            }
        }
        $descendants += [ordered]@{
            path = $entry.FullName
            owner = $acl.Owner
            sddl = $acl.Sddl
        }
    }
    return $descendants
}

$sourceArtifactSha256 = (Get-FileHash -LiteralPath $sourceArtifact -Algorithm SHA256).Hash.ToUpperInvariant()
$sourceJobspecSha256 = (Get-FileHash -LiteralPath $sourceJobspec -Algorithm SHA256).Hash.ToUpperInvariant()
$stagedArtifactSha256 = (Get-FileHash -LiteralPath $stagedArtifactPath -Algorithm SHA256).Hash.ToUpperInvariant()
$stagedJobspecSha256 = (Get-FileHash -LiteralPath $stagedJobspecPath -Algorithm SHA256).Hash.ToUpperInvariant()
$stagedJobspecText = Get-Content -LiteralPath $stagedJobspecPath -Raw
if (
    $sourceArtifactSha256 -ne $stagedArtifactSha256 -or
    $sourceJobspecSha256 -ne $stagedJobspecSha256 -or
    $stagedJobspecText -notmatch [regex]::Escape($sourceArtifactSha256) -or
    $stagedJobspecText -match "LIVE15_KALSHI_PRODUCTION|LIVE15_QUANT"
) {
    throw "sealed artifact or jobspec does not match the read-only safety boundary"
}

try {
    $historicalReceipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
}
catch {
    throw "historical staging receipt is malformed"
}
if (
    $historicalReceipt.artifact_sha256 -ne $stagedArtifactSha256 -or
    $historicalReceipt.jobspec_sha256 -ne $stagedJobspecSha256 -or
    $historicalReceipt.production -ne $false -or
    $historicalReceipt.credentials_present -ne $false
) {
    throw "historical staging receipt does not match the sealed artifact safety boundary"
}

$topAcl = Get-ValidatedRootAcl $resolvedStagingRoot "RX"
$artifactAcl = Get-ValidatedRootAcl $artifactRoot "RX"
$configAcl = Get-ValidatedRootAcl $configRoot "RX"
$logsAcl = Get-ValidatedRootAcl $logsRoot "M"
$evidenceAcl = Get-ValidatedRootAcl $evidenceRoot "RX"
$validated = [ordered]@{
    scope = "nomad_non_production_control_center_shadow"
    mode = "read_only_validation"
    artifact_sha256 = $stagedArtifactSha256
    jobspec_sha256 = $stagedJobspecSha256
    top = $topAcl
    artifact = [ordered]@{ root = $artifactAcl; descendants = Assert-SealedDescendants $artifactRoot "RX" }
    config = [ordered]@{ root = $configAcl; descendants = Assert-SealedDescendants $configRoot "RX" }
    logs = [ordered]@{ root = $logsAcl; descendants = Assert-SealedDescendants $logsRoot "M" }
    evidence = [ordered]@{ root = $evidenceAcl; descendants = Assert-SealedDescendants $evidenceRoot "RX" }
    historical_receipt = $receiptPath
}
Write-Output "VALIDATED_STAGED: $receiptPath"
$validated | ConvertTo-Json -Depth 8
