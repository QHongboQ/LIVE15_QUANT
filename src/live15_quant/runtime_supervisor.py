"""Single-instance Windows runtime supervisor for LIVE15 data services.

The supervisor owns process lifecycle only. It has no credential loading, model,
risk, Demo execution, or Production execution capability.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from live15_quant.config import Settings, load_settings
from live15_quant.kalshi_gateway.client import KalshiGatewayError, production_runtime_environment
from live15_quant.recorder_control import (
    WINDOWS_BACKGROUND_FLAGS,
    ManagedRecorderState,
    RecorderProcessController,
    process_alive,
    process_identity,
)
from live15_quant.runtime_status import (
    RuntimePidLease,
    RuntimeStatusError,
    atomic_json,
    read_json,
    utc_timestamp,
)
from live15_quant.shadow_execution import DEMO_REAL_WRITE_FROZEN_PROVIDER_BLOCKER

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ManagedChild:
    name: str
    module: str
    status_path: Path
    stdout_path: Path
    stderr_path: Path
    launcher: subprocess.Popen[bytes] | None = None
    failures: int = 0
    next_launch_monotonic: float = 0.0
    healthy_since_monotonic: float | None = None


def _control_center_http_healthy(host: str, port: int) -> bool:
    """Verify the listener is this read-only Control Center, not just any PID."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=2.0) as response:
            if response.status != 200:
                return False
        with urllib.request.urlopen(f"http://{host}:{port}/api/system", timeout=2.0) as response:
            payload = json.loads(response.read(64 * 1024))
        return bool(
            isinstance(payload, dict)
            and payload.get("service") == "LIVE15 Control Center"
            and payload.get("bind_host") == "127.0.0.1"
        )
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return False


