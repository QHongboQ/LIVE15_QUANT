"""Supervisor-owned localhost Control Center with atomic runtime heartbeat."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import uvicorn

from live15_quant.config import load_settings
from live15_quant.control_center import LOCAL_HOST, create_app
from live15_quant.logging_config import configure_logging
from live15_quant.runtime_status import (
    RuntimePidLease,
    atomic_json,
    component_status,
    read_json,
    utc_timestamp,
)


async def _monitor_runtime(
    *,
    server: uvicorn.Server,
    status: dict[str, object],
    status_path: Path,
    control_path: Path,
) -> None:
    """Keep the service receipt fresh or force a bounded process restart.

    A failed status write must not leave a responsive HTTP process whose
    supervisor receipt is permanently stale.  Stopping Uvicorn lets the
    supervisor apply its normal bounded restart policy.
    """

    try:
        while not server.should_exit:
            control = read_json(control_path)
            if control is not None and control.get("desired") == "stopped":
                server.should_exit = True
                break
            status.update(
                {
                    "status": "RUNNING" if server.started else "STARTING",
                    "last_heartbeat": utc_timestamp(),
                    "last_error": None,
                    "process_alive": True,
                }
            )
            atomic_json(status_path, status)
            await asyncio.sleep(2)
    except Exception:
        server.should_exit = True
        raise


async def _run() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    root = Path.cwd().resolve()
    runtime = root / "runtime"
    lease = RuntimePidLease(runtime / "control-center.pid")
    status_path = runtime / "control-center-status.json"
    control_path = runtime / "runtime-supervisor-control.json"
    started = utc_timestamp()
    lease.acquire()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host=LOCAL_HOST,
            port=settings.ui_port,
            log_config=None,
        )
    )
    status = component_status(
        name="control_center",
        status="STARTING",
        pid=os.getpid(),
        started_at=started,
        last_heartbeat=started,
        expected_mode="LOCALHOST_READ_ONLY_WITH_BOUNDED_RECORDER_CONTROL",
        working_directory=root,
        log_path=root / "logs" / "control_center.log",
        extra={"listen_host": LOCAL_HOST, "listen_port": settings.ui_port},
    )
    atomic_json(status_path, status)

    monitor_task = asyncio.create_task(
        _monitor_runtime(
            server=server,
            status=status,
            status_path=status_path,
            control_path=control_path,
        ),
        name="control-center-runtime-monitor",
    )
    try:
        await server.serve()
        status.update(
            {
                "status": "STOPPED",
                "last_heartbeat": utc_timestamp(),
                "stop_reason": "SUPERVISOR_STOP_REQUESTED"
                if server.should_exit
                else "SERVER_EXITED",
            }
        )
        atomic_json(status_path, status)
    except Exception as error:
        status.update(
            {
                "status": "ERROR",
                "last_heartbeat": utc_timestamp(),
                "last_error": type(error).__name__,
            }
        )
        atomic_json(status_path, status)
        raise
    finally:
        server.should_exit = True
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        lease.release()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
