"""Background managed-recorder entry point used only by Control Center."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from live15_quant.cli import _periodic_dataset_build
from live15_quant.config import Settings, load_settings
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

_HEALTH_COUNTER_TABLES = frozenset(
    {
        "kalshi_market_lifecycle",
        "kalshi_prediction_quotes",
        "coinbase_ticks",
        "underlying_observations",
        "secondary_underlying_observations",
        "kalshi_ws_orderbook_events",
        "kalshi_ws_book_checkpoints",
        "kalshi_settlements",
        "kalshi_settlement_conflicts",
        "data_gaps",
    }
)


@dataclass(frozen=True, slots=True)
class StartupHealthBaseline:
    row_counts: dict[str, int]
    active_settlement_followups: int
    integrity: str


class StartupCancelled(RuntimeError):
    """The control plane requested Pause while the child was still starting."""


@dataclass(slots=True)
class StartupDiagnostics:
    path: Path
    monotonic: Callable[[], float] = time.monotonic
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    _started_monotonic: float = field(init=False)
    phases: dict[str, dict[str, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._started_monotonic = self.monotonic()
        self.record("process_started", 0.0)

    def record(self, phase: str, duration_seconds: float) -> None:
        self.phases[phase] = {
            "elapsed_seconds": max(0.0, self.monotonic() - self._started_monotonic),
            "duration_seconds": max(0.0, duration_seconds),
        }
        payload = {
            "schema_version": 1,
            "pid": os.getpid(),
            "started_at": self.started_at.isoformat(),
            "phase": phase,
            "phases": self.phases,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)


def _last_verified_health(settings: Settings) -> StartupHealthBaseline | None:
    """Reuse the last offline/startup verification without scanning the active raw DB.

    A graceful recorder restart does not rewrite raw truth. The previous heartbeat is
    therefore the bounded source for health counters and the last completed integrity
    result; dedicated snapshot/offline tooling owns future full integrity scans.
    """

    try:
        if settings.recorder_health_path.stat().st_size > 256 * 1024:
            return None
        payload = json.loads(settings.recorder_health_path.read_text(encoding="utf-8"))
        if payload.get("integrity") != "ok":
            return None
        raw_counts = payload.get("row_counts")
        if not isinstance(raw_counts, dict):
            return None
        counts = {
            str(table): int(value)
            for table, value in raw_counts.items()
            if isinstance(table, str) and isinstance(value, int) and value >= 0
        }
        if len(counts) != len(raw_counts) or not _HEALTH_COUNTER_TABLES.issubset(counts):
            return None
        followups = payload.get("active_settlement_followups", 0)
        if isinstance(followups, bool) or not isinstance(followups, int) or followups < 0:
            return None
        return StartupHealthBaseline(counts, followups, "ok")
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


async def _watch_control(
    recorder: KalshiNativeRecorder, controller: RecorderProcessController
) -> None:
    while True:
        if controller.desired_state() == "paused":
            recorder.request_stop()
            return
        await asyncio.sleep(0.25)


async def _run(startup: StartupDiagnostics) -> None:
    settings = load_settings()
    controller = RecorderProcessController(settings)
    if controller.desired_state() == "paused":
        raise StartupCancelled("startup cancelled before database open")
    verified_health = _last_verified_health(settings)

    def observe_phase(phase: str, duration_seconds: float) -> None:
        startup.record(phase, duration_seconds)
        if (
            phase
            in {
                "db_open",
                "wal_recovery",
                "schema_check",
                "lifecycle_recovery",
                "cursor_recovery",
                "settlement_recovery",
                "gap_recovery",
                "recorder_state_ready",
            }
            and controller.desired_state() == "paused"
        ):
            raise StartupCancelled(f"startup cancelled after {phase}")

    database_existed = settings.recorder_data_path.exists()
    with RecorderStore(
        settings.recorder_data_path,
        startup_phase_observer=observe_phase,
    ) as store:
        counts = (
            store.bounded_row_count_estimates()
            if verified_health is None
            else verified_health.row_counts
        )
        recovered = database_existed
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
        recorder = KalshiNativeRecorder(
            settings,
            store,
            initial_row_counts=counts,
            # A prior heartbeat is exact only at its own observed_at. It is a safe
            # bounded baseline, but a hard-stop interval may contain later commits.
            # Only a newly created empty raw store has a complete startup count.
            initial_row_counts_complete=not database_existed,
            initial_active_settlement_followups=(
                None if verified_health is None else verified_health.active_settlement_followups
            ),
            last_verified_integrity=(
                "not_checked" if verified_health is None else verified_health.integrity
            ),
            startup_phase_observer=observe_phase,
        )
        if controller.desired_state() == "paused":
            raise StartupCancelled("startup cancelled before worker creation")
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
    startup = StartupDiagnostics(settings.recorder_control_path.with_name("recorder-startup.json"))
    desired = "running"
    try:
        lease_started = time.monotonic()
        with RecorderPidLease(settings.recorder_pid_path):
            startup.record("lease_acquisition", time.monotonic() - lease_started)
            asyncio.run(_run(startup))
            desired = controller.desired_state()
            with RecorderStore(settings.recorder_data_path) as store:
                store.append_recorder_event(
                    observed_timestamp=recorder_time(),
                    severity=RecorderEventSeverity.INFO,
                    event_type=RecorderEventType.RECORDER_STOPPED,
                    source="managed_recorder",
                    message="Recorder stopped gracefully",
                )
    except StartupCancelled:
        desired = "paused"
        startup.record("startup_cancelled", 0.0)
        controller.write_child_state(
            "paused", ManagedRecorderState.PAUSED, "startup cancelled gracefully"
        )
    except Exception as error:
        startup.record("startup_failed", 0.0)
        logger.exception(
            "Managed recorder failed",
            extra={"event": "managed_recorder_failed", "error_type": type(error).__name__},
        )
        desired = controller.desired_state()
        controller.write_child_state(desired, ManagedRecorderState.ERROR, "recorder failed")
        raise
    else:
        state = ManagedRecorderState.PAUSED if desired == "paused" else ManagedRecorderState.STOPPED
        controller.write_child_state(
            desired, state, "collection is paused" if desired == "paused" else "recorder stopped"
        )


if __name__ == "__main__":
    main()
