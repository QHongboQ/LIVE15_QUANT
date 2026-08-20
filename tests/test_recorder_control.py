from __future__ import annotations

import json
from pathlib import Path

import pytest

import live15_quant.control_center_launcher as launcher
import live15_quant.recorder_control as control
from live15_quant.config import Settings
from live15_quant.recorder_control import (
    WINDOWS_BACKGROUND_FLAGS,
    ManagedRecorderState,
    RecorderPidLease,
    RecorderProcessController,
)


def managed_settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    return Settings(
        recorder_data_path=data / "live15.sqlite3",
        recorder_health_path=data / "health.json",
        recorder_control_path=data / "recorder-control.json",
        recorder_pid_path=data / "recorder.pid",
    )


def write_control(path: Path, *, desired: str, state: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"desired": desired, "state": state, "message": state}), encoding="utf-8"
    )


def test_duplicate_start_returns_running_without_spawning(tmp_path, monkeypatch) -> None:
    configured = managed_settings(tmp_path)
    monkeypatch.setattr(control, "project_root", lambda: tmp_path)
    configured.recorder_pid_path.parent.mkdir(parents=True)
    configured.recorder_pid_path.write_text("4321\n", encoding="ascii")
    write_control(configured.recorder_control_path, desired="running", state="running")
    monkeypatch.setattr(control, "process_alive", lambda pid: pid == 4321)
    calls = 0

    def forbidden_popen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("duplicate start must not spawn")

    controller = RecorderProcessController(configured, popen=forbidden_popen)

    assert controller.start().state is ManagedRecorderState.RUNNING
    assert calls == 0


def test_start_and_graceful_pause_use_fixed_process_and_bounded_wait(tmp_path, monkeypatch) -> None:
    configured = managed_settings(tmp_path)
    monkeypatch.setattr(control, "project_root", lambda: tmp_path)
    alive = {9876: False}
    clock = [0.0]
    captured: dict[str, object] = {}

    class Process:
        def __init__(self, args, **kwargs) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs
            configured.recorder_pid_path.parent.mkdir(parents=True, exist_ok=True)
            configured.recorder_pid_path.write_text("9876\n", encoding="ascii")
            write_control(configured.recorder_control_path, desired="running", state="starting")
            configured.recorder_health_path.write_text(
                json.dumps({"observed_at": "2099-01-01T00:00:00+00:00"}),
                encoding="utf-8",
            )
            alive[9876] = True

        def poll(self):
            return None if alive[9876] else 0

    def sleep(seconds: float) -> None:
        clock[0] += seconds
        if json.loads(configured.recorder_control_path.read_text())["desired"] == "paused":
            alive[9876] = False

    monkeypatch.setattr(control, "process_alive", lambda pid: alive.get(pid, False))
    controller = RecorderProcessController(
        configured,
        monotonic=lambda: clock[0],
        sleep=sleep,
        popen=Process,  # type: ignore[arg-type]
        start_timeout=2,
        stop_timeout=2,
    )

    assert controller.start().state is ManagedRecorderState.RUNNING
    assert captured["args"][-2:] == ["-m", "live15_quant.managed_recorder"]
    assert captured["kwargs"]["stdin"] is not None  # type: ignore[index]
    assert captured["kwargs"]["stdout"] is not None  # type: ignore[index]
    assert captured["kwargs"]["stderr"] is not None  # type: ignore[index]
    assert captured["kwargs"]["creationflags"] == WINDOWS_BACKGROUND_FLAGS  # type: ignore[index]
    assert controller.pause().state is ManagedRecorderState.PAUSED
    assert controller.status().pid is None


def test_pause_timeout_never_force_kills_process(tmp_path, monkeypatch) -> None:
    configured = managed_settings(tmp_path)
    monkeypatch.setattr(control, "project_root", lambda: tmp_path)
    configured.recorder_pid_path.parent.mkdir(parents=True)
    configured.recorder_pid_path.write_text("2468\n", encoding="ascii")
    write_control(configured.recorder_control_path, desired="running", state="running")
    monkeypatch.setattr(control, "process_alive", lambda _pid: True)
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    controller = RecorderProcessController(
        configured, monotonic=lambda: clock[0], sleep=sleep, stop_timeout=0.2
    )

    try:
        controller.pause()
    except TimeoutError as error:
        assert "graceful" in str(error)
    else:
        raise AssertionError("pause must time out")
    assert control.process_alive(2468)
    assert json.loads(configured.recorder_control_path.read_text())["state"] == "stopping"


