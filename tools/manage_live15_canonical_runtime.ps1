[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Repository,
    [string]$BasePython = 'C:\Program Files\LIVE15\Python313\python.exe',
    [string]$CanonicalRuntime = 'C:\Program Files\LIVE15\ControlCenterRuntime',
    [string]$RevisionRoot = 'C:\Program Files\LIVE15\CanonicalRuntimeRevisions',
    [switch]$Apply,
    [switch]$Rollback,
    [string]$ReceiptPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ExpectedPython = '3.13.15'
$ProductionLock = Join-Path $Repository 'requirements.production.lock'
$DevOnly = @('httpx','pytest','pytest-asyncio','ruff')

function Invoke-RuntimePython([string]$Python, [string[]]$Arguments) {
    & $Python @Arguments | Out-Host
    if ($LASTEXITCODE) { throw "Runtime command failed: $($Arguments -join ' ')" }
}
function Get-Inventory([string]$Python) {
    $lines = & $Python -m pip freeze --all
    if ($LASTEXITCODE) { throw 'pip freeze failed.' }
    return @($lines | Where-Object { $_ -match '^[A-Za-z0-9_.-]+==' } | Sort-Object)
}
function Get-Identity([string]$Runtime) {
    $python = Join-Path $Runtime 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Runtime python is missing: $python" }
    $version = (& $python -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' | Out-String).Trim()
    if ($LASTEXITCODE -or $version -ne $ExpectedPython) { throw "Runtime Python must be $ExpectedPython." }
    $inventory = Get-Inventory $python
    $identity = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes(($inventory -join "`n"))))
    return [pscustomobject]@{ RuntimeRoot=$Runtime; Python=$python; PythonVersion=$version; PythonSha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $python).Hash; DependencyInventory=$inventory; DependencyIdentity=$identity }
}
function Assert-Verified([string]$Runtime) {
    $identity = Get-Identity $Runtime
    $expected = Get-Content -LiteralPath $ProductionLock | Where-Object { $_ -and -not $_.StartsWith('#') }
    foreach ($package in $expected) { if ($identity.DependencyInventory -notcontains $package) { throw "Production dependency missing: $package" } }
    foreach ($name in $DevOnly) { if ($identity.DependencyInventory | Where-Object { $_ -like "$name==*" }) { throw "DEV_ONLY dependency present: $name" } }
    Invoke-RuntimePython $identity.Python @('-m','pip','check')
    Invoke-RuntimePython $identity.Python @('-c','import live15_quant, live15_quant.native_recorder, live15_quant.archive_arrow, live15_quant.control_center, kalshi')
    return $identity
}
function New-Candidate([string]$RuntimeId) {
    $candidate = Join-Path $RevisionRoot $RuntimeId
    if (Test-Path -LiteralPath $candidate) { return Assert-Verified $candidate }
    New-Item -ItemType Directory -Force -Path $RevisionRoot | Out-Null
    Invoke-RuntimePython $BasePython @('-m','venv',$candidate)
    $python = Join-Path $candidate 'Scripts\python.exe'
    Invoke-RuntimePython $python @('-m','pip','install','--require-virtualenv','-r',$ProductionLock)
    Invoke-RuntimePython $python @('-m','pip','install','--require-virtualenv','--no-deps',$Repository)
    return Assert-Verified $candidate
}
function Write-Receipt($Previous, $Next) {
    $dir = Join-Path $RevisionRoot 'receipts'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $path = Join-Path $dir ("canonical-runtime-$($Next.DependencyIdentity).json")
    [ordered]@{ previous=$Previous; next=$Next; promoted_at_utc=(Get-Date).ToUniversalTime().ToString('o') } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $path -Encoding utf8
    return $path
}
try {
    foreach ($path in @($Repository,$BasePython,$ProductionLock)) { if (-not (Test-Path -LiteralPath $path)) { throw "Required path is unavailable: $path" } }
    $baseVersion = (& $BasePython -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' | Out-String).Trim()
    if ($baseVersion -ne $ExpectedPython) { throw "Base Python must be $ExpectedPython." }
    $current = Get-Identity $CanonicalRuntime
    if ($Rollback) {
        if (-not $ReceiptPath -or -not (Test-Path -LiteralPath $ReceiptPath)) { throw 'Rollback requires a promotion receipt.' }
        $receipt = Get-Content -Raw -LiteralPath $ReceiptPath | ConvertFrom-Json
        $candidate = [string]$receipt.previous.RuntimeRoot
    } else {
        $lockHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ProductionLock).Hash.Substring(0,12)
        $candidate = Join-Path $RevisionRoot "python-$ExpectedPython-$lockHash"
    }
    if (-not $Apply) { [pscustomobject]@{ mode='PREVIEW'; current=$current; candidate_path=$candidate; promotion_path=$CanonicalRuntime; mutation='NONE' } | ConvertTo-Json -Depth 5; exit 0 }
    $next = if ($Rollback) { Assert-Verified $candidate } else { New-Candidate (Split-Path -Leaf $candidate) }
    $backup = Join-Path $RevisionRoot ("previous-" + $current.DependencyIdentity)
    if (-not (Test-Path -LiteralPath $backup)) { Move-Item -LiteralPath $CanonicalRuntime -Destination $backup }
    try { Move-Item -LiteralPath $next.RuntimeRoot -Destination $CanonicalRuntime } catch { Move-Item -LiteralPath $backup -Destination $CanonicalRuntime; throw }
    $promoted = Assert-Verified $CanonicalRuntime
    $receipt = Write-Receipt $current $promoted
    Write-Output "CANONICAL_RUNTIME_PROMOTION = PASS receipt=$receipt"
} catch { [Console]::Error.WriteLine("CANONICAL_RUNTIME_ERROR: $($_.Exception.Message)"); exit 1 }
