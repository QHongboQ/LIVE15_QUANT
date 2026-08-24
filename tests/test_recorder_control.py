from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import live15_quant.control_center_launcher as launcher
import live15_quant.managed_recorder as managed
import live15_quant.recorder_control as control
from live15_quant.config import Settings
from live15_quant.managed_recorder import (
    StartupCancelled,
    StartupDiagnostics,
    StartupHealthBaseline,
    _last_verified_health,
)
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


def test_managed_restart_reuses_only_valid_last_verified_health(tmp_path) -> None:
    configured = managed_settings(tmp_path)
    configured.recorder_health_path.parent.mkdir(parents=True)
    counts = {
        "kalshi_market_lifecycle": 1,
        "kalshi_prediction_quotes": 2,
        "coinbase_ticks": 123,
        "underlying_observations": 4,
        "secondary_underlying_observations": 5,
        "kalshi_ws_orderbook_events": 6,
        "kalshi_ws_book_checkpoints": 7,
        "kalshi_settlements": 8,
        "kalshi_settlement_conflicts": 0,
        "data_gaps": 9,
    }
    configured.recorder_health_path.write_text(
        json.dumps(
            {
                "integrity": "ok",
                "row_counts": counts,
                "active_settlement_followups": 3,
            }
        ),
        encoding="utf-8",
    )
    assert _last_verified_health(configured) == StartupHealthBaseline(counts, 3, "ok")

    configured.recorder_health_path.write_text(
        json.dumps({"integrity": "not_checked", "row_counts": {"coinbase_ticks": 123}}),
        encoding="utf-8",
    )
    assert _last_verified_health(configured) is None


def test_startup_phase_diagnostics_are_bounded_and_path_free(tmp_path) -> None:
    path = tmp_path / "data" / "recorder-startup.json"
    clock = iter((10.0, 10.0, 10.25, 10.5)).__next__
    diagnostics = StartupDiagnostics(path, monotonic=clock)
    diagnostics.record("schema_check", 0.25)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["phase"] == "schema_check"
    assert payload["phases"]["schema_check"] == {
        "duration_seconds": 0.25,
        "elapsed_seconds": 0.25,
    }
    assert str(tmp_path) not in path.read_text(encoding="utf-8")


def test_pause_during_synchronous_startup_is_observed_between_bounded_phases(
    tmp_path, monkeypatch
) -> None:
    configured = managed_settings(tmp_path)
    monkeypatch.setattr(control, "project_root", lambda: tmp_path)
    monkeypatch.setattr(managed, "load_settings", lambda: configured)
    write_control(configured.recorder_control_path, desired="running", state="starting")
    phases: list[str] = []

    class PauseAfterDatabaseOpen:
        def record(self, phase: str, _duration: float) -> None:
            phases.append(phase)
            if phase == "db_open":
                write_control(configured.recorder_control_path, desired="paused", state="stopping")

    with pytest.raises(StartupCancelled, match="db_open"):
        asyncio.run(managed._run(PauseAfterDatabaseOpen()))  # type: ignore[arg-type]

    assert phases == ["db_open"]
    assert json.loads(configured.recorder_control_path.read_text())["desired"] == "paused"


