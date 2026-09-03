from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "manage_live15_canonical_runtime.ps1"


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
    assert "Move-Item -LiteralPath $CanonicalRuntime -Destination $backup" in source
    assert "Move-Item -LiteralPath $backup -Destination $CanonicalRuntime" in source


def test_archive_dependency_is_production_required_and_recorder_import_is_deferred() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    recorder = (ROOT / "src" / "live15_quant" / "native_recorder.py").read_text(encoding="utf-8")

    assert '"pyarrow==25.0.1"' in project.split("[project.optional-dependencies]")[0]
    assert "archive-prototype" not in project
    assert "if settings.enable_ws_archive:" in recorder
    assert "WS archive requires Production dependency pyarrow==25.0.1" in recorder
