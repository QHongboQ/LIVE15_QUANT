[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ResearchRoot,
    [Parameter(Mandatory = $true)]
    [string]$Python313
)

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$targetRoot = [System.IO.Path]::GetFullPath($ResearchRoot)
$protectedRoots = @(
    $repoRoot,
    (Join-Path $repoRoot "data"),
    (Join-Path $repoRoot "runtime")
)
foreach ($protectedRoot in $protectedRoots) {
    if ($targetRoot.StartsWith($protectedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "ResearchRoot must be outside the repository and production paths."
    }
}
if (-not (Test-Path -LiteralPath $Python313 -PathType Leaf)) {
    throw "Python313 executable was not found."
}
& $Python313 -c "import sys; assert sys.version_info[:2] == (3, 13), sys.version"
if ($LASTEXITCODE -ne 0) { throw "Python313 must be Python 3.13." }

New-Item -ItemType Directory -Force -Path $targetRoot | Out-Null
$venv = Join-Path $targetRoot ".venv"
& $Python313 -m venv $venv
if ($LASTEXITCODE -ne 0) { throw "Failed to create isolated research virtual environment." }
$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install --require-virtualenv -r (Join-Path $repoRoot "requirements.research.lock")
if ($LASTEXITCODE -ne 0) { throw "Failed to install locked research dependencies." }
& $python -m pip install --require-virtualenv --no-deps $repoRoot
if ($LASTEXITCODE -ne 0) { throw "Failed to install LIVE15 into isolated research environment." }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw "Research environment dependency check failed." }
Write-Output "RESEARCH_ENV_READY=$venv"