class RuntimeSupervisor:
    """Start dependencies in order and restart independent services with backoff."""

    def __init__(
        self,
        settings: Settings,
        *,
        root: Path | None = None,
        monotonic: Any = time.monotonic,
        sleep: Any = time.sleep,
        popen: Any = subprocess.Popen,
        controller: Any = None,
        identity_lookup: Any = process_identity,
        control_center_probe: Any = _control_center_http_healthy,
    ) -> None:
        self.settings = settings
        self.root = (root or Path.cwd()).resolve()
        self.runtime = self.root / "runtime"
        self.logs = self.root / "logs"
        self.status_path = self.runtime / "runtime-supervisor-status.json"
        self.control_path = self.runtime / "runtime-supervisor-control.json"
        self._monotonic = monotonic
        self._sleep = sleep
        self._popen = popen
        self.controller = controller or RecorderProcessController(settings)
        self._identity_lookup = identity_lookup
        self._control_center_probe = control_center_probe
        self.started_at = utc_timestamp()
        self.children = {
            "kalshi_sdk_ws_shadow": ManagedChild(
                "kalshi_sdk_ws_shadow",
                "live15_quant.managed_kalshi_sdk_shadow",
                self.runtime / "kalshi-sdk-ws-shadow-status.json",
                self.logs / "kalshi_sdk_ws_shadow.log",
                self.logs / "kalshi_sdk_ws_shadow.error.log",
            ),
            "current_trainable": ManagedChild(
                "current_trainable",
                "live15_quant.managed_trainable",
                self.runtime / "current-trainable-status.json",
                self.logs / "current_trainable_worker.log",
                self.logs / "current_trainable_worker.error.log",
            ),
            "paper_forward": ManagedChild(
                "paper_forward",
                "live15_quant.managed_paper",
                self.runtime / "paper-forward-status.json",
                self.logs / "paper_forward_worker.log",
                self.logs / "paper_forward_worker.error.log",
            ),
            "control_center": ManagedChild(
                "control_center",
                "live15_quant.managed_control_center",
                self.runtime / "control-center-status.json",
                self.logs / "control_center.log",
                self.logs / "control_center.error.log",
            ),
        }
        self._last_components: dict[str, dict[str, object]] = {}
        self._recorder_seen_running = False
        self._recorder_failures = 0
        self._recorder_next_launch = 0.0
        self._recorder_healthy_since: float | None = None

    def run(self) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        atomic_json(
            self.control_path,
            {"desired": "running", "updated_at": utc_timestamp(), "requested_by": "supervisor"},
        )
        while True:
            control = read_json(self.control_path)
            if control is not None and control.get("desired") == "stopped":
                self.stop_components()
                return
            self.tick()
            self._sleep(2.0)

    def tick(self) -> dict[str, dict[str, object]]:
        recorder = self._ensure_recorder()
        recorder_healthy = recorder["status"] == "HEALTHY"
        recorder_readable = recorder["status"] in {"HEALTHY", "RUNNING", "STARTING"}
        control_center = self._ensure_child(self.children["control_center"], allowed=True)
        kalshi_sdk_ws_shadow = self._ensure_child(
            self.children["kalshi_sdk_ws_shadow"], allowed=recorder_readable
        )
        current_trainable = self._ensure_child(
            self.children["current_trainable"], allowed=recorder_readable
        )
        paper = self._ensure_child(self.children["paper_forward"], allowed=recorder_healthy)
        first_fill = self._first_fill_status()
        components = {
            "recorder": recorder,
            "kalshi_sdk_ws_shadow": kalshi_sdk_ws_shadow,
            "current_trainable": current_trainable,
            "paper_forward": paper,
            "control_center": control_center,
            "demo_first_fill": first_fill,
        }
        self._last_components = components
        atomic_json(
            self.status_path,
            {
                "status": "RUNNING",
                "pid": os.getpid(),
                "started_at": self.started_at,
                "last_heartbeat": utc_timestamp(),
                "last_error": None,
                "process_alive": True,
                "expected_mode": "RUNTIME_SUPERVISOR_NO_TRADING",
                "working_directory": str(self.root),
                "components": components,
            },
        )
        return components

    def stop_components(self) -> None:
        # Paper and Control Center observe the shared stop receipt and exit cleanly.
        try:
            self.controller.pause()
        except (RuntimeError, TimeoutError) as error:
            logger.warning(
                "Recorder did not confirm graceful supervisor stop",
                extra={
                    "event": "supervisor_recorder_stop_failed",
                    "error_type": type(error).__name__,
                },
            )
        deadline = self._monotonic() + 30.0
        while self._monotonic() < deadline:
            live = False
            for child in self.children.values():
                payload = self._read_component(child.status_path)
                pid = int(payload.get("pid", 0)) if payload else 0
                live = live or (pid > 0 and process_alive(pid))
            if not live:
                break
            self._sleep(0.2)
        final_components = {
            name: self._component_projection(child) for name, child in self.children.items()
        }
        final_components["recorder"] = self._recorder_projection()
        final_components["demo_first_fill"] = self._first_fill_status()
        atomic_json(
            self.status_path,
            {
                "status": "STOPPED",
                "pid": os.getpid(),
                "started_at": self.started_at,
                "last_heartbeat": utc_timestamp(),
                "last_error": None,
                "process_alive": True,
                "expected_mode": "RUNTIME_SUPERVISOR_NO_TRADING",
                "working_directory": str(self.root),
                "components": final_components,
            },
        )

    def _ensure_recorder(self) -> dict[str, object]:
        status = self.controller.status()
        if status.state not in {ManagedRecorderState.RUNNING, ManagedRecorderState.STARTING}:
            now = self._monotonic()
            if now < self._recorder_next_launch:
                projection = self._recorder_projection()
                projection.update(
                    {
                        "status": "BACKOFF",
                        "restart_after_seconds": self._recorder_next_launch - now,
                    }
                )
                return projection
            if self._recorder_seen_running and self._recorder_next_launch == 0:
                self._recorder_failures += 1
                delay = min(60.0, 5.0 * (2 ** min(self._recorder_failures - 1, 4)))
                self._recorder_next_launch = now + delay
                projection = self._recorder_projection()
                projection.update({"status": "BACKOFF", "restart_after_seconds": delay})
                return projection
            self._recorder_next_launch = 0.0
            try:
                self.controller.resume()
            except (RuntimeError, TimeoutError) as error:
                return {
                    "status": "ERROR",
                    "pid": None,
                    "started_at": None,
                    "last_heartbeat": None,
                    "heartbeat_age_seconds": None,
                    "last_error": type(error).__name__,
                    "process_alive": False,
                    "expected_mode": "MANAGED_RECORDER",
                    "status_path": str(self.settings.recorder_health_path.resolve()),
                    "receipt_path": str(self.settings.recorder_control_path.resolve()),
                    "log_path": None,
                }
        projection = self._recorder_projection()
        if projection["status"] == "HEALTHY":
            self._recorder_seen_running = True
            if self._recorder_healthy_since is None:
                self._recorder_healthy_since = self._monotonic()
            elif self._monotonic() - self._recorder_healthy_since >= 60:
                self._recorder_failures = 0
                self._recorder_next_launch = 0.0
        else:
            self._recorder_healthy_since = None
        return projection

    def _recorder_projection(self) -> dict[str, object]:
        managed = self.controller.status()
        health = self._read_component(self.settings.recorder_health_path)
        observed = _aware_timestamp(health.get("observed_at") if health else None)
        age = (datetime.now(UTC) - observed).total_seconds() if observed else None
        alive = managed.pid is not None and process_alive(managed.pid)
        healthy = (
            alive
            and health is not None
            and health.get("status") == "healthy"
            and age is not None
            and -1.0 <= age <= self.settings.ui_heartbeat_stale_seconds
            and health.get("fatal_task") is None
            and health.get("fatal_error_type") is None
        )
        return {
            "status": "HEALTHY" if healthy else managed.state.value.upper(),
            "pid": managed.pid,
            "started_at": health.get("started_at") if health else None,
            "last_heartbeat": health.get("observed_at") if health else None,
            "heartbeat_age_seconds": max(0.0, age) if age is not None else None,
            "last_error": health.get("fatal_error_type") if health else None,
            "process_alive": alive,
            "expected_mode": "MANAGED_RECORDER",
            "status_path": str(self.settings.recorder_health_path.resolve()),
            "receipt_path": str(self.settings.recorder_control_path.resolve()),
            "log_path": None,
        }

    def _ensure_child(self, child: ManagedChild, *, allowed: bool) -> dict[str, object]:
        projection = self._component_projection(child)
        if not allowed:
            projection.update(
                {
                    "status": "WAITING_DEPENDENCY",
                    "last_error": "RECORDER_NOT_HEALTHY",
                }
            )
            return projection
        if bool(projection.get("process_alive")):
            now = self._monotonic()
            if child.healthy_since_monotonic is None:
                child.healthy_since_monotonic = now
            elif now - child.healthy_since_monotonic >= 60:
                child.failures = 0
                child.next_launch_monotonic = 0.0
            return projection
        child.healthy_since_monotonic = None
        if child.launcher is not None and child.launcher.poll() is None:
            projection.update(
                {
                    "status": "STARTING",
                    "process_alive": True,
                    "launcher_pid": child.launcher.pid,
                }
            )
            return projection
        now = self._monotonic()
        if now < child.next_launch_monotonic:
            projection["status"] = "BACKOFF"
            projection["restart_after_seconds"] = child.next_launch_monotonic - now
            return projection
        if child.next_launch_monotonic > 0:
            child.next_launch_monotonic = 0.0
            error = self._try_launch(child)
            if error is not None:
                projection.update(error)
                return projection
            projection.update(
                {
                    "status": "STARTING",
                    "launcher_pid": child.launcher.pid if child.launcher is not None else None,
                    "process_alive": True,
                }
            )
            return projection
        if projection.get("pid") is not None or child.launcher is not None:
            child.failures += 1
            delay = min(60.0, 5.0 * (2 ** min(child.failures - 1, 4)))
            child.next_launch_monotonic = now + delay
            child.launcher = None
            projection.update({"status": "BACKOFF", "restart_after_seconds": delay})
            return projection
        error = self._try_launch(child)
        if error is not None:
            projection.update(error)
            return projection
        return {
            "status": "STARTING",
            "pid": child.launcher.pid if child.launcher is not None else None,
            "started_at": utc_timestamp(),
            "last_heartbeat": None,
            "heartbeat_age_seconds": None,
            "last_error": None,
            "process_alive": True,
            "expected_mode": self._expected_mode(child.name),
            "status_path": str(child.status_path.resolve()),
            "receipt_path": str(
                (
                    self.runtime
                    / {
                        "kalshi_sdk_ws_shadow": "kalshi-sdk-ws-shadow.pid",
                        "paper_forward": "paper-forward.pid",
                        "current_trainable": "current-trainable.pid",
                        "control_center": "control-center.pid",
                    }[child.name]
                ).resolve()
            ),
            "log_path": str(child.stdout_path.resolve()),
            "launcher_pid": child.launcher.pid if child.launcher is not None else None,
        }

    def _try_launch(self, child: ManagedChild) -> dict[str, object] | None:
        try:
            self._launch(child)
            return None
        except OSError as error:
            child.failures += 1
            delay = min(60.0, 5.0 * (2 ** min(child.failures - 1, 4)))
            child.next_launch_monotonic = self._monotonic() + delay
            logger.warning(
                "Runtime component launch failed",
                extra={
                    "event": "runtime_component_launch_failed",
                    "component": child.name,
                    "error_type": type(error).__name__,
                    "restart_after_seconds": delay,
                },
            )
            return {
                "status": "BACKOFF",
                "last_error": type(error).__name__,
                "process_alive": False,
                "restart_after_seconds": delay,
            }

    def _launch(self, child: ManagedChild) -> None:
        child.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        if child.name == "kalshi_sdk_ws_shadow":
            try:
                environment = production_runtime_environment(self.settings, base=environment)
            except KalshiGatewayError:
                # Let the isolated child publish its typed fail-closed status;
                # an unavailable credential must not take down Supervisor.
                for name in (
                    "KALSHI_DEMO",
                    "LIVE15_KALSHI_DEMO_API_KEY_ID",
                    "LIVE15_KALSHI_DEMO_API_KEY_ID_FILE",
                    "LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH",
                ):
                    environment.pop(name, None)
        with child.stdout_path.open("ab") as stdout, child.stderr_path.open("ab") as stderr:
            child.launcher = self._popen(
                [sys.executable, "-m", child.module],
                cwd=self.root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                creationflags=WINDOWS_BACKGROUND_FLAGS if os.name == "nt" else 0,
            )
        logger.info(
            "Runtime component launched",
            extra={
                "event": "runtime_component_launched",
                "component": child.name,
                "launcher_pid": child.launcher.pid,
            },
        )

    def _component_projection(self, child: ManagedChild) -> dict[str, object]:
        payload = self._read_component(child.status_path)
        pid = int(payload.get("pid", 0)) if payload else 0
        heartbeat = _aware_timestamp(payload.get("last_heartbeat") if payload else None)
        age = (datetime.now(UTC) - heartbeat).total_seconds() if heartbeat else None
        alive = pid > 0 and process_alive(pid)
        status = str(payload.get("status", "STOPPED")) if payload else "STOPPED"
        health_state = "HEALTHY" if alive else "PROCESS_MISSING"
        if child.name == "control_center":
            health_state = self._control_center_health(payload, pid, age, alive)
            alive = health_state == "HEALTHY"
            status = "RUNNING" if alive else "RESTART_REQUIRED"
        if alive and age is not None and age > 15:
            status = "STALE"
        projection = {
            "status": status,
            "pid": pid or None,
            "started_at": payload.get("started_at") if payload else None,
            "last_heartbeat": payload.get("last_heartbeat") if payload else None,
            "heartbeat_age_seconds": max(0.0, age) if age is not None else None,
            "last_error": payload.get("last_error") if payload else None,
            "process_alive": alive,
            "expected_mode": payload.get("expected_mode") if payload else None,
            "status_path": str(child.status_path.resolve()),
            "receipt_path": str(
                (
                    self.runtime
                    / {
                        "kalshi_sdk_ws_shadow": "kalshi-sdk-ws-shadow.pid",
                        "paper_forward": "paper-forward.pid",
                        "current_trainable": "current-trainable.pid",
                        "control_center": "control-center.pid",
                    }[child.name]
                ).resolve()
            ),
            "log_path": str(child.stdout_path.resolve()),
            "launcher_pid": child.launcher.pid if child.launcher is not None else None,
            "health_state": health_state,
        }
        if child.name == "kalshi_sdk_ws_shadow" and payload:
            for key in (
                "connected_status",
                "synchronized_count",
                "subscribed_assets",
                "parity_status",
                "recent_mismatch_count",
                "recent_gap_count",
                "rollover_count",
            ):
                projection[key] = payload.get(key)
        return projection

    def _control_center_health(
        self, payload: dict[str, object] | None, pid: int, age: float | None, alive: bool
    ) -> str:
        if not payload or pid <= 0:
            return "PROCESS_MISSING"
        expected_start = payload.get("process_start_time")
        expected_executable = payload.get("expected_executable")
        if not isinstance(expected_start, str) or not isinstance(expected_executable, str):
            return "STALE_PID"
        if not alive:
            return "PROCESS_MISSING"
        identity = self._identity_lookup(pid)
        if identity is None:
            return "DEGRADED"
        if identity.get("process_start_time") != expected_start:
            return "PID_REUSED"
        if os.path.normcase(identity.get("executable", "")) != os.path.normcase(
            expected_executable
        ):
            return "COMMAND_MISMATCH"
        if age is None or age > self.settings.ui_heartbeat_stale_seconds:
            return "HEARTBEAT_STALE"
        host = payload.get("listen_host")
        port = payload.get("listen_port")
        if not isinstance(host, str) or not isinstance(port, int):
            return "PORT_NOT_LISTENING"
        return "HEALTHY" if self._control_center_probe(host, port) else "HEALTH_ENDPOINT_FAILED"

    @staticmethod
    def _expected_mode(name: str) -> str:
        return {
            "kalshi_sdk_ws_shadow": "SDK_WS_SHADOW_NO_RECORDER_WRITES",
            "paper_forward": "PAPER_SHADOW_LOCAL_ONLY",
            "current_trainable": "INCREMENTAL_CURRENT_TRAINABLE_NO_TRAINING",
            "control_center": "LOCALHOST_READ_ONLY_WITH_BOUNDED_RECORDER_CONTROL",
        }[name]

    def _first_fill_status(self) -> dict[str, object]:
        path = self.runtime / "demo_first_fill_status.json"
        payload = self._read_component(path)
        pid = int(payload.get("pid", 0)) if payload else 0
        heartbeat = _aware_timestamp(payload.get("last_heartbeat") if payload else None)
        age = (datetime.now(UTC) - heartbeat).total_seconds() if heartbeat else None
        return {
            "status": str(payload.get("status", "DISABLED")) if payload else "DISABLED",
            "pid": pid or None,
            "started_at": payload.get("started_at") if payload else None,
            "last_heartbeat": payload.get("last_heartbeat") if payload else None,
            "heartbeat_age_seconds": max(0.0, age) if age is not None else None,
            "last_error": payload.get("last_error") if payload else None,
            "process_alive": pid > 0 and process_alive(pid),
            # The persisted First-Fill status may describe a historical user
            # launch.  It must not imply current Demo-write availability after
            # the independently verified provider-side write blocker.
            "expected_mode": DEMO_REAL_WRITE_FROZEN_PROVIDER_BLOCKER,
            "historical_execution_mode": (
                str(payload.get("execution_mode", "DRY_RUN_WRITE_DISABLED"))
                if payload
                else "DISABLED_WRITE_DISABLED"
            ),
            "demo_real_write_state": DEMO_REAL_WRITE_FROZEN_PROVIDER_BLOCKER,
            "status_path": str(path.resolve()),
            "receipt_path": str((self.runtime / "demo_first_fill_worker.lock").resolve()),
            "log_path": str((self.logs / "demo_first_fill_worker.log").resolve()),
            "post_count": int(payload.get("post_count", 0)) if payload else 0,
            "launch_source": payload.get("launch_source", "UNKNOWN") if payload else "UNKNOWN",
            "launcher_name": payload.get("launcher_name") if payload else None,
            "parent_pid": payload.get("parent_pid") if payload else None,
            "parent_process": payload.get("parent_process") if payload else None,
        }

    @staticmethod
    def _read_component(path: Path) -> dict[str, object] | None:
        try:
            return read_json(path, maximum_bytes=256 * 1024)
        except RuntimeStatusError:
            return None


