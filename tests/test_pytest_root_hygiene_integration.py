import subprocess
import sys
from pathlib import Path

import pytest


def test_pytest_startup_rejects_an_unapproved_cache_override() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden = root / ".pytest_cache"
    assert not forbidden.exists()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-o",
            "cache_dir=.pytest_cache",
            "tests/test_root_hygiene.py",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "temporary artifact path" in result.stderr
    assert not forbidden.exists()
