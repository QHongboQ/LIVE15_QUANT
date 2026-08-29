from pathlib import Path

import pytest

from live15_quant.root_hygiene import RootHygieneError, resolve_pytest_cache_dir


def test_pytest_cache_defaults_to_explicitly_approved_runtime_tmp_subtree() -> None:
    root = Path(r"D:\LIVE15_QUANT")
    resolved = resolve_pytest_cache_dir(root, "runtime/tmp/pytest-cache")
    assert resolved == (root / "runtime" / "tmp" / "pytest-cache").resolve()


def test_external_cache_root_is_allowed() -> None:
    root = Path(r"D:\LIVE15_QUANT")
    external = Path(r"D:\LIVE15_TEST_TEMP\pytest-cache")
    assert resolve_pytest_cache_dir(root, external) == external.resolve()


@pytest.mark.parametrize("candidate", (".pytest_cache", "runtime/cache", "../pytest-cache"))
def test_unapproved_project_relative_cache_root_fails_closed(candidate: str) -> None:
    with pytest.raises(RootHygieneError, match="temporary artifact path"):
        resolve_pytest_cache_dir(Path(r"D:\LIVE15_QUANT"), candidate)
