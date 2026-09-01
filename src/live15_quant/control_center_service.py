"""Read-only typed service boundary for the LIVE15 Control Center."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from live15_quant.account_service import ProductionAccountService
from live15_quant.config import Settings
from live15_quant.control_center_models import (
    ArchiveResponse,
    Availability,
    CoverageResponse,
    DataResponse,
    EventSummaryResponse,
    HealthResponse,
    MarketHistoryResponse,
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
    TerminalChannel,
    TerminalEvent,
    TerminalEventType,
    TrainingResponse,
    WorkerHealthResponse,
    WsArchiveHealth,
)
from live15_quant.control_center_store import DashboardReadStore
from live15_quant.market_sessions import MarketDataState, market_data_state, market_session
from live15_quant.models import Asset, RecorderEventSeverity
from live15_quant.recorder_control import RecorderProcessController
from live15_quant.research_data_authority import ResearchDataAuthority

_CATCH_UP_MINIMUM_OBSERVATION_SECONDS = 60.0


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

    def account_summary(self, profile: str = "production_primary"):
        return self.account_service.read_summary(profile)

    def account_orders(self, profile: str = "production_primary"):
        return self.account_service.orders(profile)

    def account_fills(self, profile: str = "production_primary"):
        return self.account_service.fills(profile)

    def account_equity_history(
        self, profile: str = "production_primary", history_range: str = "1D"
    ):
        return self.account_service.equity_history(profile, history_range)

    async def run_account_equity_sampler(self, stop: asyncio.Event) -> None:
        """Collect forward-only account equity at low-idle/high-active cadence."""

        delay = 60.0
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
            if stop.is_set():
                return
            delay = await asyncio.to_thread(self.account_service.sample_equity)

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
                # Current recorder heartbeats already compute this typed state from
                # the exact source availability and session calendar.  Do not dilute
                # that authority with a second projection here.
                if asset.value in underlying_states:
                    continue
                # Older heartbeat receipts did not carry an authoritative underlying
                # state or receive cursor.  Preserve the store's explicit freshness
                # projection for those receipts instead of inventing an outage.
                if (
                    asset.value not in underlying_states
                    and asset.value not in last_underlying
                    and source not in source_failures
                ):
                    state = market_data_state(
                        asset,
                        checked_at=checked_at,
                        latest_received=None,
                        max_age=timedelta(seconds=self.settings.recorder_pyth_stale_seconds),
                    )
                    if state is not MarketDataState.MARKET_CLOSED:
                        continue
                else:
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
                current_health_issues=self._string_list(raw.get("current_health_issues")),
                source_failures=source_failures,
                stale_sources=stale_sources,
                market_closed_sources=market_closed_sources,
                underlying_market_states=underlying_states,
                worker_progress=self._datetime_map(raw.get("worker_progress")),
                worker_progress_age_seconds=self._float_map(raw.get("worker_progress_age_seconds")),
                worker_health=self._worker_health(raw.get("worker_health")),
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
                ws_archive=self._ws_archive_health(
                    raw.get("ws_archive"),
                    checked_at=checked_at,
                    maximum_rate_age_seconds=self.settings.ui_heartbeat_stale_seconds,
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
        payloads = self.store.summaries(
            self._clock(), health.current_markets, self._synchronized_markets(health)
        )
        return [
            MarketResponse.model_validate(self._apply_underlying_state(payload, health, asset))
            for asset, payload in zip(Asset, payloads, strict=True)
        ]

    def market(self, asset: Asset) -> MarketResponse:
        health = self.health()
        ticker = health.current_markets.get(asset.value)
        payload = self.store.asset(
            asset,
            self._clock(),
            ticker,
            allow_ws=self._synchronized_markets(health).get(asset.value) == ticker,
        )
        payload = self._apply_underlying_state(payload, health, asset)
        payload["previous_events"] = self.store.previous_events(asset)
        return MarketResponse.model_validate(payload)

    def market_history(self, asset: Asset) -> MarketHistoryResponse:
        health = self.health()
        return MarketHistoryResponse.model_validate(
            self.store.market_history(asset, self._clock(), health.current_markets.get(asset.value))
        )

    def terminal_cursor(self, channel: TerminalChannel | str) -> tuple[object, ...]:
        channel = TerminalChannel(channel)
        try:
            heartbeat_cursor: object = self.settings.recorder_health_path.stat().st_mtime_ns
        except OSError:
            heartbeat_cursor = None
        if channel is TerminalChannel.OVERVIEW:
            return (heartbeat_cursor,)
        if channel is TerminalChannel.MARKETS:
            return (*self.store.realtime_cursor(), heartbeat_cursor)
        asset = Asset(channel.value.removeprefix("market:"))
        health = self.health()
        ticker = health.current_markets.get(asset.value)
        return (
            *self.store.realtime_asset_cursor(asset, ticker),
            ticker,
            self._synchronized_markets(health).get(asset.value),
            health.underlying_market_states.get(asset.value),
        )

    def terminal_event(
        self,
        channel: TerminalChannel | str,
        sequence: int,
        event_type: TerminalEventType | str,
    ) -> TerminalEvent:
        channel = TerminalChannel(channel)
        now = self._clock()
        if channel is TerminalChannel.OVERVIEW:
            health = self.health()
            return TerminalEvent(
                event_type=event_type,
                channel=channel,
                observed_at=now,
                authoritative_at=health.observed_at,
                sequence=sequence,
                payload=health,
            )
        if channel is TerminalChannel.MARKETS:
            markets = self.markets()
            authoritative = self._latest_market_authority(markets)
            return TerminalEvent(
                event_type=event_type,
                channel=channel,
                observed_at=now,
                authoritative_at=authoritative,
                sequence=sequence,
                payload=markets,
            )
        asset = Asset(channel.value.removeprefix("market:"))
        health = self.health()
        ticker = health.current_markets.get(asset.value)
        payload = self.store.realtime_asset(
            asset,
            now,
            ticker,
            allow_ws=self._synchronized_markets(health).get(asset.value) == ticker,
        )
        payload = self._apply_underlying_state(payload, health, asset)
        market = MarketResponse.model_validate(payload)
        return TerminalEvent(
            event_type=event_type,
            channel=channel,
            asset=asset.value,
            ticker=market.ticker,
            observed_at=now,
            authoritative_at=self._latest_market_authority((market,)),
            sequence=sequence,
            payload=market,
        )

    @staticmethod
    def _latest_market_authority(markets: Iterable[MarketResponse]) -> datetime | None:
        timestamps = [
            value
            for market in markets
            for value in (
                market.projection_available_timestamp,
                market.underlying_persisted_timestamp,
            )
            if value is not None
        ]
        return max(timestamps, default=None)

    @staticmethod
    def _synchronized_markets(health: HealthResponse) -> dict[str, str]:
        if (
            health.recorder_state is not RecorderState.RUNNING
            or health.kalshi_ws_connection_state != "synchronized"
        ):
            return {}
        return health.kalshi_ws_synchronized_markets

    @staticmethod
    def _apply_underlying_state(
        payload: dict[str, object], health: HealthResponse, asset: Asset
    ) -> dict[str, object]:
        """Health's current source authority overrides only the list/detail display state."""

        state = health.underlying_market_states.get(asset.value)
        if state in {"healthy", "market_closed", "stale", "source_unavailable"}:
            payload["underlying_status"] = state
        return payload

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
        pool = self.store.raw_finalized_pool()
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
        checked_at = self._clock()
        rate_evidence_fresh = (
            archive.archive_rate_observed_at is not None
            and 0
            <= (checked_at - archive.archive_rate_observed_at).total_seconds()
            <= self.settings.ui_heartbeat_stale_seconds
        )
        if not rate_evidence_fresh:
            archive = archive.model_copy(
                update={
                    "archive_throughput_events_per_second": None,
                    "archive_throughput_observation_window_seconds": None,
                    "archive_catch_up_ratio": None,
                    "archive_backlog_slope_events_per_second": None,
                    "archive_catch_up_eta_seconds": None,
                    "archive_catch_up_status": "UNKNOWN",
                }
            )
        state = "disabled" if not archive.enabled else "healthy"
        if archive.failed or archive.quarantined:
            state = "attention"
        compressed_saved, compression_percent = self._compression_savings(archive)
        return ArchiveResponse(
            generated_at=checked_at,
            state=state,
            enabled=archive.enabled,
            poll_mode=archive.archive_poll_mode,
            next_poll_seconds=archive.archive_next_poll_seconds,
            verified_chunks=archive.verified,
            failed_chunks=archive.failed,
            waiting_chunks=archive.waiting_for_replay_baseline,
            quarantined_chunks=archive.quarantined,
            backlog_events=archive.archive_backlog_events,
            backlog_capped=archive.archive_backlog_capped,
            deferred_for_ws_backpressure=archive.deferred_for_ws_backpressure,
            throughput_events_per_second=archive.archive_throughput_events_per_second,
            input_ws_events_per_second=archive.input_ws_events_per_second,
            input_ws_observation_window_seconds=archive.input_ws_observation_window_seconds,
            throughput_observation_window_seconds=(
                archive.archive_throughput_observation_window_seconds
            ),
            rate_observed_at=archive.archive_rate_observed_at,
            catch_up_ratio=archive.archive_catch_up_ratio,
            backlog_slope_events_per_second=archive.archive_backlog_slope_events_per_second,
            catch_up_eta_seconds=archive.archive_catch_up_eta_seconds,
            catch_up_status=archive.archive_catch_up_status,
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
            notes=(
                ["Purge eligibility is a dry-run projection; destructive actions are absent."]
                if rate_evidence_fresh
                else [
                    "Purge eligibility is a dry-run projection; destructive actions are absent.",
                    "Catch-up rate evidence is stale or unavailable; status fails closed.",
                ]
            ),
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
        """RuntimeSupervisor is retired; retain the response field as an empty projection."""
        return {}

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

    def event_summary(
        self,
        *,
        asset: Asset | None,
        source: str | None,
        since: datetime,
    ) -> EventSummaryResponse:
        checked_at = self._clock().astimezone(UTC)
        if since > checked_at or checked_at - since > timedelta(hours=24):
            raise ValueError("event summary window must be within the last 24 hours")
        counts = self.store.event_summary(asset=asset, source=source, since=since, until=checked_at)
        return EventSummaryResponse(
            window_start=since.astimezone(UTC),
            window_end=checked_at,
            availability=Availability.AVAILABLE if counts is not None else Availability.UNAVAILABLE,
            warnings=None if counts is None else counts.get("warning", 0),
            errors=None if counts is None else counts.get("error", 0),
            fatals=None if counts is None else counts.get("fatal", 0),
            sample_truncated=False,
        )

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
    def _worker_health(value: object) -> dict[str, WorkerHealthResponse]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, WorkerHealthResponse] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, dict):
                continue
            consecutive = item.get("consecutive_failures", 0)
            result[key] = WorkerHealthResponse(
                current_state=str(item.get("current_state", "unknown")),
                consecutive_failures=(
                    consecutive
                    if (
                        isinstance(consecutive, int)
                        and not isinstance(consecutive, bool)
                        and consecutive >= 0
                    )
                    else 0
                ),
                last_error_type=ControlCenterService._optional_string(item.get("last_error_type")),
                next_retry_at=ControlCenterService._optional_aware_datetime(
                    item.get("next_retry_at")
                ),
                last_progress_timestamp=ControlCenterService._optional_aware_datetime(
                    item.get("last_progress_timestamp")
                ),
                last_successful_observation_timestamp=ControlCenterService._optional_aware_datetime(
                    item.get("last_successful_observation_timestamp")
                ),
                age_seconds=ControlCenterService._optional_float(item.get("age_seconds")),
            )
        return result

    @staticmethod
    def _ws_archive_health(
        value: object,
        *,
        checked_at: datetime | None = None,
        maximum_rate_age_seconds: float | None = None,
    ) -> WsArchiveHealth:
        """Project an evolving recorder heartbeat onto the stable public API schema.

        Recorder-internal archive/adaptive metrics intentionally evolve faster than
        the Control Center response. Unknown fields are not malformed heartbeat
        facts; they are excluded by this explicit allowlist and never reach the UI.
        Known fields remain strictly validated by ``WsArchiveHealth``.
        """

        if not isinstance(value, dict):
            return WsArchiveHealth()
        allowed = WsArchiveHealth.model_fields
        archive = WsArchiveHealth.model_validate(
            {key: item for key, item in value.items() if key in allowed}
        )
        if checked_at is not None and maximum_rate_age_seconds is not None:
            rate_observed_at = archive.archive_rate_observed_at
            rate_age_seconds = (
                None
                if rate_observed_at is None
                else (checked_at - rate_observed_at).total_seconds()
            )
            if (
                rate_age_seconds is None
                or rate_age_seconds < 0
                or rate_age_seconds > maximum_rate_age_seconds
            ):
                return archive.model_copy(
                    update={
                        "archive_throughput_events_per_second": None,
                        "archive_throughput_observation_window_seconds": None,
                        "archive_catch_up_ratio": None,
                        "archive_backlog_slope_events_per_second": None,
                        "archive_catch_up_eta_seconds": None,
                        "archive_catch_up_status": "UNKNOWN",
                    }
                )
        incoming = archive.input_ws_events_per_second
        throughput = archive.archive_throughput_events_per_second
        backlog = archive.archive_backlog_events
        if (
            incoming is None
            or incoming <= 0
            or throughput is None
            or throughput < 0
            or archive.input_ws_observation_window_seconds is None
            or archive.archive_throughput_observation_window_seconds is None
            or archive.input_ws_observation_window_seconds < _CATCH_UP_MINIMUM_OBSERVATION_SECONDS
            or archive.archive_throughput_observation_window_seconds
            < _CATCH_UP_MINIMUM_OBSERVATION_SECONDS
        ):
            return archive
        ratio = throughput / incoming
        slope = throughput - incoming
        if ratio < 1:
            status = "FALLING_BEHIND"
        elif backlog > 0 and slope > 0:
            status = "CATCHING_UP"
        elif backlog == 0 and ratio >= 1:
            status = "KEEPING_UP"
        else:
            status = "UNKNOWN"
        eta = backlog / slope if backlog > 0 and slope > 0 else None
        return archive.model_copy(
            update={
                "archive_catch_up_ratio": ratio,
                "archive_backlog_slope_events_per_second": slope,
                "archive_catch_up_eta_seconds": eta,
                "archive_catch_up_status": status,
            }
        )
