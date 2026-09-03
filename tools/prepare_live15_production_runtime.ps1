[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Repository,
    [string]$BasePython = 'C:\Program Files\LIVE15\Python313\python.exe',
    [string]$RevisionRoot = 'C:\Program Files\LIVE15\CanonicalRuntimeRevisions',
    [string]$ProductionLock,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ExpectedPython = '3.13.15'
$ExpectedPip = '26.2.1'
$ManifestName = 'live15-runtime-manifest.json'
$PythonVersionExpression = "import sys; print('.'.join(map(str, sys.version_info[:3])))"

if ([string]::IsNullOrWhiteSpace($ProductionLock)) {
    $ProductionLock = Join-Path $Repository 'requirements.production.lock'
}

function Normalize-PackageName([string]$Name) {
    return (($Name.Trim().ToLowerInvariant()) -replace '[-_.]+', '-')
}

function Get-FileSha256([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($stream)
        return ([System.BitConverter]::ToString($hash)).Replace('-', '')
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

function Get-PythonVersion([string]$Python) {
    $version = (& $Python -c $PythonVersionExpression | Out-String).Trim()
    if ($LASTEXITCODE) { throw "Python version query failed: $Python" }
    return $version
}

function Invoke-RuntimePython([string]$Python, [string[]]$Arguments) {
    & $Python @Arguments | Out-Host
    if ($LASTEXITCODE) { throw "Runtime command failed: $($Arguments -join ' ')" }
}

function Get-LockInventory {
    $result = @()
    foreach ($raw in Get-Content -LiteralPath $ProductionLock) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        if ($line -notmatch '^(?<name>[A-Za-z0-9_.-]+)==(?<version>\S+)$') {
            throw "Production lock must contain exact NAME==VERSION pins only: $line"
        }
        $result += "$(Normalize-PackageName $Matches.name)==$($Matches.version)"
    }
    return @($result | Sort-Object -Unique)
}

function Get-InstalledInventory([string]$Python) {
    $raw = (& $Python -m pip list --format=json | Out-String).Trim()
    if ($LASTEXITCODE) { throw 'pip list failed.' }
    $items = $raw | ConvertFrom-Json
    $result = @()
    foreach ($item in @($items)) {
        $name = Normalize-PackageName ([string]$item.name)
        if ($name -in @('pip', 'setuptools', 'wheel', 'live15-quant')) { continue }
        $result += "$name==$([string]$item.version)"
    }
    return @($result | Sort-Object -Unique)
}

function Assert-ExactInventory([string[]]$Expected, [string[]]$Actual, [string]$Label) {
    $missing = @($Expected | Where-Object { $Actual -notcontains $_ })
    $extra = @($Actual | Where-Object { $Expected -notcontains $_ })
    if ($missing.Count -or $extra.Count) {
        throw "$Label dependency closure mismatch. Missing=[$($missing -join ', ')] Extra=[$($extra -join ', ')]"
    }
}

function Get-DependencyIdentity([string[]]$Inventory) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes(($Inventory -join "`n"))
        $hash = $sha.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hash)).Replace('-', '')
    } finally {
        $sha.Dispose()
    }
}

function Assert-ResolverClosure {
    $reportPath = [System.IO.Path]::GetTempFileName()
    try {
        & $BasePython -m pip install --dry-run --ignore-installed --quiet --report $reportPath -r $ProductionLock | Out-Host
        if ($LASTEXITCODE) { throw 'pip dry-run dependency resolution failed.' }
        $report = Get-Content -Raw -LiteralPath $reportPath | ConvertFrom-Json
        $resolved = @()
        foreach ($item in @($report.install)) {
            $name = Normalize-PackageName ([string]$item.metadata.name)
            $version = [string]$item.metadata.version
            $resolved += "$name==$version"
        }
        $expected = Get-LockInventory
        Assert-ExactInventory $expected @($resolved | Sort-Object -Unique) 'pip dry-run'
    } finally {
        Remove-Item -LiteralPath $reportPath -Force -ErrorAction SilentlyContinue
    }
}

