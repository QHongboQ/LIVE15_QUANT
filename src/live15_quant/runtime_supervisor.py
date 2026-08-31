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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from live15_quant.config import Settings, load_settings
from live15_quant.recorder_control import WINDOWS_BACKGROUND_FLAGS, process_alive
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
    automatic: bool = False
    paused_reason: str | None = None


class RuntimeSupervisor:
    """Supervise only explicitly registered auxiliary child processes.

    Recorder and Control Center are independent WinSW services.  This class is
    deliberately unable to start, stop, or restart either service.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        root: Path | None = None,
        monotonic: Any = time.monotonic,
        sleep: Any = time.sleep,
        popen: Any = subprocess.Popen,
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
        self.started_at = utc_timestamp()
        self.children = {
            "paper_forward": ManagedChild(
                "paper_forward",
                "live15_quant.managed_paper",
                self.runtime / "paper-forward-status.json",
                self.logs / "paper_forward_worker.log",
                self.logs / "paper_forward_worker.error.log",
                automatic=False,
                paused_reason="PAUSED_BY_DESIGN",
            ),
        }
        self._last_components: dict[str, dict[str, object]] = {}

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
        components = {
            name: self._ensure_child(child, allowed=child.automatic)
            for name, child in self.children.items()
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
        # Auxiliary workers observe the shared stop receipt and exit cleanly.
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

    def _ensure_child(self, child: ManagedChild, *, allowed: bool) -> dict[str, object]:
        projection = self._component_projection(child)
        if not allowed:
            projection.update(
                {
                    "status": child.paused_reason or "ON_DEMAND",
                    "last_error": None,
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
            "receipt_path": str((self.runtime / "paper-forward.pid").resolve()),
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
            "receipt_path": str((self.runtime / "paper-forward.pid").resolve()),
            "log_path": str(child.stdout_path.resolve()),
            "launcher_pid": child.launcher.pid if child.launcher is not None else None,
        }
        return projection

    @staticmethod
    def _expected_mode(name: str) -> str:
        assert name == "paper_forward"
        return "PAPER_SHADOW_LOCAL_ONLY"

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
