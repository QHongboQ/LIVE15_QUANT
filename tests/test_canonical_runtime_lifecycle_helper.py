import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "manage_live15_canonical_runtime.ps1"


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("Windows PowerShell is unavailable on this developer platform")
    return executable


def _python_executable() -> Path:
    executable = Path(sys.executable)
    assert executable.is_file(), f"Test Python executable is unavailable: {executable}"
    return executable


def test_native_python_version_query_receives_valid_expression() -> None:
    """Execute the helper's exact expression through Windows PowerShell."""
    wrapper = ROOT / ".pytest-version-query.ps1"
    try:
        wrapper.write_text(
            """
param([string]$Helper, [string]$Python)
$source = Get-Content -Raw -LiteralPath $Helper
$start = $source.IndexOf('$PythonVersionExpression')
$end = $source.IndexOf('function Get-Inventory')
if ($start -lt 0 -or $end -le $start) { throw 'version helper source not found' }
Invoke-Expression $source.Substring($start, $end - $start)
$version = Get-PythonVersion $Python
if ($version -notmatch '^\\d+\\.\\d+\\.\\d+$') { throw "invalid version: $version" }
Write-Output $version
""".strip(),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                _powershell(),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(wrapper),
                "-Helper",
                str(HELPER),
                "-Python",
                str(_python_executable()),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        wrapper.unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "3.13.15", result.stdout
    assert "..join" not in result.stderr + result.stdout


def test_windows_powershell_51_compatibility_primitives() -> None:
    """Run pure helper primitives under Windows PowerShell 5.1 when available."""
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell 5.1 is unavailable on this developer platform")
    wrapper = ROOT / ".pytest-powershell51-compatibility.ps1"
    revision_root = ROOT / ".pytest-powershell51-revisions"
    try:
        wrapper.write_text(
            """
param([string]$Helper, [string]$Python, [string]$RevisionRoot)
$Repository = (Get-Location).Path
$source = Get-Content -Raw -LiteralPath $Helper
$start = $source.IndexOf('$ExpectedPython')
$end = $source.IndexOf("try {`n    foreach")
if ($start -lt 0 -or $end -le $start) { throw 'helper primitives not found' }
Invoke-Expression $source.Substring($start, $end - $start)
$version = Get-PythonVersion $Python
$inventory = @('a==1', 'b==2')
$identity = Get-DependencyIdentity $inventory
$RevisionRoot = $RevisionRoot
$previous = [pscustomobject]@{
    retained_revision_path = 'previous-A'
    DependencyIdentity = $identity
}
$next = [pscustomobject]@{
    RuntimeRoot = 'candidate-B'
    DependencyIdentity = $identity
}
$receipt = Write-Receipt $previous $next
$json = Get-Content -Raw -LiteralPath $receipt | ConvertFrom-Json
[ordered]@{
    version = $version
    identity = $identity
    receipt_path = $receipt
    retained = $json.previous.retained_revision_path
    next_root = $json.next.RuntimeRoot
} | ConvertTo-Json -Depth 5
""".strip(),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(wrapper),
                "-Helper",
                str(HELPER),
                "-Python",
                sys.executable,
                "-RevisionRoot",
                str(revision_root),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        wrapper.unlink(missing_ok=True)
        if revision_root.exists():
            shutil.rmtree(revision_root)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["version"] == "3.13.15"
    assert payload["identity"] == hashlib.sha256(b"a==1\nb==2").hexdigest().upper()
    assert payload["retained"] == "previous-A"
    assert payload["next_root"] == "candidate-B"


def test_production_lock_promotes_pyarrow_and_excludes_dev_only_packages() -> None:
    packages = (ROOT / "requirements.production.lock").read_text(encoding="utf-8").splitlines()

    assert "pyarrow==25.0.1" in packages
    for name in ("httpx", "pytest", "pytest-asyncio", "ruff"):
        assert not any(item.startswith(f"{name}==") for item in packages)


def test_lifecycle_helper_has_preview_apply_and_receipt_bound_rollback_contracts() -> None:
    source = HELPER.read_text(encoding="utf-8")

    assert "mode='PREVIEW'" in source
    assert "mutation='NONE'" in source
    assert "[switch]$Apply" in source
    assert "Rollback requires a promotion receipt." in source
    assert "previous=$Previous; next=$Next" in source
    assert "retained_revision_path" in source
    assert "previous.retained_revision_path" in source
    assert "-MaintenanceConfirmed" in source
    assert "REQUIRES_MAINTENANCE_BOUNDARY" in source
    assert "live15-recorder" in source and "live15-control-center" in source
    assert "stop/drain canonical-runtime consumers through their existing owners" in source
    assert "Move-Item -LiteralPath $CanonicalRuntime -Destination $backup" in source
    assert "Move-Item -LiteralPath $backup -Destination $CanonicalRuntime" in source
    assert (
        "$PythonVersionExpression = \"import sys; print('.'.join(map(str, sys.version_info[:3])))\""
        in source
    )
    assert source.count("Get-PythonVersion") >= 3
    assert 'print(".".join' not in source


def test_archive_dependency_is_production_required_and_recorder_import_is_deferred() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    recorder = (ROOT / "src" / "live15_quant" / "native_recorder.py").read_text(encoding="utf-8")

    assert '"pyarrow==25.0.1"' in project.split("[project.optional-dependencies]")[0]
    assert "archive-prototype" not in project
    assert "if settings.enable_ws_archive:" in recorder
    assert "WS archive requires Production dependency pyarrow==25.0.1" in recorder


def test_canonical_runtime_is_one_authority_with_versioned_revisions() -> None:
    source = HELPER.read_text(encoding="utf-8")

    assert "CanonicalRuntimeRevisions" in source
    assert "CanonicalRuntime" in source
    assert "previous-" in source
    assert "pip uninstall" not in source
