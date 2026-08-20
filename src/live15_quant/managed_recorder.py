"""Background managed-recorder entry point used only by Control Center."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from live15_quant.cli import _periodic_dataset_build
from live15_quant.config import load_settings
from live15_quant.logging_config import configure_logging
from live15_quant.models import RecorderEventSeverity, RecorderEventType
from live15_quant.native_recorder import KalshiNativeRecorder
from live15_quant.recorder_control import (
    ManagedRecorderState,
    RecorderPidLease,
    RecorderProcessController,
)
from live15_quant.storage import RecorderStore

logger = logging.getLogger(__name__)


async def _watch_control(
    recorder: KalshiNativeRecorder, controller: RecorderProcessController
) -> None:
    while True:
        if controller.desired_state() == "paused":
            recorder.request_stop()
            return
        await asyncio.sleep(0.25)


async def _run() -> None:
    settings = load_settings()
    controller = RecorderProcessController(settings)
    with RecorderStore(settings.recorder_data_path) as store:
        recovered = any(store.row_counts().values())
        store.append_recorder_event(
            observed_timestamp=recorder_time(),
            severity=RecorderEventSeverity.INFO,
            event_type=(
                RecorderEventType.RECORDER_RECOVERED
                if recovered
                else RecorderEventType.RECORDER_STARTED
            ),
            source="managed_recorder",
            message="Recorder resumed from SQLite" if recovered else "Recorder started",
        )
        recorder = KalshiNativeRecorder(settings, store)
        controller.write_child_state(
            "running", ManagedRecorderState.STARTING, "awaiting recorder heartbeat"
        )
        recorder_task = asyncio.create_task(recorder.run(), name="managed-native-recorder")
        watcher = asyncio.create_task(_watch_control(recorder, controller), name="control-watcher")
        snapshot = (
            asyncio.create_task(_periodic_dataset_build(settings), name="periodic-dataset")
            if settings.dataset_build_interval_seconds is not None
            else None
        )
        try:
            await recorder_task
        finally:
            watcher.cancel()
            if snapshot is not None:
                snapshot.cancel()
            await asyncio.gather(
                watcher, *(item for item in (snapshot,) if item is not None), return_exceptions=True
            )


def recorder_time() -> datetime:
    return datetime.now(UTC)


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    controller = RecorderProcessController(settings)
    desired = "running"
    try:
        with RecorderPidLease(settings.recorder_pid_path):
            asyncio.run(_run())
            desired = controller.desired_state()
            with RecorderStore(settings.recorder_data_path) as store:
                store.append_recorder_event(
                    observed_timestamp=recorder_time(),
                    severity=RecorderEventSeverity.INFO,
                    event_type=RecorderEventType.RECORDER_STOPPED,
                    source="managed_recorder",
                    message="Recorder stopped gracefully",
                )
    except Exception as error:
        logger.exception(
            "Managed recorder failed",
            extra={"event": "managed_recorder_failed", "error_type": type(error).__name__},
        )
        controller.write_child_state("running", ManagedRecorderState.ERROR, "recorder failed")
        raise
    else:
        state = ManagedRecorderState.PAUSED if desired == "paused" else ManagedRecorderState.STOPPED
        controller.write_child_state(
            desired, state, "collection is paused" if desired == "paused" else "recorder stopped"
        )


if __name__ == "__main__":
    main()
