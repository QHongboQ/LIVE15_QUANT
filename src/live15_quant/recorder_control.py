"""Fixed-command, localhost-only process supervision for the native recorder."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from live15_quant.config import Settings
from live15_quant.kalshi_gateway.client import KalshiGatewayError, production_runtime_environment

WINDOWS_CREATE_NO_WINDOW = 0x08000000
WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
WINDOWS_BACKGROUND_FLAGS = WINDOWS_CREATE_NO_WINDOW | WINDOWS_CREATE_NEW_PROCESS_GROUP


class ManagedRecorderState(StrEnum):
    RUNNING = "running"
    STARTING = "starting"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class RecorderControlStatus:
    state: ManagedRecorderState
    pid: int | None
    message: str


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_pid(path: Path) -> int | None:
    try:
        if path.stat().st_size > 32:
            return None
        raw = path.read_text(encoding="ascii").strip()
        pid = int(raw)
        return pid if pid > 0 else None
    except (FileNotFoundError, OSError, ValueError):
        return None


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            exit_code = ctypes.c_ulong()
            return bool(
                ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))
                and exit_code.value == 259
            )
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class RecorderPidLease(AbstractContextManager["RecorderPidLease"]):
    """Cross-process singleton lease represented by one fixed runtime PID file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.pid = os.getpid()
        self._owned = False

    def __enter__(self) -> RecorderPidLease:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = _read_pid(self.path)
        if existing is not None and process_alive(existing):
            raise RuntimeError("another LIVE15 recorder process is already running")
        if existing is not None:
            self.path.unlink(missing_ok=True)
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise RuntimeError("another LIVE15 recorder process is starting") from error
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(f"{self.pid}\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._owned = True
        return self

    def __exit__(self, *_args: object) -> None:
        if self._owned and _read_pid(self.path) == self.pid:
            self.path.unlink(missing_ok=True)
        self._owned = False


class RecorderProcessController:
    """Expose only start, graceful pause, and resume for one fixed recorder command."""

    def __init__(
        self,
        settings: Settings,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        start_timeout: float = 30.0,
        stop_timeout: float = 60.0,
    ) -> None:
        self.settings = settings
        self._monotonic = monotonic
        self._sleep = sleep
        self._popen = popen
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        root = project_root()
        expected = (root / "data" / "live15.sqlite3").resolve()
        if settings.recorder_data_path.resolve() != expected:
            raise ValueError("Control Center recorder control requires data/live15.sqlite3")
        self._control_path = settings.recorder_control_path.resolve()
        self._pid_path = settings.recorder_pid_path.resolve()
        data_root = (root / "data").resolve()
        if self._control_path.parent != data_root or self._pid_path.parent != data_root:
            raise ValueError(
                "recorder control runtime files must stay in the project data directory"
            )
        self._start_lock = data_root / "recorder-start.lock"

    def status(self) -> RecorderControlStatus:
        payload = self._read_control()
        pid = _read_pid(self._pid_path)
        alive = pid is not None and process_alive(pid)
        state_raw = payload.get("state")
        desired = payload.get("desired")
        if alive:
            if state_raw == ManagedRecorderState.STOPPING.value or desired == "paused":
                state = ManagedRecorderState.STOPPING
            elif state_raw == ManagedRecorderState.STARTING.value:
                state = ManagedRecorderState.STARTING
            else:
                state = ManagedRecorderState.RUNNING
            return RecorderControlStatus(state, pid, str(payload.get("message", state.value)))
        if pid is not None:
            self._pid_path.unlink(missing_ok=True)
        if state_raw == ManagedRecorderState.ERROR.value:
            return RecorderControlStatus(
                ManagedRecorderState.ERROR, None, "recorder exited with error"
            )
        if desired == "paused" or state_raw == ManagedRecorderState.PAUSED.value:
            return RecorderControlStatus(ManagedRecorderState.PAUSED, None, "collection is paused")
        return RecorderControlStatus(ManagedRecorderState.STOPPED, None, "recorder is stopped")

    def start(self) -> RecorderControlStatus:
        current = self.status()
        if current.state in {ManagedRecorderState.RUNNING, ManagedRecorderState.STARTING}:
            return current
        if current.state is ManagedRecorderState.STOPPING:
            raise RuntimeError("recorder is still stopping")
        self._acquire_start_lock()
        try:
            current = self.status()
            if current.state in {ManagedRecorderState.RUNNING, ManagedRecorderState.STARTING}:
                return current
            self._write_control("running", ManagedRecorderState.STARTING, "recorder is starting")
            previous_heartbeat = self._heartbeat_marker()
            environment = os.environ.copy()
            # Public, market-data-only secondary streams are part of the single
            # UI-managed recorder. Manual foreground runs remain opt-in via env.
            environment.setdefault("LIVE15_ENABLE_SECONDARY_UNDERLYING", "true")
            managed_ws_disabled = environment.get(
                "LIVE15_MANAGED_DISABLE_KALSHI_PRODUCTION_WEBSOCKET", ""
            ).strip().lower() in {"1", "true", "yes", "on"}
            if not managed_ws_disabled:
                try:
                    environment = production_runtime_environment(self.settings, base=environment)
                except KalshiGatewayError:
                    # Credential availability must never turn into a Demo or stale
                    # credential fallback.  The recorder continues public collection
                    # while its official Production WS remains explicitly disabled.
                    for name in (
                        "LIVE15_KALSHI_DEMO_API_KEY_ID",
                        "LIVE15_KALSHI_DEMO_API_KEY_ID_FILE",
                        "LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH",
                    ):
                        environment.pop(name, None)
                    environment["LIVE15_ENABLE_KALSHI_PRODUCTION_WEBSOCKET"] = "false"
            else:
                for name in (
                    "KALSHI_DEMO",
                    "LIVE15_KALSHI_DEMO_API_KEY_ID",
                    "LIVE15_KALSHI_DEMO_API_KEY_ID_FILE",
                    "LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH",
                    "LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH",
                    "LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH",
                ):
                    environment.pop(name, None)
            flags = 0
            if os.name == "nt":
                # DETACHED_PROCESS is deliberately absent: combined with CREATE_NO_WINDOW
                # it caused an empty console. A new process group keeps the console-free
                # recorder isolated from Ctrl+C delivered while the UI shuts down.
                flags = WINDOWS_BACKGROUND_FLAGS
            launched_at = datetime.now(UTC)
            logs = project_root() / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            # A managed startup must preserve its traceback locally.  Dropping
            # stderr made a typed fatal health field impossible to diagnose after
            # the child had exited.  The recorder never logs credentials; this
            # file is operational diagnostics only.
            with (
                (logs / "managed_recorder.log").open("ab") as stdout,
                (logs / "managed_recorder.error.log").open("ab") as stderr,
            ):
                process = self._popen(
                    [sys.executable, "-m", "live15_quant.managed_recorder"],
                    cwd=project_root(),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    close_fds=True,
                    creationflags=flags,
                )
            deadline = self._monotonic() + self.start_timeout
            while self._monotonic() < deadline:
                status = self.status()
                desired_paused = self._read_control().get("desired") == "paused"
                if status.state is ManagedRecorderState.ERROR:
                    self._write_control(
                        "paused" if desired_paused else "running",
                        ManagedRecorderState.ERROR,
                        "recorder failed during startup",
                    )
                    raise RuntimeError("recorder failed during bounded startup")
                if desired_paused:
                    if process.poll() is not None or status.pid is None:
                        self._write_control(
                            "paused", ManagedRecorderState.PAUSED, "startup cancelled gracefully"
                        )
                        return self.status()
                    self._sleep(min(0.1, max(0.0, deadline - self._monotonic())))
                    continue
                if status.pid is not None and self._heartbeat_since(
                    launched_at, previous_heartbeat
                ):
                    self._write_control(
                        "running", ManagedRecorderState.RUNNING, "recorder is running"
                    )
                    return self.status()
                if process.poll() is not None:
                    desired = str(self._read_control().get("desired", "running"))
                    self._write_control(
                        desired, ManagedRecorderState.ERROR, "recorder failed during startup"
                    )
                    raise RuntimeError("recorder failed during bounded startup")
                self._sleep(min(0.1, max(0.0, deadline - self._monotonic())))
            self._write_control(
                "paused",
                ManagedRecorderState.STOPPING,
                "startup heartbeat timed out; graceful stop requested",
            )
            cleanup_deadline = self._monotonic() + min(30.0, self.stop_timeout)
            while self._monotonic() < cleanup_deadline:
                pid = _read_pid(self._pid_path)
                if pid is None or not process_alive(pid):
                    break
                self._sleep(min(0.1, max(0.0, cleanup_deadline - self._monotonic())))
            self._write_control(
                "paused", ManagedRecorderState.ERROR, "startup heartbeat confirmation timed out"
            )
            raise TimeoutError(
                f"recorder startup heartbeat timed out after {self.start_timeout:g}s"
            )
        finally:
            self._release_start_lock()

    def resume(self) -> RecorderControlStatus:
        return self.start()

    def pause(self) -> RecorderControlStatus:
        current = self.status()
        if current.state is ManagedRecorderState.PAUSED:
            return current
        if current.state is ManagedRecorderState.STOPPED:
            self._write_control("paused", ManagedRecorderState.PAUSED, "collection is paused")
            return self.status()
        if current.pid is None:
            raise RuntimeError("recorder process identity is unavailable")
        self._write_control("paused", ManagedRecorderState.STOPPING, "graceful stop requested")
        deadline = self._monotonic() + self.stop_timeout
        while self._monotonic() < deadline:
            if not process_alive(current.pid):
                self._write_control("paused", ManagedRecorderState.PAUSED, "collection is paused")
                return self.status()
            self._sleep(min(0.1, max(0.0, deadline - self._monotonic())))
        raise TimeoutError(f"graceful recorder stop timed out after {self.stop_timeout:g}s")

    def write_child_state(self, desired: str, state: ManagedRecorderState, message: str) -> None:
        self._write_control(desired, state, message)

    def desired_state(self) -> str:
        return str(self._read_control().get("desired", "running"))

    def _acquire_start_lock(self) -> None:
        self._start_lock.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                descriptor = os.open(self._start_lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as error:
                owner = _read_pid(self._start_lock)
                try:
                    age = max(0.0, time.time() - self._start_lock.stat().st_mtime)
                except OSError:
                    age = 0.0
                recent_unowned = owner is None and age <= max(30.0, self.start_timeout * 2)
                if (owner is not None and process_alive(owner)) or recent_unowned or attempt:
                    raise RuntimeError(
                        "another recorder control operation is in progress"
                    ) from error
                self._start_lock.unlink(missing_ok=True)
                continue
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(f"{os.getpid()}\n")
                stream.flush()
                os.fsync(stream.fileno())
            return
        raise RuntimeError("another recorder control operation is in progress")

    def _release_start_lock(self) -> None:
        if _read_pid(self._start_lock) == os.getpid():
            self._start_lock.unlink(missing_ok=True)

    def _read_control(self) -> dict[str, object]:
        try:
            if self._control_path.stat().st_size > 64 * 1024:
                return {}
            parsed = json.loads(self._control_path.read_text(encoding="utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _write_control(self, desired: str, state: ManagedRecorderState, message: str) -> None:
        _atomic_json(
            self._control_path,
            {
                "desired": desired,
                "state": state.value,
                "message": message,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    def _heartbeat_marker(self) -> tuple[int, str] | None:
        try:
            stat = self.settings.recorder_health_path.stat()
            if stat.st_size > 256 * 1024:
                return None
            payload = json.loads(self.settings.recorder_health_path.read_text(encoding="utf-8"))
            return stat.st_mtime_ns, str(payload["observed_at"])
        except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def _heartbeat_since(self, launched_at: datetime, previous: tuple[int, str] | None) -> bool:
        try:
            stat = self.settings.recorder_health_path.stat()
            if stat.st_size > 256 * 1024:
                return False
            payload = json.loads(self.settings.recorder_health_path.read_text(encoding="utf-8"))
            observed_raw = str(payload["observed_at"])
            if previous == (stat.st_mtime_ns, observed_raw):
                return False
            observed = datetime.fromisoformat(observed_raw)
            if observed.tzinfo is None or observed.utcoffset() is None:
                return False
            return observed.astimezone(UTC) >= launched_at
        except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False