function Get-RuntimeIdentity([string]$RuntimeRoot) {
    $python = Join-Path $RuntimeRoot 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Runtime python is missing: $python" }
    $version = Get-PythonVersion $python
    if ($version -ne $ExpectedPython) { throw "Runtime Python must be $ExpectedPython." }
    Invoke-RuntimePython $python @('-m', 'pip', 'check')
    $expected = Get-LockInventory
    $actual = Get-InstalledInventory $python
    Assert-ExactInventory $expected $actual 'Prepared runtime'
    $sourceRoot = Join-Path $Repository 'src'
    $importExpression = "import sys; sys.path.insert(0, r'$sourceRoot'); import live15_quant.native_recorder, live15_quant.archive_arrow, live15_quant.control_center, kalshi"
    Invoke-RuntimePython $python @('-I', '-c', $importExpression)
    return [pscustomobject]@{
        RuntimeRoot = $RuntimeRoot
        Python = $python
        PythonVersion = $version
        PythonSha256 = Get-FileSha256 $python
        ProductionLockSha256 = Get-FileSha256 $ProductionLock
        DependencyInventory = $actual
        DependencyIdentity = Get-DependencyIdentity $actual
    }
}

function Assert-PreparedRuntime([string]$RuntimeRoot) {
    $manifestPath = Join-Path $RuntimeRoot $ManifestName
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Existing runtime revision is incomplete and must not be used: $RuntimeRoot"
    }
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    $identity = Get-RuntimeIdentity $RuntimeRoot
    if ([string]$manifest.runtime_root -ne $identity.RuntimeRoot -or
        [string]$manifest.python -ne $identity.Python -or
        [string]$manifest.python_version -ne $identity.PythonVersion -or
        [string]$manifest.python_sha256 -ne $identity.PythonSha256 -or
        [string]$manifest.production_lock_sha256 -ne $identity.ProductionLockSha256 -or
        [string]$manifest.dependency_identity -ne $identity.DependencyIdentity) {
        throw "Runtime manifest does not match immutable revision contents: $RuntimeRoot"
    }
    return $identity
}

try {
    foreach ($path in @($Repository, $BasePython, $ProductionLock)) {
        if (-not (Test-Path -LiteralPath $path)) { throw "Required path is unavailable: $path" }
    }
    if ((Get-PythonVersion $BasePython) -ne $ExpectedPython) { throw "Base Python must be $ExpectedPython." }
    Assert-ResolverClosure
    $lockSha = Get-FileSha256 $ProductionLock
    $runtimeId = "runtime-py$ExpectedPython-$($lockSha.Substring(0, 12))"
    $runtimeRoot = Join-Path $RevisionRoot $runtimeId

    if (-not $Apply) {
        $state = if (Test-Path -LiteralPath $runtimeRoot -PathType Container) { 'EXISTS' } else { 'ABSENT' }
        [pscustomobject]@{
            mode = 'PREVIEW'
            runtime_id = $runtimeId
            runtime_root = $runtimeRoot
            production_lock_sha256 = $lockSha
            revision_state = $state
            active_runtime_unchanged = $true
            mutation = 'NONE'
        } | ConvertTo-Json -Depth 4
        exit 0
    }

    if (Test-Path -LiteralPath $runtimeRoot -PathType Container) {
        $identity = Assert-PreparedRuntime $runtimeRoot
        [pscustomobject]@{ mode='PREPARED_EXISTING'; identity=$identity; active_runtime_unchanged=$true } | ConvertTo-Json -Depth 5
        exit 0
    }

    New-Item -ItemType Directory -Force -Path $RevisionRoot | Out-Null
    Invoke-RuntimePython $BasePython @('-m', 'venv', $runtimeRoot)
    $python = Join-Path $runtimeRoot 'Scripts\python.exe'
    Invoke-RuntimePython $python @('-m', 'pip', 'install', "pip==$ExpectedPip")
    Invoke-RuntimePython $python @('-m', 'pip', 'install', '--require-virtualenv', '-r', $ProductionLock)
    $identity = Get-RuntimeIdentity $runtimeRoot
    $manifestPath = Join-Path $runtimeRoot $ManifestName
    [ordered]@{
        schema_version = 1
        runtime_id = $runtimeId
        runtime_root = $identity.RuntimeRoot
        python = $identity.Python
        python_version = $identity.PythonVersion
        python_sha256 = $identity.PythonSha256
        production_lock_sha256 = $identity.ProductionLockSha256
        dependency_identity = $identity.DependencyIdentity
        dependency_inventory = $identity.DependencyInventory
        prepared_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding utf8
    $verified = Assert-PreparedRuntime $runtimeRoot
    [pscustomobject]@{ mode='PREPARED_NEW'; identity=$verified; manifest=$manifestPath; active_runtime_unchanged=$true } | ConvertTo-Json -Depth 5
} catch {
    [Console]::Error.WriteLine("PRODUCTION_RUNTIME_PREP_ERROR: $($_.Exception.Message)")
    exit 1
}
