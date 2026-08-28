"""Read-only typed service boundary for the LIVE15 Control Center."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from live15_quant.account_service import ProductionAccountService
from live15_quant.config import Settings
from live15_quant.control_center_models import (
    ArchiveResponse,
    Availability,
    CoverageResponse,
    DataResponse,
    HealthResponse,
    MarketResponse,
    OperationsResponse,
    RecorderControlAction,
    RecorderControlOutcome,
    RecorderControlResponse,
    RecorderEventResponse,
    RecorderState,
    ResearchDataResponse,
    RuntimeComponentResponse,
    StorageResponse,
    SystemResponse,
    TrainingResponse,
    WsArchiveHealth,
)
from live15_quant.control_center_store import DashboardReadStore
from live15_quant.market_sessions import MarketDataState, market_data_state, market_session
from live15_quant.models import Asset, RecorderEventSeverity
from live15_quant.recorder_control import RecorderProcessController, process_alive
from live15_quant.research_data_authority import ResearchDataAuthority
from live15_quant.runtime_status import RuntimeStatusError, read_json

_INTENTIONAL_AUXILIARY_STATUSES = frozenset({"ON_DEMAND", "PAUSED_BY_DESIGN"})


class ControlCenterService:
    """Expose bounded status projections without credentials or write operations."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        controller: RecorderProcessController | None = None,
        account_service: ProductionAccountService | None = None,
        research_authority: ResearchDataAuthority | None = None,
    ) -> None:
        self.settings = settings
        self.store = DashboardReadStore(
            settings.recorder_data_path,
            settings.feature_store_path,
            current_trainable_path=settings.current_trainable_path,
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
        self.account_service = account_service or ProductionAccountService(settings)
        self.research_authority = research_authority or ResearchDataAuthority(settings)
        if controller is not None:
            self.controller = controller
        else:
            try:
                self.controller = RecorderProcessController(settings)
            except ValueError:
                self.controller = None

    def account_profiles(self):
        return self.account_service.profiles()

    def account(self, profile: str = "production_primary"):
        return self.account_service.read(profile)

    def research_data(self) -> ResearchDataResponse:
        """Return aggregate research-source metadata, never research payloads or secrets."""

        return ResearchDataResponse.model_validate(
            self.research_authority.snapshot().to_public_dict()
        )

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
            checked_at = self._clock()
            age = max(0.0, (checked_at - observed).total_seconds())
            stale = age > self.settings.ui_heartbeat_stale_seconds
            source_failures = self._string_map(raw.get("source_failures"))
            stale_sources = self._string_list(raw.get("stale_sources"))
            market_closed_sources = self._string_list(raw.get("market_closed_sources"))
            underlying_states = self._string_map(raw.get("underlying_market_states"))
            last_underlying = self._datetime_map(raw.get("last_additional_underlying"))
            for asset in Asset:
                if market_session(asset) is None:
                    continue
                source = f"pyth:{asset.value}"
                state = market_data_state(
                    asset,
                    checked_at=checked_at,
                    latest_received=last_underlying.get(asset.value),
                    max_age=timedelta(seconds=self.settings.recorder_pyth_stale_seconds),
                    source_available=source not in source_failures,
                )
                underlying_states[asset.value] = state.value
                if state is MarketDataState.MARKET_CLOSED:
                    stale_sources = [value for value in stale_sources if value != source]
                    if source not in market_closed_sources:
                        market_closed_sources.append(source)
            stale_workers = self._string_list(raw.get("stale_workers"))
            raw_status = str(raw.get("status", "unknown"))
            if (
                raw_status == "degraded"
                and not source_failures
                and not stale_sources
                and not stale_workers
            ):
                raw_status = "healthy"
            response = HealthResponse(
                status=raw_status,
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
                source_failures=source_failures,
                stale_sources=stale_sources,
                market_closed_sources=market_closed_sources,
                underlying_market_states=underlying_states,
                worker_progress=self._datetime_map(raw.get("worker_progress")),
                worker_progress_age_seconds=self._float_map(raw.get("worker_progress_age_seconds")),
                stale_workers=stale_workers,
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
                ws_archive=self._ws_archive_health(raw.get("ws_archive")),
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
        responses: list[MarketResponse] = []
        for asset in Asset:
            payload = self.store.asset(
                asset, self._clock(), health.current_markets.get(asset.value)
            )
            if health.underlying_market_states.get(asset.value) == "market_closed":
                payload["underlying_status"] = "market_closed"
            responses.append(MarketResponse.model_validate(payload))
        return responses

    def market(self, asset: Asset) -> MarketResponse:
        health = self.health()
        payload = self.store.asset(asset, self._clock(), health.current_markets.get(asset.value))
        if health.underlying_market_states.get(asset.value) == "market_closed":
            payload["underlying_status"] = "market_closed"
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

    def training(self) -> TrainingResponse:
        payload = self.store.training()
        return TrainingResponse(generated_at=self._clock(), **payload)

    def data(self) -> DataResponse:
        health = self.health()
        pool = self.store.training()["raw_finalized_pool"]
        return DataResponse(
            generated_at=self._clock(),
            recorder_state=health.recorder_state,
            raw_store=Availability.AVAILABLE
            if self.settings.recorder_data_path.is_file()
            else Availability.UNAVAILABLE,
            finalized_events=pool.get("events"),
            finalized_assets=pool.get("assets"),
            source_as_of=pool.get("observed_at"),
            freshness=pool.get("status", "UNKNOWN"),
            notes=["Finalized settlement truth is read-only; trainability is shown separately."],
        )

    def archive(self) -> ArchiveResponse:
        health = self.health()
        archive = health.ws_archive
        state = "disabled" if not archive.enabled else "healthy"
        if archive.failed or archive.quarantined:
            state = "attention"
        compressed_saved, compression_percent = self._compression_savings(archive)
        return ArchiveResponse(
            generated_at=self._clock(),
            state=state,
            enabled=archive.enabled,
            poll_mode=archive.archive_poll_mode,
            next_poll_seconds=archive.archive_next_poll_seconds,
            verified_chunks=archive.verified,
            failed_chunks=archive.failed,
            waiting_chunks=archive.waiting_for_replay_baseline,
            quarantined_chunks=archive.quarantined,
            backlog_events=archive.archive_backlog_events,
            throughput_events_per_second=archive.archive_throughput_events_per_second,
            lag_seconds=archive.archive_lag_seconds,
            uncompressed_archive_bytes=archive.uncompressed,
            compressed_archive_bytes=archive.compressed,
            compression_ratio=archive.compression_ratio,
            compressed_bytes_saved=compressed_saved,
            compression_saving_percent=compression_percent,
            cold_archive_bytes=archive.cold_archive_bytes,
            purge_eligible_chunks=archive.eligible,
            purged_chunks=None,
            total_purged_events=archive.purged,
            purge_eligible_events=archive.eligible,
            last_purge_deleted_events=archive.last_purge_deleted_events,
            last_purge_duration_seconds=archive.last_purge_transaction_seconds,
            last_purge_reusable_bytes=archive.last_purge_reusable_bytes,
            notes=["Purge eligibility is a dry-run projection; destructive actions are absent."],
        )

    def storage(self) -> StorageResponse:
        health = self.health()
        archive = health.ws_archive
        state = archive.disk_threshold_state
        compressed_saved, compression_percent = self._compression_savings(archive)
        reclaimable = archive.freelist_reusable_bytes
        physical = archive.physical_database_bytes
        reclaimable_percent = (
            None
            if reclaimable is None or physical is None or physical <= 0
            else (reclaimable / physical) * 100
        )
        minimum_bytes = self.settings.ws_compaction_min_reclaim_bytes
        minimum_percent = float(self.settings.ws_compaction_min_reclaim_percent)
        if reclaimable is None or reclaimable_percent is None:
            compaction_status = "UNKNOWN"
        elif reclaimable >= minimum_bytes and reclaimable_percent >= minimum_percent:
            compaction_status = "ELIGIBLE"
        else:
            compaction_status = "NOT_ELIGIBLE"
        return StorageResponse(
            generated_at=self._clock(),
            state=state,
            disk_total_bytes=archive.disk_total_bytes,
            disk_free_bytes=archive.disk_free_bytes,
            hot_sqlite_bytes=archive.hot_sqlite_used_bytes,
            sqlite_reusable_bytes=reclaimable,
            physical_reclaimed_bytes=None,
            cold_archive_bytes=archive.cold_archive_bytes,
            wal_bytes=archive.wal_bytes if archive.wal_bytes is not None else health.wal_bytes,
            compression_saved_bytes=compressed_saved,
            compression_saving_percent=compression_percent,
            growth_bytes_per_day=archive.net_disk_growth_bytes_per_day,
            raw_ws_growth_bytes_per_hour=getattr(archive, "raw_ws_growth_bytes_per_hour", None),
            raw_ws_growth_bytes_per_day=getattr(archive, "raw_ws_growth_bytes_per_day", None),
            cold_archive_growth_bytes_per_hour=archive.cold_archive_growth_bytes_per_hour,
            cold_archive_growth_bytes_per_day=archive.cold_archive_growth_bytes_per_day,
            net_disk_growth_bytes_per_hour=archive.net_disk_growth_bytes_per_hour,
            net_disk_growth_bytes_per_day=archive.net_disk_growth_bytes_per_day,
            retention_seconds=archive.hot_retention_seconds,
            purge_eligible_chunks=archive.eligible,
            purged_chunks=None,
            total_purged_events=archive.purged,
            last_purge_deleted_events=archive.last_purge_deleted_events,
            last_purge_duration_seconds=archive.last_purge_transaction_seconds,
            last_purge_reusable_bytes=archive.last_purge_reusable_bytes,
            compaction_reclaimable_bytes=reclaimable,
            compaction_reclaimable_percent=reclaimable_percent,
            compaction_minimum_required_bytes=minimum_bytes,
            compaction_minimum_required_percent=minimum_percent,
            compaction_status=compaction_status,
            notes=[
                "Compression savings, SQLite reusable space, and physical reclaimed "
                "space are separate facts."
            ],
        )

    @staticmethod
    def _compression_savings(archive: WsArchiveHealth) -> tuple[int | None, float | None]:
        uncompressed = archive.uncompressed
        compressed = archive.compressed
        if (
            uncompressed is None
            or compressed is None
            or uncompressed <= 0
            or compressed < 0
            or compressed > uncompressed
        ):
            return None, None
        saved = uncompressed - compressed
        return saved, (saved / uncompressed) * 100

    def operations(self) -> OperationsResponse:
        health = self.health()
        events = self.recorder_events(limit=20)
        return OperationsResponse(
            generated_at=self._clock(),
            recorder_state=health.recorder_state,
            recorder_heartbeat=health.heartbeat_status,
            fatal_task=health.fatal_task,
            fatal_error_type=health.fatal_error_type,
            active_markets=sum(value is not None for value in health.current_markets.values()),
            pending_settlements=health.active_settlement_followups,
            retries=sum(health.retry_counts.values()),
            runtime_components=self._runtime_components(),
            recent_events=events,
        )

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
            runtime_components=self._runtime_components(),
        )

    def _runtime_components(self) -> dict[str, RuntimeComponentResponse]:
        data_parent = self.settings.recorder_data_path.resolve().parent
        root = data_parent.parent if data_parent.name.lower() == "data" else data_parent
        try:
            supervisor = read_json(root / "runtime" / "runtime-supervisor-status.json")
        except RuntimeStatusError:
            return {}
        raw_components = supervisor.get("components") if supervisor else None
        if not isinstance(raw_components, dict):
            return {}
        result: dict[str, RuntimeComponentResponse] = {}
        checked_at = self._clock().astimezone(UTC)
        supervisor_heartbeat = self._optional_aware_datetime(supervisor.get("last_heartbeat"))
        supervisor_age = (
            max(0.0, (checked_at - supervisor_heartbeat).total_seconds())
            if supervisor_heartbeat
            else None
        )
        supervisor_current = (
            supervisor.get("status") == "RUNNING"
            and supervisor_age is not None
            and supervisor_age <= self.settings.ui_heartbeat_stale_seconds
        )
        for name, raw in raw_components.items():
            if not isinstance(name, str) or not isinstance(raw, dict):
                continue
            pid = self._optional_int(raw.get("pid"))
            started = self._optional_aware_datetime(raw.get("started_at"))
            heartbeat = self._optional_aware_datetime(raw.get("last_heartbeat"))
            age = max(0.0, (checked_at - heartbeat).total_seconds()) if heartbeat else None
            fresh = age is not None and age <= self.settings.ui_heartbeat_stale_seconds
            declared_status = str(raw.get("status", "UNKNOWN"))
            # A current supervisor receipt is the authority for desired component state.
            # Intentional auxiliaries are not expected to emit a current child heartbeat;
            # their historic PID can never be projected as live. Every other component
            # must still satisfy its own heartbeat freshness gate.
            if not supervisor_current:
                status = "STALE"
                effective_pid = None
                process_is_alive = False
            elif declared_status in _INTENTIONAL_AUXILIARY_STATUSES:
                status = declared_status
                effective_pid = None
                process_is_alive = False
            else:
                status = declared_status if fresh else "STALE"
                effective_pid = pid if fresh else None
                process_is_alive = fresh and pid is not None and process_alive(pid)
            result[name] = RuntimeComponentResponse(
                status=status,
                pid=effective_pid,
                started_at=started,
                last_heartbeat=heartbeat,
                heartbeat_age_seconds=age,
                last_error=self._optional_string(raw.get("last_error")),
                process_alive=process_is_alive,
                expected_mode=self._optional_string(raw.get("expected_mode")),
            )
        return result

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
            before = self.controller.status()
            result = method()
        already_in_state = (action == "pause" and before.state.value == "paused") or (
            action in {"start", "resume"} and before.state.value in {"running", "starting"}
        )
        return RecorderControlResponse(
            action=RecorderControlAction(action),
            outcome=(
                RecorderControlOutcome.ALREADY_IN_STATE
                if already_in_state
                else RecorderControlOutcome.APPLIED
            ),
            state=RecorderState(result.state.value),
            pid=result.pid,
            message=result.message,
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
    def _optional_aware_datetime(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)

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

    @staticmethod
    def _ws_archive_health(value: object) -> WsArchiveHealth:
        """Project an evolving recorder heartbeat onto the stable public API schema.

        Recorder-internal archive/adaptive metrics intentionally evolve faster than
        the Control Center response. Unknown fields are not malformed heartbeat
        facts; they are excluded by this explicit allowlist and never reach the UI.
        Known fields remain strictly validated by ``WsArchiveHealth``.
        """

        if not isinstance(value, dict):
            return WsArchiveHealth()
        allowed = WsArchiveHealth.model_fields
        return WsArchiveHealth.model_validate(
            {key: item for key, item in value.items() if key in allowed}
        )
