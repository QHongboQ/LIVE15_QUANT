"""Single-instance Windows runtime supervisor for LIVE15 data services.

The supervisor owns process lifecycle only. It has no credential loading, model,
risk, Demo execution, or Production execution capability.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from live15_quant.config import Settings, load_settings
from live15_quant.runtime_status import (
    RuntimePidLease,
    RuntimeStatusError,
    atomic_json,
    read_json,
    utc_timestamp,
)
logger = logging.getLogger(__name__)


class RuntimeSupervisor:
    """Retained zero-child legacy status boundary pending service retirement.

    Recorder and Control Center are independent WinSW services.  This class is
    deliberately unable to start, stop, or restart any application workload.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        root: Path | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.settings = settings
        self.root = (root or Path.cwd()).resolve()
        self.runtime = self.root / "runtime"
        self.logs = self.root / "logs"
        self.status_path = self.runtime / "runtime-supervisor-status.json"
        self.control_path = self.runtime / "runtime-supervisor-control.json"
        self._sleep = sleep
        self.started_at = utc_timestamp()
        self.children: dict[str, object] = {}

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
        components: dict[str, dict[str, object]] = {}
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
                "components": {},
            },
        )


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