def test_start_requires_a_new_recorder_heartbeat(tmp_path, monkeypatch) -> None:
    configured = managed_settings(tmp_path)
    monkeypatch.setattr(control, "project_root", lambda: tmp_path)
    configured.recorder_health_path.parent.mkdir(parents=True)
    configured.recorder_health_path.write_text(
        json.dumps({"observed_at": "2020-01-01T00:00:00+00:00"}), encoding="utf-8"
    )
    alive = True
    clock = [0.0]

    class Process:
        def __init__(self, _args, **_kwargs) -> None:
            configured.recorder_pid_path.write_text("1357\n", encoding="ascii")
            write_control(configured.recorder_control_path, desired="running", state="starting")

        def poll(self):
            return None if alive else 1

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(control, "process_alive", lambda pid: pid == 1357 and alive)
    controller = RecorderProcessController(
        configured,
        monotonic=lambda: clock[0],
        sleep=sleep,
        popen=Process,  # type: ignore[arg-type]
        start_timeout=0.2,
    )

    with pytest.raises(TimeoutError, match="heartbeat"):
        controller.start()
    assert json.loads(configured.recorder_control_path.read_text())["state"] == "error"


def test_start_rejects_unchanged_future_dated_heartbeat(tmp_path, monkeypatch) -> None:
    configured = managed_settings(tmp_path)
    monkeypatch.setattr(control, "project_root", lambda: tmp_path)
    configured.recorder_health_path.parent.mkdir(parents=True)
    configured.recorder_health_path.write_text(
        json.dumps({"observed_at": "2099-01-01T00:00:00+00:00"}), encoding="utf-8"
    )
    clock = [0.0]

    class Process:
        def __init__(self, _args, **_kwargs) -> None:
            configured.recorder_pid_path.write_text("9753\n", encoding="ascii")
            write_control(configured.recorder_control_path, desired="running", state="starting")

        def poll(self):
            return None

    monkeypatch.setattr(control, "process_alive", lambda pid: pid == 9753)

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    controller = RecorderProcessController(
        configured,
        monotonic=lambda: clock[0],
        sleep=sleep,
        popen=Process,  # type: ignore[arg-type]
        start_timeout=0.2,
    )
    with pytest.raises(TimeoutError, match="heartbeat"):
        controller.start()


def test_pid_lease_removes_stale_pid_and_cleans_normal_or_exception_exit(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "recorder.pid"
    path.write_text("99999\n", encoding="ascii")
    monkeypatch.setattr(control, "process_alive", lambda _pid: False)

    with RecorderPidLease(path) as lease:
        assert path.read_text(encoding="ascii").strip() == str(lease.pid)
    assert not path.exists()

    with pytest.raises(RuntimeError, match="simulated crash"):
        with RecorderPidLease(path):
            raise RuntimeError("simulated crash")
    assert not path.exists()


def test_pid_lease_refuses_live_owner_without_replacing_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "recorder.pid"
    path.write_text("2468\n", encoding="ascii")
    monkeypatch.setattr(control, "process_alive", lambda pid: pid == 2468)

    with pytest.raises(RuntimeError, match="already running"):
        RecorderPidLease(path).__enter__()
    assert path.read_text(encoding="ascii") == "2468\n"


def test_controller_recovers_stale_pid_and_start_lock(tmp_path, monkeypatch) -> None:
    configured = managed_settings(tmp_path)
    monkeypatch.setattr(control, "project_root", lambda: tmp_path)
    configured.recorder_pid_path.parent.mkdir(parents=True)
    configured.recorder_pid_path.write_text("99991\n", encoding="ascii")
    start_lock = configured.recorder_pid_path.parent / "recorder-start.lock"
    start_lock.write_text("99992\n", encoding="ascii")
    monkeypatch.setattr(control, "process_alive", lambda _pid: False)
    controller = RecorderProcessController(configured)

    assert controller.status().state is ManagedRecorderState.STOPPED
    assert not configured.recorder_pid_path.exists()
    controller._acquire_start_lock()
    assert start_lock.read_text(encoding="ascii").strip() == str(control.os.getpid())
    controller._release_start_lock()
    assert not start_lock.exists()


def test_launcher_opens_existing_ui_without_second_server(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(launcher, "_control_center_running", lambda _url: True)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    monkeypatch.setattr(
        launcher.uvicorn, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError())
    )

    assert launcher.launch(Settings()) == 0
    assert opened == ["http://127.0.0.1:8765"]


def test_launcher_rejects_non_live15_port_occupant(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "_control_center_running", lambda _url: False)
    monkeypatch.setattr(launcher, "_port_available", lambda _port: False)

    assert launcher.launch(Settings()) == 2


def test_windows_launcher_is_fixed_and_does_not_require_powershell() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "start_control_center.cmd").read_text(
        encoding="utf-8"
    )
    lowered = script.lower()
    assert '.venv\\scripts\\python.exe" -m live15_quant.control_center_launcher' in lowered
    assert "powershell" not in lowered
    assert "%*" not in script
    assert "0.0.0.0" not in script
