from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from live15_quant.runtime_status import RuntimePidLease, RuntimeStatusError, atomic_json, read_json
from live15_quant.runtime_supervisor import RuntimeSupervisor


def configured(_tmp_path: Path):
    return object()


def test_supervisor_has_zero_registered_children(tmp_path: Path) -> None:
    supervisor = RuntimeSupervisor(configured(tmp_path), root=tmp_path)
    components = supervisor.tick()

    assert supervisor.children == {}
    assert components == {}
    source = Path("src/live15_quant/runtime_supervisor.py").read_text(encoding="utf-8")
    assert "RecorderProcessController" not in source
    assert "managed_control_center" not in source
    assert "managed_paper" not in source
    assert "managed_kalshi_sdk_shadow" not in source
    assert "PAPER_SHADOW_LOCAL_ONLY" not in source


def test_runtime_pid_lease_blocks_duplicate_and_recovers_stale(tmp_path: Path) -> None:
    path = tmp_path / "supervisor.pid"
    first = RuntimePidLease(path)
    first.acquire()
    with pytest.raises(RuntimeStatusError, match="already running"):
        RuntimePidLease(path).acquire()
    first.release()
    path.write_text("999999999\n", encoding="ascii")
    recovered = RuntimePidLease(path)
    recovered.acquire()
    recovered.release()


def test_atomic_status_retries_transient_windows_sharing_violation(tmp_path: Path) -> None:
    attempts = 0

    def replace(source: Path, target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("simulated sharing violation")
        os.replace(source, target)

    delays: list[float] = []
    path = tmp_path / "status.json"
    atomic_json(path, {"status": "RUNNING"}, replace=replace, sleep=delays.append)

    assert attempts == 3
    assert delays == [0.01, 0.02]
    assert read_json(path) == {"status": "RUNNING"}


def test_stop_preserves_zero_child_status(tmp_path: Path) -> None:
    supervisor = RuntimeSupervisor(configured(tmp_path), root=tmp_path, sleep=lambda _seconds: None)
    supervisor.stop_components()
    payload = json.loads((tmp_path / "runtime/runtime-supervisor-status.json").read_text())
    assert payload["status"] == "STOPPED"
    assert payload["components"] == {}