def test_default_startup_budget_is_30_seconds(tmp_path, monkeypatch) -> None:
    configured = managed_settings(tmp_path)
    monkeypatch.setattr(control, "project_root", lambda: tmp_path)
    controller = RecorderProcessController(configured)
    assert controller.start_timeout == 30.0


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
    credential_root = tmp_path / "home" / ".live15_quant" / "credentials"
    credential_root.mkdir(parents=True)
    (credential_root / "kalshi-production-readonly-key-id.txt").write_text(
        "test-key-id\n", encoding="utf-8"
    )
    (credential_root / "kalshi-production-readonly.key").write_text(
        "test-private-key\n", encoding="utf-8"
    )
    monkeypatch.setattr(control.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("LIVE15_ENABLE_KALSHI_PRODUCTION_WEBSOCKET", "false")
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
    assert Path(captured["kwargs"]["stdout"].name).name == "managed_recorder.log"  # type: ignore[index,union-attr]
    assert Path(captured["kwargs"]["stderr"].name).name == "managed_recorder.error.log"  # type: ignore[index,union-attr]
    assert captured["kwargs"]["creationflags"] == WINDOWS_BACKGROUND_FLAGS  # type: ignore[index]
    assert captured["kwargs"]["env"]["LIVE15_ENABLE_SECONDARY_UNDERLYING"] == "true"  # type: ignore[index]
    assert captured["kwargs"]["env"]["LIVE15_ENABLE_KALSHI_PRODUCTION_WEBSOCKET"] == "true"  # type: ignore[index]
    assert captured["kwargs"]["env"]["LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH"] == str(
        credential_root / "kalshi-production-readonly-key-id.txt"
    )  # type: ignore[index]
    assert captured["kwargs"]["env"]["LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH"] == str(
        credential_root / "kalshi-production-readonly.key"
    )  # type: ignore[index]
    assert controller.pause().state is ManagedRecorderState.PAUSED
    assert controller.status().pid is None


def test_managed_explicit_ws_opt_out_preserves_disabled_environment(tmp_path, monkeypatch) -> None:
    configured = managed_settings(tmp_path)
    monkeypatch.setattr(control, "project_root", lambda: tmp_path)
    credential_root = tmp_path / "home" / ".live15_quant" / "credentials"
    credential_root.mkdir(parents=True)
    (credential_root / "kalshi-production-readonly-key-id.txt").write_text(
        "test-key-id\n", encoding="utf-8"
    )
    (credential_root / "kalshi-production-readonly.key").write_text(
        "test-private-key\n", encoding="utf-8"
    )
    monkeypatch.setattr(control.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("LIVE15_ENABLE_KALSHI_PRODUCTION_WEBSOCKET", "false")
    monkeypatch.setenv("LIVE15_MANAGED_DISABLE_KALSHI_PRODUCTION_WEBSOCKET", "true")
    alive = {2469: False}
    clock = [0.0]
    captured: dict[str, object] = {}

    class Process:
        def __init__(self, _args, **kwargs) -> None:
            captured["kwargs"] = kwargs
            configured.recorder_pid_path.parent.mkdir(parents=True, exist_ok=True)
            configured.recorder_pid_path.write_text("2469\n", encoding="ascii")
            write_control(configured.recorder_control_path, desired="running", state="starting")
            configured.recorder_health_path.write_text(
                json.dumps({"observed_at": "2099-01-01T00:00:00+00:00"}), encoding="utf-8"
            )
            alive[2469] = True

        def poll(self):
            return None if alive[2469] else 0

    def sleep(seconds: float) -> None:
        clock[0] += seconds
        if json.loads(configured.recorder_control_path.read_text())["desired"] == "paused":
            alive[2469] = False

    monkeypatch.setattr(control, "process_alive", lambda pid: alive.get(pid, False))
    controller = RecorderProcessController(
        configured,
        monotonic=lambda: clock[0],
        sleep=sleep,
        popen=Process,  # type: ignore[arg-type]
    )

    assert controller.start().state is ManagedRecorderState.RUNNING
    child_environment = captured["kwargs"]["env"]  # type: ignore[index]
    assert child_environment["LIVE15_ENABLE_KALSHI_PRODUCTION_WEBSOCKET"] == "false"
    assert "LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH" not in child_environment
    assert "LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH" not in child_environment


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


def test_repeated_pause_is_idempotent_and_does_not_rewrite_control_state(
    tmp_path, monkeypatch
) -> None:
    configured = managed_settings(tmp_path)
    monkeypatch.setattr(control, "project_root", lambda: tmp_path)
    write_control(configured.recorder_control_path, desired="paused", state="paused")
    controller = RecorderProcessController(configured)
    writes: list[tuple[object, ...]] = []
    monkeypatch.setattr(controller, "_write_control", lambda *args: writes.append(args))

    first = controller.pause()
    second = controller.pause()

    assert first.state is second.state is ManagedRecorderState.PAUSED
    assert writes == []


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
    payload = json.loads(configured.recorder_control_path.read_text())
    assert payload["state"] == "error"
    assert payload["desired"] == "paused"
    assert clock[0] <= 60.2


def test_startup_timeout_requests_pause_and_observes_cooperative_exit(
    tmp_path, monkeypatch
) -> None:
    configured = managed_settings(tmp_path)
    monkeypatch.setattr(control, "project_root", lambda: tmp_path)
    alive = {8642: True}
    clock = [0.0]

    class Process:
        def __init__(self, _args, **_kwargs) -> None:
            configured.recorder_pid_path.parent.mkdir(parents=True, exist_ok=True)
            configured.recorder_pid_path.write_text("8642\n", encoding="ascii")

        def poll(self):
            return None if alive[8642] else 0

    def sleep(seconds: float) -> None:
        clock[0] += seconds
        payload = json.loads(configured.recorder_control_path.read_text())
        if payload["desired"] == "paused":
            alive[8642] = False
            configured.recorder_pid_path.unlink(missing_ok=True)

    monkeypatch.setattr(control, "process_alive", lambda pid: alive.get(pid, False))
    controller = RecorderProcessController(
        configured,
        monotonic=lambda: clock[0],
        sleep=sleep,
        popen=Process,  # type: ignore[arg-type]
        start_timeout=0.2,
        stop_timeout=0.2,
    )

    with pytest.raises(TimeoutError, match="heartbeat"):
        controller.start()

    payload = json.loads(configured.recorder_control_path.read_text())
    assert payload["desired"] == "paused"
    assert alive[8642] is False
    assert clock[0] <= 0.300001


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
