"""Repository-wide pytest safety hooks."""

from pathlib import Path

import pytest

from live15_quant.root_hygiene import RootHygieneError, resolve_pytest_cache_dir


def pytest_configure(config: pytest.Config) -> None:
    """Fail before collection when pytest would leave cache residue in the root."""

    cache = getattr(config, "cache", None)
    if cache is None:
        return
    try:
        resolve_pytest_cache_dir(Path(config.rootpath), Path(cache._cachedir))
    except RootHygieneError as error:
        raise pytest.UsageError(str(error)) from error
