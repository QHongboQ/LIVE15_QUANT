"""Read-only typed service boundary for the LIVE15 Control Center."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from live15_quant.config import Settings
from live15_quant.control_center_models import (
    Availability,
    CoverageResponse,
    HealthResponse,
    MarketResponse,
    RecorderControlResponse,
    RecorderEventResponse,
    RecorderState,
    SystemResponse,
)
from live15_quant.control_center_store import DashboardReadStore
from live15_quant.models import Asset, RecorderEventSeverity
from live15_quant.recorder_control import RecorderProcessController


class ControlCenterService:
    """Expose bounded status projections without credentials or write operations."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        controller: RecorderProcessController | None = None,
    ) -> None:
        self.settings = settings
        self.store = DashboardReadStore(
            settings.recorder_data_path,
            settings.feature_store_path,
            coinbase_stale_seconds=settings.recorder_coinbase_stale_seconds,
            pyth_stale_seconds=settings.recorder_pyth_stale_seconds,
            secondary_stale_seconds=settings.recorder_secondary_stale_seconds,
        )
        self._clock = clock
        self._monotonic = monotonic
        self._coverage_lock = threading.Lock()
        self._coverage_cached_at: float | None = None
        self._coverage_cache: CoverageResponse | None = None
        self._control_lock = threading.Lock()
        if controller is not None:
            self.controller = controller
        else:
            try:
                self.controller = RecorderProcessController(settings)
            except ValueError:
                self.controller = None

    def health(self) -> HealthResponse:
        path = self.settings.recorder_health_path
        try:
            if path.stat().st_size > 256 * 1024:
                raise ValueError("health heartbeat exceeds bounded size")
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("health heartbeat must be an object")
            observed = datetime.fromisoformat(str(raw["observed_at"]))
            if observed.tzinfo is None or observed.utcoffset() is None:
                raise ValueError("health heartbeat timestamp must be timezone-aware")
            observed = observed.astimezone(UTC)
            age = max(0.0, (self._clock() - observed).total_seconds())
            stale = age > self.settings.ui_heartbeat_stale_seconds
            response = HealthResponse(
                status=str(raw.get("status", "unknown")),
                recorder_state=RecorderState.STALE if stale else RecorderState.RUNNING,
                heartbeat_status=Availability.STALE if stale else Availability.AVAILABLE,
                heartbeat_age_seconds=age,
                observed_at=observed,
                uptime_seconds=self._optional_float(raw.get("uptime_seconds")),
                database_bytes=self._optional_int(raw.get("database_bytes")),
                wal_bytes=self._optional_int(raw.get("wal_bytes")),
                written_records=self._optional_int(raw.get("written_records")),
                current_markets=self._string_map(raw.get("current_markets"), optional=True),
                active_settlement_followups=self._optional_int(
                    raw.get("active_settlement_followups")
                ),
                last_finalized_settlement=self._string_map(raw.get("last_finalized_settlement")),
                retry_counts=self._int_map(raw.get("retry_counts")),
                source_failures=self._string_map(raw.get("source_failures")),
                stale_sources=self._string_list(raw.get("stale_sources")),
                worker_progress=self._datetime_map(raw.get("worker_progress")),
                worker_progress_age_seconds=self._float_map(raw.get("worker_progress_age_seconds")),
                stale_workers=self._string_list(raw.get("stale_workers")),
                event_loop_lag_seconds=self._optional_float(raw.get("event_loop_lag_seconds")),
                fatal_task=self._optional_string(raw.get("fatal_task")),
                fatal_error_type=self._optional_string(raw.get("fatal_error_type")),
                kalshi_ws_connection_state=str(raw.get("kalshi_ws_connection_state", "disabled")),
                kalshi_ws_synchronized_markets=self._string_map(
                    raw.get("kalshi_ws_synchronized_markets"), optional=True
                ),
                kalshi_ws_synchronized_count=(
                    self._optional_int(raw.get("kalshi_ws_synchronized_count")) or 0
                ),
                kalshi_ws_book_age_seconds=self._float_map(raw.get("kalshi_ws_book_age_seconds")),
                kalshi_ws_seq_gaps=self._optional_int(raw.get("kalshi_ws_seq_gaps")) or 0,
                kalshi_ws_resync_count=self._optional_int(raw.get("kalshi_ws_resync_count")) or 0,
                kalshi_ws_reconnect_count=(
                    self._optional_int(raw.get("kalshi_ws_reconnect_count")) or 0
                ),
                kalshi_ws_queue_high_watermark=(
                    self._optional_int(raw.get("kalshi_ws_queue_high_watermark")) or 0
                ),
                kalshi_ws_queue_capacity=(
                    self._optional_int(raw.get("kalshi_ws_queue_capacity")) or 0
                ),
                kalshi_ws_queue_depth=(self._optional_int(raw.get("kalshi_ws_queue_depth")) or 0),
                kalshi_ws_queue_enqueued=(
                    self._optional_int(raw.get("kalshi_ws_queue_enqueued")) or 0
                ),
                kalshi_ws_queue_dequeued=(
                    self._optional_int(raw.get("kalshi_ws_queue_dequeued")) or 0
                ),
                kalshi_ws_queue_full_waits=(
                    self._optional_int(raw.get("kalshi_ws_queue_full_waits")) or 0
                ),
                kalshi_ws_queue_dropped=(
                    self._optional_int(raw.get("kalshi_ws_queue_dropped")) or 0
                ),
                kalshi_ws_queue_max_backlog_seconds=(
                    self._optional_float(raw.get("kalshi_ws_queue_max_backlog_seconds")) or 0.0
                ),
                kalshi_ws_queue_above_50_seconds=(
                    self._optional_float(raw.get("kalshi_ws_queue_above_50_seconds")) or 0.0
                ),
                kalshi_ws_queue_above_75_seconds=(
                    self._optional_float(raw.get("kalshi_ws_queue_above_75_seconds")) or 0.0
                ),
                kalshi_ws_queue_above_90_seconds=(
                    self._optional_float(raw.get("kalshi_ws_queue_above_90_seconds")) or 0.0
                ),
                kalshi_ws_receive_persist_latency_ms=self._optional_string(
                    raw.get("kalshi_ws_receive_persist_latency_ms")
                ),
                kalshi_rest_fallback_status=str(
                    raw.get("kalshi_rest_fallback_status", "unavailable")
                ),
            )
            return self._apply_managed_state(response)
        except FileNotFoundError:
            return self._apply_managed_state(
                HealthResponse(
                    status="unavailable",
                    recorder_state=RecorderState.STOPPED,
                    heartbeat_status=Availability.UNAVAILABLE,
                )
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return HealthResponse(
                status="error",
                recorder_state=RecorderState.ERROR,
                heartbeat_status=Availability.ERROR,
                source_failures={"health": "malformed_heartbeat"},
            )

    def _apply_managed_state(self, health: HealthResponse) -> HealthResponse:
        if self.controller is None:
            return health
        managed = self.controller.status()
        state = RecorderState(managed.state.value)
        if state is RecorderState.RUNNING and health.heartbeat_status is Availability.STALE:
            state = RecorderState.STALE
        return health.model_copy(
            update={
                "recorder_state": state,
                "status": managed.message if state is not RecorderState.RUNNING else health.status,
            }
        )

    def markets(self) -> list[MarketResponse]:
        health = self.health()
        return [
            MarketResponse.model_validate(
                self.store.asset(asset, self._clock(), health.current_markets.get(asset.value))
            )
            for asset in Asset
        ]

    def market(self, asset: Asset) -> MarketResponse:
        health = self.health()
        payload = self.store.asset(asset, self._clock(), health.current_markets.get(asset.value))
        payload["previous_events"] = self.store.previous_events(asset)
        return MarketResponse.model_validate(payload)

    def coverage(self) -> CoverageResponse:
        # Coverage aggregates immutable completed builds and finalized settlements. Cache
        # briefly so future browser polling cannot repeatedly scan growing tables.
        with self._coverage_lock:
            now = self._monotonic()
            if (
                self._coverage_cache is not None
                and self._coverage_cached_at is not None
                and 0 <= now - self._coverage_cached_at < 30
            ):
                return self._coverage_cache
            response = CoverageResponse.model_validate(self.store.coverage())
            self._coverage_cache = response
            self._coverage_cached_at = now
            return response

    def system(self) -> SystemResponse:
        health = self.health()
        return SystemResponse(
            generated_at=self._clock(),
            recorder_state=health.recorder_state,
            raw_store=(
                Availability.AVAILABLE
                if self.settings.recorder_data_path.is_file()
                else Availability.UNAVAILABLE
            ),
            feature_store=(
                Availability.AVAILABLE
                if self.settings.feature_store_path.is_file()
                else Availability.UNAVAILABLE
            ),
        )

    def recorder_action(self, action: str) -> RecorderControlResponse:
        if self.controller is None:
            raise RuntimeError("managed recorder control is unavailable for this configuration")
        methods = {
            "start": self.controller.start,
            "pause": self.controller.pause,
            "resume": self.controller.resume,
        }
        method = methods.get(action)
        if method is None:
            raise ValueError("unsupported recorder action")
        with self._control_lock:
            result = method()
        return RecorderControlResponse(
            state=RecorderState(result.state.value), message=result.message
        )

    def recorder_events(
        self,
        *,
        limit: int = 100,
        severity: RecorderEventSeverity | None = None,
        asset: Asset | None = None,
        source: str | None = None,
        since: datetime | None = None,
    ) -> list[RecorderEventResponse]:
        rows = self.store.recorder_events(
            limit=limit,
            severity=severity.value if severity is not None else None,
            asset=asset,
            source=source,
            since=since,
        )
        return [
            RecorderEventResponse(
                timestamp=datetime.fromisoformat(str(row["observed_timestamp"])),
                severity=str(row["severity"]),
                event_type=str(row["event_type"]),
                asset=self._optional_string(row["asset"]),
                source=self._optional_string(row["source"]),
                error_type=self._optional_string(row["error_type"]),
                message=str(row["message"]),
            )
            for row in rows
        ]

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _optional_float(value: object) -> float | None:
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        return None

    @staticmethod
    def _float_map(value: object) -> dict[str, float]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, float] = {}
        for key, item in value.items():
            if (
                isinstance(key, str)
                and isinstance(item, (int, float))
                and not isinstance(item, bool)
            ):
                result[key] = float(item)
        return result

    @staticmethod
    def _datetime_map(value: object) -> dict[str, datetime]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, datetime] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                continue
            try:
                parsed = datetime.fromisoformat(item)
            except ValueError:
                continue
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                result[key] = parsed.astimezone(UTC)
        return result

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _string_map(value: object, *, optional: bool = False) -> dict[str, str | None]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, str | None] = {}
        for key, item in value.items():
            if isinstance(key, str) and (isinstance(item, str) or (optional and item is None)):
                result[key] = item
        return result

    @staticmethod
    def _int_map(value: object) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        return {
            key: item
            for key, item in value.items()
            if isinstance(key, str) and isinstance(item, int) and not isinstance(item, bool)
        }

    @staticmethod
    def _string_list(value: object) -> list[str]:
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