def _aware_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _configure_log(path: Path) -> logging.FileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handler


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="live15-runtime-supervisor")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--stop", action="store_true")
    actions.add_argument("--status", action="store_true")
    arguments = parser.parse_args(argv)
    root = Path.cwd().resolve()
    runtime = root / "runtime"
    control_path = runtime / "runtime-supervisor-control.json"
    status_path = runtime / "runtime-supervisor-status.json"
    if arguments.stop:
        atomic_json(
            control_path,
            {"desired": "stopped", "updated_at": utc_timestamp(), "requested_by": "operator"},
        )
        print(json.dumps({"status": "STOP_REQUESTED"}, sort_keys=True))
        return
    if arguments.status:
        print(json.dumps(read_json(status_path) or {"status": "NOT_RUNNING"}, sort_keys=True))
        return
    lease = RuntimePidLease(runtime / "runtime-supervisor.pid")
    try:
        lease.acquire()
    except RuntimeStatusError:
        owner = None
        try:
            owner = int((runtime / "runtime-supervisor.pid").read_text(encoding="ascii").strip())
        except (FileNotFoundError, OSError, ValueError):
            pass
        print(json.dumps({"status": "ALREADY_RUNNING", "pid": owner}, sort_keys=True))
        return
    handler = _configure_log(root / "logs" / "runtime_supervisor.log")
    try:
        RuntimeSupervisor(load_settings(), root=root).run()
    finally:
        lease.release()
        logger.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    main(sys.argv[1:])
