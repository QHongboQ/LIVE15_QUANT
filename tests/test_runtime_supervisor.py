from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from live15_quant.managed_control_center import _monitor_runtime
from live15_quant.recorder_control import (
    ManagedRecorderState,
    RecorderControlStatus,
)
from live15_quant.runtime_status import (
    RuntimePidLease,
    RuntimeStatusError,
    atomic_json,
    read_json,
)
from live15_quant.runtime_supervisor import RuntimeSupervisor

NOW = datetime(2026, 8, 24, tzinfo=UTC)


class FakeController:
    def __init__(self, state: ManagedRecorderState = ManagedRecorderState.RUNNING) -> None:
        self.state = state
        self.resume_calls = 0
        self.pause_calls = 0

    def status(self) -> RecorderControlStatus:
        pid = os.getpid() if self.state is ManagedRecorderState.RUNNING else None
        return RecorderControlStatus(self.state, pid, self.state.value)

    def resume(self) -> RecorderControlStatus:
        self.resume_calls += 1
        self.state = ManagedRecorderState.RUNNING
        return self.status()

    def pause(self) -> RecorderControlStatus:
        self.pause_calls += 1
        self.state = ManagedRecorderState.PAUSED
        return self.status()


class FakeProcess:
    next_pid = 30_000

    def __init__(self, command: list[str]) -> None:
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.command = command
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


def configured(tmp_path: Path) -> SimpleNamespace:
    data = tmp_path / "data"
    data.mkdir()
    health = data / "health.json"
    health.write_text(
        json.dumps(
            {
                "status": "healthy",
                "started_at": NOW.isoformat(),
                "observed_at": datetime.now(UTC).isoformat(),
                "fatal_task": None,
                "fatal_error_type": None,
            }
        ),
        encoding="utf-8",
    )
    return SimpleNamespace(
        recorder_health_path=health,
        recorder_control_path=data / "recorder-control.json",
        ui_heartbeat_stale_seconds=90.0,
    )


def test_supervisor_starts_control_center_then_paper_after_healthy_recorder(
    tmp_path: Path,
) -> None:
    launched: list[FakeProcess] = []

    def popen(command, **_kwargs):
        process = FakeProcess(command)
        launched.append(process)
        return process

    supervisor = RuntimeSupervisor(
        configured(tmp_path),
        root=tmp_path,
        controller=FakeController(),
        popen=popen,
    )

    components = supervisor.tick()

    assert components["recorder"]["status"] == "HEALTHY"
    assert [process.command[-1] for process in launched] == [
        "live15_quant.managed_control_center",
        "live15_quant.managed_paper",
    ]
    assert components["paper_forward"]["status"] == "STARTING"
    assert components["demo_first_fill"]["status"] == "DISABLED"
    status = read_json(tmp_path / "runtime" / "runtime-supervisor-status.json")
    assert status is not None
    assert status["expected_mode"] == "RUNTIME_SUPERVISOR_NO_TRADING"


def test_paper_waits_for_healthy_recorder(tmp_path: Path) -> None:
    launched: list[FakeProcess] = []

    def popen(command, **_kwargs):
        process = FakeProcess(command)
        launched.append(process)
        return process

    settings = configured(tmp_path)
    settings.recorder_health_path.write_text(
        json.dumps(
            {
                "status": "degraded",
                "observed_at": datetime.now(UTC).isoformat(),
                "fatal_task": None,
                "fatal_error_type": None,
            }
        ),
        encoding="utf-8",
    )
    supervisor = RuntimeSupervisor(
        settings,
        root=tmp_path,
        controller=FakeController(),
        popen=popen,
    )

    components = supervisor.tick()

    assert components["paper_forward"]["status"] == "WAITING_DEPENDENCY"
    assert [process.command[-1] for process in launched] == ["live15_quant.managed_control_center"]


def test_stale_component_receipt_uses_bounded_restart_backoff(tmp_path: Path) -> None:
    clock = [100.0]
    settings = configured(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "paper-forward-status.json").write_text(
        json.dumps(
            {
                "status": "RUNNING",
                "pid": 999_999_999,
                "started_at": NOW.isoformat(),
                "last_heartbeat": NOW.isoformat(),
                "last_error": None,
                "expected_mode": "PAPER_SHADOW_LOCAL_ONLY",
            }
        ),
        encoding="utf-8",
    )
    launched: list[FakeProcess] = []

    def popen(command, **_kwargs):
        process = FakeProcess(command)
        launched.append(process)
        return process

    supervisor = RuntimeSupervisor(
        settings,
        root=tmp_path,
        controller=FakeController(),
        popen=popen,
        monotonic=lambda: clock[0],
    )

    first = supervisor.tick()
    second = supervisor.tick()
    clock[0] += 5.0
    third = supervisor.tick()

    assert first["paper_forward"]["status"] == "BACKOFF"
    assert second["paper_forward"]["status"] == "BACKOFF"
    assert third["paper_forward"]["status"] == "STARTING"
    assert sum(p.command[-1] == "live15_quant.managed_paper" for p in launched) == 1


def test_one_component_launch_failure_is_backed_off_without_killing_tick(
    tmp_path: Path,
) -> None:
    def popen(command, **_kwargs):
        if command[-1] == "live15_quant.managed_control_center":
            raise OSError("simulated launch failure")
        return FakeProcess(command)

    supervisor = RuntimeSupervisor(
        configured(tmp_path),
        root=tmp_path,
        controller=FakeController(),
        popen=popen,
        monotonic=lambda: 100.0,
    )

    components = supervisor.tick()

    assert components["recorder"]["status"] == "HEALTHY"
    assert components["control_center"]["status"] == "BACKOFF"
    assert components["control_center"]["last_error"] == "OSError"
    assert components["paper_forward"]["status"] == "STARTING"


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


@pytest.mark.asyncio
async def test_control_center_heartbeat_failure_requests_bounded_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = SimpleNamespace(should_exit=False, started=True)

    def fail_status_write(*_args, **_kwargs) -> None:
        raise PermissionError("persistent status write failure")

    monkeypatch.setattr("live15_quant.managed_control_center.atomic_json", fail_status_write)

    with pytest.raises(PermissionError, match="status write failure"):
        await _monitor_runtime(
            server=server,
            status={},
            status_path=tmp_path / "status.json",
            control_path=tmp_path / "control.json",
        )

    assert server.should_exit is True
