"""Supervisor-owned persistent Paper/forward runtime with atomic health facts."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from live15_quant.config import load_settings
from live15_quant.forward_shadow import ForwardShadowRuntime
from live15_quant.logging_config import configure_logging
from live15_quant.runtime_status import (
    RuntimePidLease,
    atomic_json,
    component_status,
    read_json,
    utc_timestamp,
)
from live15_quant.shadow_execution import DEMO_REAL_WRITE_FROZEN_PROVIDER_BLOCKER

logger = logging.getLogger(__name__)


def _stop_requested(path: Path) -> bool:
    payload = read_json(path)
    return payload is not None and payload.get("desired") == "stopped"


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    root = Path.cwd().resolve()
    runtime = root / "runtime"
    lease = RuntimePidLease(runtime / "paper-forward.pid")
    status_path = runtime / "paper-forward-status.json"
    control_path = runtime / "runtime-supervisor-control.json"
    log_path = root / "logs" / "paper_forward_worker.log"
    started = utc_timestamp()
    lease.acquire()
    status = component_status(
        name="paper_forward",
        status="STARTING",
        pid=os.getpid(),
        started_at=started,
        last_heartbeat=started,
        expected_mode="PAPER_SHADOW_LOCAL_ONLY",
        working_directory=root,
        log_path=log_path,
        extra={"demo_real_write_state": DEMO_REAL_WRITE_FROZEN_PROVIDER_BLOCKER},
    )
    atomic_json(status_path, status)
    try:
        with ForwardShadowRuntime(settings) as shadow:
            while True:
                if _stop_requested(control_path):
                    status.update(
                        {
                            "status": "STOPPED",
                            "last_heartbeat": utc_timestamp(),
                            "process_alive": True,
                            "stop_reason": "SUPERVISOR_STOP_REQUESTED",
                        }
                    )
                    atomic_json(status_path, status)
                    return
                summary = shadow.run_once()
                status.update(
                    {
                        "status": "RUNNING",
                        "last_heartbeat": utc_timestamp(),
                        "last_error": None,
                        "process_alive": True,
                        "metrics": summary,
                    }
                )
                atomic_json(status_path, status)
                logger.info(
                    "Managed Paper forward cycle",
                    extra={"event": "managed_paper_cycle", **summary},
                )
                time.sleep(settings.forward_shadow_poll_interval_seconds)
    except KeyboardInterrupt:
        status.update(
            {
                "status": "STOPPED",
                "last_heartbeat": utc_timestamp(),
                "last_error": None,
                "stop_reason": "INTERRUPTED",
            }
        )
        atomic_json(status_path, status)
    except Exception as error:
        status.update(
            {
                "status": "ERROR",
                "last_heartbeat": utc_timestamp(datetime.now(UTC)),
                "last_error": type(error).__name__,
            }
        )
        atomic_json(status_path, status)
        raise
    finally:
        lease.release()


if __name__ == "__main__":
    main()
