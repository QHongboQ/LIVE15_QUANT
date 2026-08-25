from __future__ import annotations

import json
from pathlib import Path

from live15_quant.kalshi_gateway.smoke_result import write_smoke_result_atomic


def test_smoke_result_is_fsynced_then_atomically_published(tmp_path: Path) -> None:
    destination = tmp_path / "sdk-production-host-smoke-result.json"
    calls: list[tuple[str, object]] = []

    def fsync(descriptor: int) -> None:
        calls.append(("fsync", descriptor))

    def replace(source: str | Path, target: str | Path) -> None:
        calls.append(("replace", Path(source)))
        assert Path(source).exists()
        assert not destination.exists()
        Path(source).replace(target)

    write_smoke_result_atomic(
        destination,
        {"status": "PASSED", "last_error": None, "rows_added": 10},
        replace=replace,
        fsync=fsync,
    )

    assert [name for name, _value in calls] == ["fsync", "replace"]
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "last_error": None,
        "rows_added": 10,
        "status": "PASSED",
    }
    assert not list(tmp_path.glob(".*.tmp"))


def test_failed_smoke_result_is_published_for_postmortem(tmp_path: Path) -> None:
    destination = tmp_path / "sdk-production-host-smoke-result.json"
    payload = {
        "status": "FAILED",
        "last_error": "RuntimeError: sanitized failure",
        "checkpoint_after": None,
    }

    write_smoke_result_atomic(destination, payload)

    assert json.loads(destination.read_text(encoding="utf-8")) == payload
