[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Repository,
    [string]$BasePython = 'C:\Program Files\LIVE15\Python313\python.exe',
    [string]$CanonicalRuntime = 'C:\Program Files\LIVE15\ControlCenterRuntime',
    [string]$RevisionRoot = 'C:\Program Files\LIVE15\CanonicalRuntimeRevisions',
    [string]$NomadAddress = 'http://127.0.0.1:4646',
    [switch]$Apply,
    [switch]$MaintenanceConfirmed,
    [switch]$Rollback,
    [string]$ReceiptPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ExpectedPython = '3.13.15'
$ProductionLock = Join-Path $Repository 'requirements.production.lock'
$DevOnly = @('httpx','pytest','pytest-asyncio','ruff')
$PythonVersionExpression = "import sys; print('.'.join(map(str, sys.version_info[:3])))"

function Invoke-RuntimePython([string]$Python, [string[]]$Arguments) {
    & $Python @Arguments | Out-Host
    if ($LASTEXITCODE) { throw "Runtime command failed: $($Arguments -join ' ')" }
}
function Get-PythonVersion([string]$Python) {
    $version = (& $Python -c $PythonVersionExpression | Out-String).Trim()
    if ($LASTEXITCODE) { throw "Python version query failed: $Python" }
    return $version
}
function Get-Inventory([string]$Python) {
    $lines = & $Python -m pip freeze --all
    if ($LASTEXITCODE) { throw 'pip freeze failed.' }
    return @($lines | Where-Object { $_ -match '^[A-Za-z0-9_.-]+==' } | Sort-Object)
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
function Get-Identity([string]$Runtime) {
    $python = Join-Path $Runtime 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Runtime python is missing: $python" }
    $version = Get-PythonVersion $python
    if ($LASTEXITCODE -or $version -ne $ExpectedPython) { throw "Runtime Python must be $ExpectedPython." }
    $inventory = Get-Inventory $python
    $identity = Get-DependencyIdentity $inventory
    return [pscustomobject]@{ RuntimeRoot=$Runtime; Python=$python; PythonVersion=$version; PythonSha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $python).Hash; DependencyInventory=$inventory; DependencyIdentity=$identity }
}
function Get-RunningAllocationCount($Allocations) {
    if ($null -eq $Allocations) { return 0 }
    $running = 0
    foreach ($allocation in @($Allocations)) {
        if ($null -eq $allocation) { continue }
        $statusProperty = $allocation.PSObject.Properties['ClientStatus']
        if ($null -eq $statusProperty) { throw 'Nomad allocation response lacks ClientStatus.' }
        if ([string]$statusProperty.Value -eq 'running') { $running++ }
    }
    return $running
}
function Get-Consumers {
    $result = @()
    foreach ($jobId in @('live15-recorder', 'live15-control-center')) {
        try {
            $allocations = Invoke-RestMethod -Uri "$NomadAddress/v1/job/$jobId/allocations" -Method Get -ErrorAction Stop
            $running = Get-RunningAllocationCount $allocations
        } catch {
            $running = -1
        }
        $result += [pscustomobject]@{ owner="Nomad:$jobId"; running_allocations=$running }
    }
    return $result
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
    $baseVersion = Get-PythonVersion $BasePython
    if ($baseVersion -ne $ExpectedPython) { throw "Base Python must be $ExpectedPython." }
    $current = Get-Identity $CanonicalRuntime
    $consumers = Get-Consumers
    $unsafeConsumers = @($consumers | Where-Object { $_.running_allocations -ne 0 })
    if ($unsafeConsumers.Count -gt 0 -and $Apply -and -not $MaintenanceConfirmed) {
        throw "REQUIRES_MAINTENANCE_BOUNDARY: stop/drain canonical-runtime consumers through their existing owners, then rerun with -MaintenanceConfirmed. Consumers=$($unsafeConsumers.owner -join ',')"
    }
    if ($Apply -and -not $MaintenanceConfirmed) { throw 'Apply requires explicit -MaintenanceConfirmed.' }
    if ($Rollback) {
        if (-not $ReceiptPath -or -not (Test-Path -LiteralPath $ReceiptPath)) { throw 'Rollback requires a promotion receipt.' }
        $receipt = Get-Content -Raw -LiteralPath $ReceiptPath | ConvertFrom-Json
        $candidate = [string]$receipt.previous.retained_revision_path
        if ([string]::IsNullOrWhiteSpace($candidate)) { throw 'Receipt lacks retained previous revision path.' }
    } else {
        $lockHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ProductionLock).Hash.Substring(0,12)
        $candidate = Join-Path $RevisionRoot "python-$ExpectedPython-$lockHash"
    }
    if (-not $Apply) {
        [pscustomobject]@{ mode='PREVIEW'; current=$current; candidate_path=$candidate; promotion_path=$CanonicalRuntime; consumers=$consumers; safety=if($unsafeConsumers.Count){'REQUIRES_MAINTENANCE_BOUNDARY'}else{'SAFE_NOW'}; mutation='NONE' } | ConvertTo-Json -Depth 5
        exit 0
    }
    $next = if ($Rollback) { Assert-Verified $candidate } else { New-Candidate (Split-Path -Leaf $candidate) }
    $consumersBeforeSwitch = Get-Consumers
    $unsafeBeforeSwitch = @($consumersBeforeSwitch | Where-Object { $_.running_allocations -ne 0 })
    if ($unsafeBeforeSwitch.Count -gt 0) {
        throw "REQUIRES_MAINTENANCE_BOUNDARY: canonical-runtime consumers are not stopped or have unknown state: $($unsafeBeforeSwitch.owner -join ',')"
    }
    $backup = Join-Path $RevisionRoot ("previous-" + $current.DependencyIdentity)
    if (Test-Path -LiteralPath $backup) { throw "Retained revision already exists; refusing to overwrite: $backup" }
    Move-Item -LiteralPath $CanonicalRuntime -Destination $backup
    $current = $current | Select-Object *, @{Name='retained_revision_path';Expression={$backup}}
    try {
        Move-Item -LiteralPath $next.RuntimeRoot -Destination $CanonicalRuntime
        $promoted = Assert-Verified $CanonicalRuntime
    } catch {
        $failed = Join-Path $RevisionRoot ("failed-" + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ'))
        if (Test-Path -LiteralPath $CanonicalRuntime) { Move-Item -LiteralPath $CanonicalRuntime -Destination $failed }
        Move-Item -LiteralPath $backup -Destination $CanonicalRuntime
        $restored = Assert-Verified $CanonicalRuntime
        if ($restored.DependencyIdentity -ne $current.DependencyIdentity) { throw 'Restored canonical runtime identity does not match the original.' }
        throw
    }
    $receipt = Write-Receipt $current $promoted
    Write-Output "CANONICAL_RUNTIME_PROMOTION = PASS receipt=$receipt"
} catch { [Console]::Error.WriteLine("CANONICAL_RUNTIME_ERROR: $($_.Exception.Message)"); exit 1 }
