from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from live15_quant.runtime_status import RuntimePidLease, RuntimeStatusError, atomic_json, read_json
from live15_quant.runtime_supervisor import RuntimeSupervisor


class FakeProcess:
    next_pid = 30_000

    def __init__(self, command: list[str]) -> None:
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.command = command
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


def configured(_tmp_path: Path):
    return object()


def test_supervisor_never_owns_recorder_or_control_center(tmp_path: Path) -> None:
    launched: list[FakeProcess] = []

    def popen(command, **_kwargs):
        process = FakeProcess(command)
        launched.append(process)
        return process

    supervisor = RuntimeSupervisor(configured(tmp_path), root=tmp_path, popen=popen)
    components = supervisor.tick()

    assert launched == []
    assert set(components) == {"kalshi_sdk_ws_shadow", "paper_forward"}
    assert components["kalshi_sdk_ws_shadow"]["status"] == "ON_DEMAND"
    assert components["paper_forward"]["status"] == "PAUSED_BY_DESIGN"
    source = Path("src/live15_quant/runtime_supervisor.py").read_text(encoding="utf-8")
    assert "RecorderProcessController" not in source
    assert "managed_control_center" not in source


def test_supervisor_restarts_only_explicit_automatic_child(tmp_path: Path) -> None:
    launched: list[FakeProcess] = []

    def popen(command, **_kwargs):
        process = FakeProcess(command)
        launched.append(process)
        return process

    supervisor = RuntimeSupervisor(configured(tmp_path), root=tmp_path, popen=popen)
    child = supervisor.children["kalshi_sdk_ws_shadow"]
    child.automatic = True

    components = supervisor.tick()

    assert components["kalshi_sdk_ws_shadow"]["status"] == "STARTING"
    assert [process.command[-1] for process in launched] == [
        "live15_quant.managed_kalshi_sdk_shadow"
    ]
    assert all("recorder" not in process.command[-1] for process in launched)
    assert all("control_center" not in process.command[-1] for process in launched)


def test_supervisor_pins_legacy_shadow_lifecycle_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    def popen(_command, **kwargs):
        captured.update(kwargs["env"])
        return FakeProcess([])

    monkeypatch.setenv("LIVE15_KALSHI_SDK_SHADOW_LIFECYCLE_OWNER", "nomad")
    supervisor = RuntimeSupervisor(configured(tmp_path), root=tmp_path, popen=popen)
    supervisor._launch(supervisor.children["kalshi_sdk_ws_shadow"])

    assert captured["LIVE15_KALSHI_SDK_SHADOW_LIFECYCLE_OWNER"] == "runtime_supervisor"


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


def test_stop_does_not_issue_recorder_process_command(tmp_path: Path) -> None:
    supervisor = RuntimeSupervisor(configured(tmp_path), root=tmp_path, sleep=lambda _seconds: None)
    supervisor.stop_components()
    payload = json.loads((tmp_path / "runtime/runtime-supervisor-status.json").read_text())
    assert payload["status"] == "STOPPED"
    assert set(payload["components"]) == {"kalshi_sdk_ws_shadow", "paper_forward"}
