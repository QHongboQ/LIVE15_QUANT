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
$evidenceRootExisted = Test-Path -LiteralPath $evidenceRoot -PathType Container
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

function Get-ExpectedRootAcl([string]$Path, [string]$LocalServicePermission) {
    $acl = Get-Acl -LiteralPath $Path
    if ($acl.Owner -ne "BUILTIN\Administrators") {
        throw "staged ACL owner is not the trusted BUILTIN\Administrators principal: $Path"
    }
    if (-not $acl.AreAccessRulesProtected) {
        throw "staged ACL inherits entries instead of using the sealed DACL: $Path"
    }
    $expectedRules = @{
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
            throw "staged ACL is not the exact sealed DACL: $Path"
        }
    }
    $readback = @(& icacls $Path)
    if ($LASTEXITCODE -ne 0) { throw "unable to read staged ACL: $Path" }
    return [ordered]@{
        owner = $acl.Owner
        sddl = $acl.Sddl
        icacls = $readback
    }
}

function Assert-SealedDescendants([string]$Path) {
    $descendantReadback = @()
    foreach ($entry in @(Get-ChildItem -LiteralPath $Path -Force -Recurse)) {
        $acl = Get-Acl -LiteralPath $entry.FullName
        if ($acl.Owner -ne "BUILTIN\Administrators") {
            throw "staged child owner is not the trusted BUILTIN\Administrators principal: $($entry.FullName)"
        }
        if ($acl.AreAccessRulesProtected) {
            throw "staged child ACL blocks inheritance from its sealed root: $($entry.FullName)"
        }
        if (@($acl.Access | Where-Object { -not $_.IsInherited }).Count -ne 0) {
            throw "staged child ACL has an explicit access rule: $($entry.FullName)"
        }
        $descendantReadback += [ordered]@{
            path = $entry.FullName
            owner = $acl.Owner
            sddl = $acl.Sddl
        }
    }
    return $descendantReadback
}

function Seal-OrValidateAcl([string]$Path, [string]$LocalServicePermission, [bool]$Existed) {
    if (-not $Existed) {
        & icacls $Path /setowner "BUILTIN\Administrators" /T /C | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "failed to set trusted owner recursively for staged root: $Path" }
        & icacls $Path /inheritance:r /grant:r "BUILTIN\Administrators:(OI)(CI)F" "NT AUTHORITY\SYSTEM:(OI)(CI)F" "NT AUTHORITY\LOCAL SERVICE:(OI)(CI)$LocalServicePermission" "BUILTIN\Users:(OI)(CI)RX" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "failed to seal staged ACL: $Path" }
    }
    return [ordered]@{
        root = Get-ExpectedRootAcl $Path $LocalServicePermission
        descendants = Assert-SealedDescendants $Path
    }
}

$sourceJobspecSha256 = (Get-FileHash -LiteralPath $sourceJobspec -Algorithm SHA256).Hash.ToUpperInvariant()
Copy-OrVerify-SealedSource $sourceArtifact $stagedArtifactPath $artifactSha256
Copy-OrVerify-SealedSource $sourceJobspec $stagedJobspecPath $sourceJobspecSha256

$stagedArtifactSha256 = (Get-FileHash -LiteralPath $stagedArtifactPath -Algorithm SHA256).Hash.ToUpperInvariant()
$stagedJobspecSha256 = (Get-FileHash -LiteralPath $stagedJobspecPath -Algorithm SHA256).Hash.ToUpperInvariant()
if ($stagedArtifactSha256 -ne $artifactSha256 -or $stagedJobspecSha256 -ne $sourceJobspecSha256) {
    throw "post-copy shadow hash verification failed"
}

$artifactAcl = Seal-OrValidateAcl $artifactRoot "RX" $artifactRootExisted
$configAcl = Seal-OrValidateAcl $configRoot "RX" $configRootExisted
$logsAcl = Seal-OrValidateAcl $logsRoot "M" $logsRootExisted

if ($evidenceRootExisted) {
    $evidenceAcl = Seal-OrValidateAcl $evidenceRoot "RX" $true
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
        throw "sealed evidence root has no complete staging receipt"
    }
    try {
        $existingReceipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    }
    catch {
        throw "staging receipt is malformed; refusing to accept sealed staging"
    }
    if (
        $existingReceipt.artifact_sha256 -ne $stagedArtifactSha256 -or
        $existingReceipt.jobspec_sha256 -ne $stagedJobspecSha256 -or
        $existingReceipt.production -ne $false -or
        $existingReceipt.credentials_present -ne $false -or
        $null -eq $existingReceipt.acl_readback
    ) {
        throw "staging receipt does not match the sealed artifact safety boundary"
    }
    Write-Output "ALREADY_STAGED: $receiptPath"
    exit 0
}

# Keep this handle open while the evidence directory is sealed so the
# post-seal ACL read-back and copied hashes are recorded atomically in the receipt.
$receiptHandle = [IO.File]::Open($receiptPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
try {
    $evidenceAcl = Seal-OrValidateAcl $evidenceRoot "RX" $false

    $receipt = [ordered]@{
        schema_version = "1"
        staged_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        scope = "nomad_non_production_control_center_shadow"
        artifact_path = $stagedArtifactPath
        artifact_sha256 = $stagedArtifactSha256
        jobspec_path = $stagedJobspecPath
        jobspec_sha256 = $stagedJobspecSha256
        acl_readback = [ordered]@{
            artifact = $artifactAcl
            config = $configAcl
            logs = $logsAcl
            evidence = $evidenceAcl
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
