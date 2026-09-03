import importlib.metadata
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "prepare_live15_production_runtime.ps1"
LOCK = ROOT / "requirements.production.lock"


def _lock() -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, version = line.partition("==")
        assert separator == "==" and name and version and ";" not in version
        result[canonicalize_name(name)] = version
    return result


def _powershell() -> str:
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if executable is None:
        pytest.fail("PowerShell is required to parse the Production runtime helper")
    return executable


def test_production_lock_contains_complete_runtime_closure() -> None:
    lock = _lock()
    for required in ("pyarrow", "httpx", "httpcore", "annotated-doc", "colorama"):
        assert canonicalize_name(required) in lock
    for dev_only in ("pytest", "pytest-asyncio", "ruff"):
        assert canonicalize_name(dev_only) not in lock

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    queue: list[str] = []
    for text in project["project"]["dependencies"]:
        requirement = Requirement(text)
        if requirement.marker is None or requirement.marker.evaluate():
            queue.append(canonicalize_name(requirement.name))

    visited: set[str] = set()
    while queue:
        name = queue.pop()
        if name in visited:
            continue
        visited.add(name)
        assert name in lock, f"Production dependency closure is not pinned: {name}"
        distribution = importlib.metadata.distribution(name)
        assert distribution.version == lock[name]
        for text in distribution.requires or []:
            requirement = Requirement(text)
            if requirement.marker is not None and not requirement.marker.evaluate():
                continue
            queue.append(canonicalize_name(requirement.name))


def test_runtime_preparer_builds_directly_at_immutable_revision_path() -> None:
    source = HELPER.read_text(encoding="utf-8")

    assert "CanonicalRuntimeRevisions" in source
    assert "runtime-py$ExpectedPython-$($lockSha.Substring(0, 12))" in source
    assert "live15-runtime-manifest.json" in source
    assert "--dry-run" in source and "--ignore-installed" in source and "--report" in source
    assert "Assert-ExactInventory" in source
    assert "pip==$ExpectedPip" in source
    assert "active_runtime_unchanged" in source
    assert "Move-Item" not in source
    assert "ControlCenterRuntime" not in source
    assert "Get-Consumers" not in source
    assert "MaintenanceConfirmed" not in source
    assert "Rollback" not in source


def test_runtime_preparer_does_not_install_live15_into_dependency_runtime() -> None:
    source = HELPER.read_text(encoding="utf-8")

    assert "pip', 'install', '--require-virtualenv', '-r', $ProductionLock" in source
    assert "--no-deps', $Repository" not in source
    assert "sys.path.insert(0" in source
    assert "live15_quant.native_recorder" in source
    assert "live15_quant.archive_arrow" in source


def test_runtime_preparer_parses_in_powershell() -> None:
    command = (
        "$tokens = $null; $errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{HELPER}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
