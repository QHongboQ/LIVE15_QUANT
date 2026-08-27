"""Continuous Kalshi-native training-data recorder."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

import requests

from live15_quant.adaptive_retention import (
    AdaptiveRetentionController,
    AdaptiveRetentionError,
    AdaptiveRetentionMode,
    AdaptiveRetentionObservation,
    AdaptiveRetentionPolicy,
    AdaptiveRetentionStateError,
    write_adaptive_retention_status,
)
from live15_quant.config import Settings
from live15_quant.features import COINBASE_PRODUCT_BY_ASSET
from live15_quant.gaps import (
    DataGap,
    GapReason,
    GapSource,
    GapStream,
    configured_streams,
    timedelta_seconds,
)
from live15_quant.kalshi_gateway.production_recorder_host import SdkProductionRecorderHost
from live15_quant.kalshi_gateway.recorder_provider import RecorderMarketDataEvent
from live15_quant.kalshi_lifecycle import (
    KalshiDiscovery,
    KalshiLifecycle,
    KalshiLifecycleStateMachine,
    KalshiMarket,
    KalshiNativeMarketProvider,
)
from live15_quant.kalshi_ws import (
    KalshiAtomicOrderBookCoordinator,
    KalshiAtomicSessionProcessor,
    KalshiBookInvariantError,
    KalshiBookSyncStatus,
    KalshiCommandAcknowledged,
    KalshiOrderBookDelta,
    KalshiOrderBookSnapshot,
    KalshiServerMessage,
    KalshiSubscribed,
    KalshiSubscriptionCommand,
    KalshiTickerUpdate,
    KalshiUnsynchronizedBookError,
    KalshiWsErrorMessage,
    KalshiWsPayloadError,
    KalshiWsPayloadIssue,
    KalshiWsProtocolNotice,
    KalshiWsRuntimeState,
    SynchronizedKalshiOrderBook,
    update_subscription_command,
)
from live15_quant.market_sessions import (
    MarketDataState,
    market_data_state,
    open_intervals_for_asset,
)
from live15_quant.models import (
    Asset,
    FifteenMinuteContract,
    FreshnessState,
    KalshiNativeQuote,
    MarketTick,
    RecorderEventSeverity,
    RecorderEventType,
)
from live15_quant.providers.coinbase import CoinbaseWebSocketClient
from live15_quant.providers.kalshi import (
    KALSHI_15MIN_SERIES,
    KalshiOfficialQuoteProvider,
    KalshiPublicApiError,
    KalshiTargetUnavailableError,
)
from live15_quant.providers.kalshi_ws import (
    KalshiProductionReadOnlyWebSocket,
    KalshiReadOnlyWsError,
)
from live15_quant.providers.low_latency import (
    BenchmarkPayloadError,
    BenchmarkSource,
    BinanceBnbPublicMarketDataSource,
    HyperliquidHypePublicMarketDataSource,
)
from live15_quant.providers.pyth import (
    PYTH_FEEDS,
    PythFeedDemultiplexer,
    PythHermesClient,
    PythNetworkError,
    PythPayloadError,
    PythRateLimitError,
    PythUpdateBatch,
)
from live15_quant.providers.robinhood_15min import Robinhood15MinuteProvider
from live15_quant.records import KalshiMarketRecord
from live15_quant.runtime_status import atomic_json
from live15_quant.secondary import secondary_from_benchmark_tick
from live15_quant.storage import (
    KalshiWsPersistenceEvent,
    MarketIdentityConflictError,
    RecorderStorageError,
    RecorderStore,
    SecondaryAppendStatus,
    SettlementConflictError,
)
from live15_quant.ws_retention import (
    DiskQuota,
    WsArchiveService,
    WsPurgeService,
    WsRetentionError,
    WsRetentionManifest,
)

logger = logging.getLogger(__name__)

_LIVE_WS_PROJECTION_DEPTH = 16


def _next_pyth_batch(iterator: Iterator[PythUpdateBatch]) -> PythUpdateBatch | None:
    return next(iterator, None)


class NativeDiscovery(Protocol):
    def discover(self, asset: Asset, now: datetime | None = None) -> KalshiDiscovery: ...

    def get_market(
        self, asset: Asset, ticker: str, *, historical: bool = False
    ) -> KalshiMarket: ...


class NativeQuoteSource(Protocol):
    def quote_native(self, market: KalshiMarket) -> KalshiNativeQuote: ...


class TickStream(Protocol):
    def ticks(self) -> AsyncIterator[MarketTick]: ...


class RobinhoodReference(Protocol):
    def discover(self) -> Sequence[FifteenMinuteContract]: ...


class UnderlyingSource(Protocol):
    def stream_batches(self) -> Iterator[PythUpdateBatch]: ...

    def latest_batch(self) -> PythUpdateBatch: ...

    def close(self) -> None: ...


class KalshiWsSource(Protocol):
    diagnostics: object

    def messages(self, tickers: Sequence[str]) -> AsyncIterator[KalshiServerMessage]: ...

    def set_reconnect_tickers(self, tickers: Sequence[str]) -> None: ...

    async def send_command(self, command: KalshiSubscriptionCommand) -> None: ...

    async def close(self) -> None: ...

    async def request_reconnect(self) -> None: ...


@dataclass(frozen=True, slots=True)
class KalshiNativeHealth:
    started_at: datetime
    observed_at: datetime
    current_markets: dict[Asset, str | None]
    last_discovery: dict[Asset, datetime]
    last_quotes: dict[Asset, datetime]
    last_coinbase: dict[str, datetime]
    last_additional_underlying: dict[Asset, datetime]
    last_secondary_underlying: dict[str, datetime]
    secondary_persist_latency_ms: dict[str, str]
    secondary_diagnostics: dict[str, int]
    active_settlement_followups: int
    settlement_count: int
    database_bytes: int
    wal_bytes: int
    row_counts: dict[str, int]
    row_counts_complete: bool
    retry_counts: dict[str, int]
    source_failures: dict[str, str]
    stale_sources: tuple[str, ...]
    market_closed_sources: tuple[str, ...]
    underlying_market_states: dict[Asset, MarketDataState]
    worker_progress: dict[str, datetime]
    stale_workers: tuple[str, ...]
    event_loop_lag_seconds: float
    last_finalized_settlement: dict[Asset, str]
    written_records: int
    integrity: str
    robinhood_reference_healthy: bool | None
    fatal_task: str | None
    fatal_error_type: str | None
    kalshi_ws_connection_state: KalshiWsRuntimeState
    kalshi_ws_synchronized_markets: dict[Asset, str]
    kalshi_ws_last_books: dict[Asset, datetime]
    kalshi_ws_seq_gaps: int
    kalshi_ws_resync_count: int
    kalshi_ws_reconnect_count: int
    kalshi_ws_queue_high_watermark: int
    kalshi_ws_queue_capacity: int
    kalshi_ws_queue_depth: int
    kalshi_ws_queue_enqueued: int
    kalshi_ws_queue_dequeued: int
    kalshi_ws_queue_full_waits: int
    kalshi_ws_queue_dropped: int
    kalshi_ws_queue_max_backlog_seconds: float
    kalshi_ws_queue_above_50_seconds: float
    kalshi_ws_queue_above_75_seconds: float
    kalshi_ws_queue_above_90_seconds: float
    kalshi_ws_receive_persist_latency_ms: str | None
    kalshi_rest_fallback_status: str
    ws_archive_metrics: dict[str, object]

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, (self.observed_at - self.started_at).total_seconds())

    def as_dict(self) -> dict[str, object]:
        def timestamps(values: dict[object, datetime]) -> dict[str, str]:
            return {str(key): value.isoformat() for key, value in values.items()}

        def ages(values: dict[object, datetime]) -> dict[str, float]:
            return {
                str(key): max(0.0, (self.observed_at - value).total_seconds())
                for key, value in values.items()
            }

        status, current_health_issues = _aggregate_current_health(
            integrity=self.integrity,
            source_failures=self.source_failures,
            stale_sources=self.stale_sources,
            stale_workers=self.stale_workers,
        )
        return {
            "status": status,
            "current_health_issues": current_health_issues,
            "started_at": self.started_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "uptime_seconds": self.uptime_seconds,
            "current_markets": {str(key): value for key, value in self.current_markets.items()},
            "last_discovery": timestamps(self.last_discovery),
            "last_discovery_age_seconds": ages(self.last_discovery),
            "last_quotes": timestamps(self.last_quotes),
            "last_quote_age_seconds": ages(self.last_quotes),
            "last_coinbase": timestamps(self.last_coinbase),
            "last_underlying_tick_age_seconds": ages(self.last_coinbase),
            "last_additional_underlying": timestamps(self.last_additional_underlying),
            "last_additional_underlying_age_seconds": ages(self.last_additional_underlying),
            "last_secondary_underlying": timestamps(self.last_secondary_underlying),
            "last_secondary_underlying_age_seconds": ages(self.last_secondary_underlying),
            "secondary_persist_latency_ms": self.secondary_persist_latency_ms,
            "secondary_diagnostics": self.secondary_diagnostics,
            "active_settlement_followups": self.active_settlement_followups,
            "settlement_count": self.settlement_count,
            "database_bytes": self.database_bytes,
            "wal_bytes": self.wal_bytes,
            "row_counts": self.row_counts,
            "row_counts_complete": self.row_counts_complete,
            "retry_counts": self.retry_counts,
            "source_failures": self.source_failures,
            "stale_sources": self.stale_sources,
            "market_closed_sources": self.market_closed_sources,
            "underlying_market_states": {
                str(asset): state.value for asset, state in self.underlying_market_states.items()
            },
            "worker_progress": timestamps(self.worker_progress),
            "worker_progress_age_seconds": ages(self.worker_progress),
            "stale_workers": self.stale_workers,
            "event_loop_lag_seconds": self.event_loop_lag_seconds,
            "last_finalized_settlement": {
                str(key): value for key, value in self.last_finalized_settlement.items()
            },
            "written_records": self.written_records,
            "integrity": self.integrity,
            "robinhood_reference_healthy": self.robinhood_reference_healthy,
            "fatal_task": self.fatal_task,
            "fatal_error_type": self.fatal_error_type,
            "kalshi_ws_connection_state": self.kalshi_ws_connection_state.value,
            "kalshi_ws_synchronized_markets": {
                str(asset): ticker for asset, ticker in self.kalshi_ws_synchronized_markets.items()
            },
            "kalshi_ws_synchronized_count": len(self.kalshi_ws_synchronized_markets),
            "kalshi_ws_book_age_seconds": ages(self.kalshi_ws_last_books),
            "kalshi_ws_seq_gaps": self.kalshi_ws_seq_gaps,
            "kalshi_ws_resync_count": self.kalshi_ws_resync_count,
            "kalshi_ws_reconnect_count": self.kalshi_ws_reconnect_count,
            "kalshi_ws_queue_high_watermark": self.kalshi_ws_queue_high_watermark,
            "kalshi_ws_queue_capacity": self.kalshi_ws_queue_capacity,
            "kalshi_ws_queue_depth": self.kalshi_ws_queue_depth,
            "kalshi_ws_queue_enqueued": self.kalshi_ws_queue_enqueued,
            "kalshi_ws_queue_dequeued": self.kalshi_ws_queue_dequeued,
            "kalshi_ws_queue_full_waits": self.kalshi_ws_queue_full_waits,
            "kalshi_ws_queue_dropped": self.kalshi_ws_queue_dropped,
            "kalshi_ws_queue_max_backlog_seconds": self.kalshi_ws_queue_max_backlog_seconds,
            "kalshi_ws_queue_above_50_seconds": self.kalshi_ws_queue_above_50_seconds,
            "kalshi_ws_queue_above_75_seconds": self.kalshi_ws_queue_above_75_seconds,
            "kalshi_ws_queue_above_90_seconds": self.kalshi_ws_queue_above_90_seconds,
            "kalshi_ws_receive_persist_latency_ms": self.kalshi_ws_receive_persist_latency_ms,
            "kalshi_rest_fallback_status": self.kalshi_rest_fallback_status,
            "ws_archive": self.ws_archive_metrics,
        }


def _aggregate_current_health(
    *,
    integrity: str,
    source_failures: dict[str, str],
    stale_sources: tuple[str, ...],
    stale_workers: tuple[str, ...],
) -> tuple[str, list[str]]:
    """Classify present conditions without treating historical retries as faults.

    ``retry_counts`` is intentionally absent from this input: it is a retained
    incident counter, not evidence that a dependency is currently unhealthy.
    Source failures are removed by ``_source_ok`` after recovery, while stale
    source and worker facts are recomputed for each heartbeat.
    """
    if integrity not in {"ok", "not_checked"}:
        return "storage_error", ["storage_integrity"]
    issues = [
        *[f"source_failure:{key}" for key in sorted(source_failures)],
        *[f"stale_source:{key}" for key in sorted(stale_sources)],
        *[f"stale_worker:{key}" for key in sorted(stale_workers)],
    ]
    return ("degraded" if issues else "healthy"), issues


@dataclass(slots=True)
class _MutableHealth:
    started_at: datetime
    current: dict[Asset, KalshiMarket] = field(default_factory=dict)
    states: dict[str, KalshiLifecycle] = field(default_factory=dict)
    last_discovery: dict[Asset, datetime] = field(default_factory=dict)
    last_quotes: dict[Asset, datetime] = field(default_factory=dict)
    last_coinbase: dict[str, datetime] = field(default_factory=dict)
    last_additional_underlying: dict[Asset, datetime] = field(default_factory=dict)
    additional_underlying_freshness: dict[Asset, FreshnessState] = field(default_factory=dict)
    last_secondary_underlying: dict[str, datetime] = field(default_factory=dict)
    secondary_persist_latency_ms: dict[str, str] = field(default_factory=dict)
    secondary_diagnostics: dict[str, int] = field(default_factory=dict)
    retry_counts: dict[str, int] = field(default_factory=dict)
    consecutive_failures: dict[str, int] = field(default_factory=dict)
    source_failures: dict[str, str] = field(default_factory=dict)
    worker_progress: dict[str, datetime] = field(default_factory=dict)
    event_loop_lag_seconds: float = 0.0
    last_finalized: dict[Asset, str] = field(default_factory=dict)
    written_records: int = 0
    row_counts: dict[str, int] = field(default_factory=dict)
    row_counts_complete: bool = False
    active_settlement_followups: int = 0
    integrity: str = "not_checked"
    robinhood_reference_healthy: bool | None = None
    fatal_task: str | None = None
    fatal_error_type: str | None = None
    kalshi_ws_state: KalshiWsRuntimeState = KalshiWsRuntimeState.CONNECTING
    kalshi_ws_synchronized: dict[Asset, str] = field(default_factory=dict)
    kalshi_ws_last_books: dict[Asset, datetime] = field(default_factory=dict)
    kalshi_ws_seq_gaps: int = 0
    kalshi_ws_resync_count: int = 0
    kalshi_ws_reconnect_count: int = 0
    kalshi_ws_receive_persist_latency_ms: str | None = None
    ws_archive_metrics: dict[str, object] = field(default_factory=dict)


def _market_from_record(record: KalshiMarketRecord) -> KalshiMarket:
    return KalshiMarket(
        asset=record.asset,
        series=record.series,
        ticker=record.ticker,
        event_ticker=record.event_ticker,
        window_start=record.window_start,
        window_end=record.window_end,
        target=record.target,
        lifecycle=record.lifecycle,
        official_status=record.official_status,
        fetched_timestamp=record.fetched_timestamp,
        source_url=record.source_url,
        rules_primary=record.rules_primary,
        rules_secondary=record.rules_secondary,
        settlement_timer_seconds=record.settlement_timer_seconds,
        determination_result=record.determination_result,
    )


class KalshiNativeRecorder:
    """Persist independent Kalshi truth/quotes and Coinbase predictive input."""

    def __init__(
        self,
        settings: Settings,
        store: RecorderStore,
        *,
        discovery: NativeDiscovery | None = None,
        quotes: NativeQuoteSource | None = None,
        coinbase_factory: Callable[[], TickStream] | None = None,
        robinhood_reference: RobinhoodReference | None = None,
        underlying_factory: Callable[[], UnderlyingSource] | None = None,
        secondary_factories: dict[Asset, Callable[[], BenchmarkSource]] | None = None,
        kalshi_ws_factory: Callable[[], KalshiWsSource] | None = None,
        initial_row_counts: Mapping[str, int] | None = None,
        initial_row_counts_complete: bool = False,
        initial_active_settlement_followups: int | None = None,
        last_verified_integrity: str | None = None,
        startup_phase_observer: Callable[[str, float], None] | None = None,
        controlled_pause: Callable[[str], None] | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._store = store
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._startup_phase_observer = startup_phase_observer
        self._controlled_pause = controlled_pause
        self._startup_ws_synchronized_reported = False
        self._owned_clients: list[KalshiOfficialQuoteProvider] = []
        if quotes is None:
            quote_clients = {
                asset: KalshiOfficialQuoteProvider(
                    settings,
                    retry_total=1,
                    respect_retry_after_header=False,
                )
                for asset in Asset
            }
            self._owned_clients.extend(quote_clients.values())
            self._quotes = quote_clients
        else:
            self._quotes = {asset: quotes for asset in Asset}
        if discovery is None:
            discovery_clients = {
                asset: KalshiOfficialQuoteProvider(
                    settings,
                    retry_total=1,
                    respect_retry_after_header=False,
                )
                for asset in Asset
            }
            self._owned_clients.extend(discovery_clients.values())
            self._discoveries = {
                asset: KalshiNativeMarketProvider(discovery_clients[asset]) for asset in Asset
            }
            settlement_clients = {
                asset: KalshiOfficialQuoteProvider(
                    settings,
                    retry_total=1,
                    respect_retry_after_header=False,
                )
                for asset in Asset
            }
            self._owned_clients.extend(settlement_clients.values())
            self._settlements = {
                asset: KalshiNativeMarketProvider(settlement_clients[asset]) for asset in Asset
            }
        else:
            self._discoveries = {asset: discovery for asset in Asset}
            self._settlements = {asset: discovery for asset in Asset}
        self._coinbase_factory = coinbase_factory or (
            lambda: CoinbaseWebSocketClient(settings, products=settings.products)
        )
        self._underlying_factory = underlying_factory or (lambda: PythHermesClient(settings))
        self._secondary_factories = secondary_factories or {
            Asset.BNB: BinanceBnbPublicMarketDataSource,
            Asset.HYPE: HyperliquidHypePublicMarketDataSource,
        }
        self._kalshi_ws: KalshiWsSource | None = None
        self._sdk_recorder_host: SdkProductionRecorderHost | None = None
        if (
            settings.enable_kalshi_production_websocket
            and settings.kalshi_recorder_provider == "legacy"
        ):
            self._kalshi_ws = (
                kalshi_ws_factory()
                if kalshi_ws_factory is not None
                else KalshiProductionReadOnlyWebSocket.from_settings(settings)
            )
        self._kalshi_ws_coordinator: KalshiAtomicOrderBookCoordinator | None = None
        self._kalshi_ws_waiting_since_monotonic: float | None = None
        self._kalshi_ws_books: dict[Asset, SynchronizedKalshiOrderBook] = {}
        # One compact as-of book per configured forward decision bucket is enough
        # for frozen-paper inference.  Keep only the current ticker's offsets in
        # memory so this observability aid cannot grow for a 24/7 recorder.
        self._forward_shadow_checkpointed: dict[tuple[Asset, str], set[int]] = {}
        self._kalshi_ws_pending: list[
            tuple[
                KalshiOrderBookSnapshot | KalshiOrderBookDelta | KalshiCommandAcknowledged,
                KalshiBookSyncStatus,
            ]
        ] = []
        self._robinhood = robinhood_reference
        if settings.enable_robinhood_reference and self._robinhood is None:
            self._robinhood = Robinhood15MinuteProvider(settings)
        observed = self._utc_now()
        phase_started = self._startup_phase_started()
        records = store.latest_kalshi_states(
            window_end_at_or_after=observed - timedelta(minutes=30),
            window_end_before=observed + timedelta(hours=2),
        )
        self._mark_startup_phase("lifecycle_recovery", phase_started)
        phase_started = self._startup_phase_started()
        quote_cursors, tick_cursors = store.latest_native_cursors(settings.products)
        self._mark_startup_phase("cursor_recovery", phase_started)
        phase_started = self._startup_phase_started()
        finalized = store.latest_finalized_by_asset()
        current = {
            record.asset: _market_from_record(record)
            for record in records
            if record.lifecycle is KalshiLifecycle.OPEN
            and record.window_start <= observed < record.window_end
        }
        self._health = _MutableHealth(
            started_at=observed,
            current=current,
            states={record.ticker: record.lifecycle for record in records},
            last_quotes=quote_cursors,
            last_coinbase=tick_cursors,
            last_finalized={
                asset: f"{truth.ticker}:{truth.result.value}" for asset, truth in finalized.items()
            },
            row_counts=(
                store.bounded_row_count_estimates()
                if initial_row_counts is None
                else dict(initial_row_counts)
            ),
            row_counts_complete=initial_row_counts_complete,
            active_settlement_followups=(
                sum(
                    record.lifecycle in {KalshiLifecycle.CLOSED, KalshiLifecycle.SETTLEMENT_PENDING}
                    for record in records
                )
                if initial_active_settlement_followups is None
                else initial_active_settlement_followups
            ),
            integrity=last_verified_integrity or "not_checked",
        )
        self._mark_startup_phase("settlement_recovery", phase_started)
        self._stop_event = asyncio.Event()
        self._followup_cursors: dict[Asset, str | None] = {asset: None for asset in Asset}
        self._operation_condition = threading.Condition()
        self._active_operations = 0
        self._reported_stale_sources: set[str] = set()
        phase_started = self._startup_phase_started()
        self._gap_streams = {
            (stream.source, stream.asset): stream for stream in configured_streams(settings)
        }
        self._gap_last = {
            key: timestamp
            for key, stream in self._gap_streams.items()
            if (
                timestamp := store.latest_gap_stream_timestamp(
                    stream.source, stream.asset, stream.instrument
                )
            )
            is not None
        }
        current_ws_cursor_times = [
            timestamp
            for (source, asset), timestamp in self._gap_last.items()
            if source is GapSource.KALSHI_WS and asset in current
        ]
        self._hot_ws_access_evidence_complete = (
            bool(current)
            and len(current_ws_cursor_times) == len(current)
            and all(timestamp <= observed for timestamp in current_ws_cursor_times)
        )
        self._startup_ws_recovery_lookback_seconds = (
            max((observed - value).total_seconds() for value in current_ws_cursor_times)
            if self._hot_ws_access_evidence_complete
            else None
        )
        self._active_gaps: dict[tuple[GapSource, Asset], list[DataGap]] = {}
        for gap in store.active_data_gaps(tuple(self._gap_streams.values())):
            key = (gap.source, gap.asset)
            if key in self._gap_streams:
                self._active_gaps.setdefault(key, []).append(gap)
        for gaps in self._active_gaps.values():
            gaps.sort(key=lambda gap: (gap.gap_start, gap.detected_at))
        self._mark_startup_phase("gap_recovery", phase_started)
        phase_started = self._startup_phase_started()
        self._archive_service: WsArchiveService | None = None
        self._purge_service: WsPurgeService | None = None
        self._adaptive_retention: AdaptiveRetentionController | None = None
        if settings.enable_ws_archive:
            archive_root = settings.ws_archive_root or (store.path.parent / "ws_archive")
            manifest_path = settings.ws_archive_manifest_path or (
                store.path.parent / "ws_archive_manifest.sqlite3"
            )
            manifest = WsRetentionManifest(manifest_path)
            self._archive_service = WsArchiveService(
                store.path,
                archive_root,
                manifest,
                hot_retention=timedelta(seconds=settings.ws_archive_hot_retention_seconds),
                chunk_records=settings.ws_archive_chunk_records,
            )
            self._purge_service = WsPurgeService(
                store.path,
                archive_root,
                manifest,
                batch_rows=settings.ws_archive_purge_batch_rows,
            )
            if settings.enable_adaptive_ws_retention:
                policy = AdaptiveRetentionPolicy(
                    minimum_seconds=settings.adaptive_retention_min_seconds,
                    maximum_seconds=settings.adaptive_retention_max_seconds,
                    evidence_window=timedelta(
                        seconds=settings.adaptive_retention_evidence_window_seconds
                    ),
                    minimum_evidence_duration=timedelta(
                        seconds=settings.adaptive_retention_min_evidence_seconds
                    ),
                    minimum_verified_chunks=settings.adaptive_retention_min_verified_chunks,
                    minimum_evidence_samples=(settings.adaptive_retention_min_evidence_samples),
                    minimum_recovery_sessions=(settings.adaptive_retention_min_recovery_sessions),
                    minimum_simulation_passes=settings.adaptive_retention_simulation_passes,
                    safety_margin=timedelta(
                        seconds=settings.adaptive_retention_safety_margin_seconds
                    ),
                    cooldown=timedelta(seconds=settings.adaptive_retention_cooldown_seconds),
                    reevaluation_interval=timedelta(
                        seconds=settings.adaptive_retention_reevaluation_seconds
                    ),
                    serious_incident_quiet_period=timedelta(
                        seconds=settings.adaptive_retention_incident_quiet_seconds
                    ),
                    minimum_projection_window=timedelta(
                        seconds=(settings.adaptive_retention_min_projection_window_seconds)
                    ),
                    disk_deescalation_samples=(
                        settings.adaptive_retention_disk_deescalation_samples
                    ),
                    auto_adjust=settings.adaptive_retention_auto_adjust,
                )
                try:
                    self._adaptive_retention = AdaptiveRetentionController(
                        settings.adaptive_retention_state_path
                        or (store.path.parent / "adaptive-retention.sqlite3"),
                        policy,
                        initial_retention_seconds=int(settings.ws_archive_hot_retention_seconds),
                    )
                    self._archive_service.hot_retention = timedelta(
                        seconds=self._adaptive_retention.current_retention_seconds()
                    )
                except (AdaptiveRetentionError, sqlite3.DatabaseError) as error:
                    self._adaptive_retention = None
                    self._archive_service.hot_retention = timedelta(
                        seconds=settings.adaptive_retention_max_seconds
                    )
                    self._source_failed("adaptive_retention", error)
            self._health.ws_archive_metrics = manifest.metrics()
        self._mark_startup_phase("recorder_state_ready", phase_started)
        if (
            settings.enable_kalshi_production_websocket
            and settings.kalshi_recorder_provider == "sdk"
        ):
            self._sdk_recorder_host = SdkProductionRecorderHost(
                settings=settings,
                store=store,
                universe=self._sdk_ws_universe,
                on_committed=self._on_sdk_ws_committed,
                on_transport_state=self._on_sdk_ws_transport_state,
            )

    def _startup_phase_started(self) -> float | None:
        return self._monotonic() if self._startup_phase_observer is not None else None

    def _mark_startup_phase(self, phase: str, started: float | None) -> None:
        if self._startup_phase_observer is not None and started is not None:
            self._startup_phase_observer(phase, max(0.0, self._monotonic() - started))

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recorder clock must return timezone-aware timestamps")
        return value.astimezone(UTC)

    def request_stop(self) -> None:
        self._stop_event.set()

    def health(self) -> KalshiNativeHealth:
        observed = self._utc_now()
        database_bytes, wal_bytes = self._store.database_sizes()
        ws_state = self._health.kalshi_ws_state
        if (
            self._kalshi_ws is not None
            and getattr(self._kalshi_ws.diagnostics, "transport_state", None)
            is KalshiWsRuntimeState.RECONNECTING
        ):
            ws_state = KalshiWsRuntimeState.RECONNECTING
        if ws_state is KalshiWsRuntimeState.SYNCHRONIZED and not self._kalshi_ws_transport_fresh(
            observed
        ):
            ws_state = KalshiWsRuntimeState.UNSYNCHRONIZED
        pyth_states: dict[Asset, MarketDataState] = {}
        for asset in PYTH_FEEDS:
            state = market_data_state(
                asset,
                checked_at=observed,
                latest_received=self._health.last_additional_underlying.get(asset),
                max_age=timedelta(seconds=self._settings.recorder_pyth_stale_seconds),
                source_available=f"pyth:{asset.value}" not in self._health.source_failures,
            )
            if (
                state is MarketDataState.HEALTHY
                and self._health.additional_underlying_freshness.get(asset) is FreshnessState.STALE
            ):
                state = MarketDataState.STALE
            pyth_states[asset] = state
        stale_sources = tuple(
            sorted(
                [
                    f"kalshi_discovery:{asset.value}"
                    for asset in KALSHI_15MIN_SERIES
                    if asset not in self._health.last_discovery
                    or (observed - self._health.last_discovery[asset]).total_seconds()
                    > self._settings.native_discovery_poll_interval_seconds * 3
                ]
                + [
                    f"kalshi_quote:{asset.value}"
                    for asset in self._health.current
                    if asset not in self._health.last_quotes
                    or (observed - self._health.last_quotes[asset]).total_seconds()
                    > self._settings.official_quote_max_source_age_seconds
                ]
                + [
                    f"kalshi_ws:{asset.value}"
                    for asset in self._health.current
                    if self._settings.enable_kalshi_production_websocket
                    and (
                        asset not in self._health.kalshi_ws_synchronized
                        or not self._kalshi_ws_transport_fresh(observed)
                    )
                ]
                + [
                    f"coinbase:{product}"
                    for product in self._settings.products
                    if product not in self._health.last_coinbase
                    or (observed - self._health.last_coinbase[product]).total_seconds()
                    > self._settings.recorder_coinbase_stale_seconds
                ]
                + [
                    f"pyth:{asset.value}"
                    for asset in PYTH_FEEDS
                    if self._settings.enable_pyth_underlying
                    and pyth_states[asset]
                    in {MarketDataState.STALE, MarketDataState.SOURCE_UNAVAILABLE}
                ]
                + [
                    f"secondary:{key}"
                    for key in ("BNB:binance_spot", "HYPE:hyperliquid_perp")
                    if self._settings.enable_secondary_underlying
                    and (
                        key not in self._health.last_secondary_underlying
                        or (observed - self._health.last_secondary_underlying[key]).total_seconds()
                        > self._settings.recorder_secondary_stale_seconds
                    )
                ]
            )
        )
        stale_workers = tuple(
            sorted(
                key
                for key, threshold in self._expected_worker_thresholds().items()
                if key not in self._health.worker_progress
                or (observed - self._health.worker_progress[key]).total_seconds() > threshold
            )
        )
        return KalshiNativeHealth(
            started_at=self._health.started_at,
            observed_at=observed,
            current_markets={
                asset: (
                    self._health.current[asset].ticker if asset in self._health.current else None
                )
                for asset in KALSHI_15MIN_SERIES
            },
            last_discovery=dict(self._health.last_discovery),
            last_quotes=dict(self._health.last_quotes),
            last_coinbase=dict(self._health.last_coinbase),
            last_additional_underlying=dict(self._health.last_additional_underlying),
            last_secondary_underlying=dict(self._health.last_secondary_underlying),
            secondary_persist_latency_ms=dict(self._health.secondary_persist_latency_ms),
            secondary_diagnostics=dict(self._health.secondary_diagnostics),
            active_settlement_followups=self._health.active_settlement_followups,
            settlement_count=self._health.row_counts["kalshi_settlements"],
            database_bytes=database_bytes,
            wal_bytes=wal_bytes,
            row_counts=dict(self._health.row_counts),
            row_counts_complete=self._health.row_counts_complete,
            retry_counts=dict(self._health.retry_counts),
            source_failures=dict(self._health.source_failures),
            stale_sources=stale_sources,
            market_closed_sources=tuple(
                f"pyth:{asset.value}"
                for asset, state in pyth_states.items()
                if self._settings.enable_pyth_underlying and state is MarketDataState.MARKET_CLOSED
            ),
            underlying_market_states=pyth_states,
            worker_progress=dict(self._health.worker_progress),
            stale_workers=stale_workers,
            event_loop_lag_seconds=self._health.event_loop_lag_seconds,
            last_finalized_settlement=dict(self._health.last_finalized),
            written_records=self._health.written_records,
            integrity=self._health.integrity,
            robinhood_reference_healthy=self._health.robinhood_reference_healthy,
            fatal_task=self._health.fatal_task,
            fatal_error_type=self._health.fatal_error_type,
            kalshi_ws_connection_state=ws_state,
            kalshi_ws_synchronized_markets=dict(self._health.kalshi_ws_synchronized),
            kalshi_ws_last_books=dict(self._health.kalshi_ws_last_books),
            kalshi_ws_seq_gaps=self._health.kalshi_ws_seq_gaps,
            kalshi_ws_resync_count=self._health.kalshi_ws_resync_count,
            kalshi_ws_reconnect_count=(
                int(getattr(self._kalshi_ws.diagnostics, "reconnects", 0))
                if self._kalshi_ws is not None
                else (
                    self._sdk_recorder_host.reconnect_count
                    if self._sdk_recorder_host is not None
                    else 0
                )
            ),
            kalshi_ws_queue_high_watermark=(
                int(getattr(self._kalshi_ws.diagnostics, "receive_queue_high_watermark", 0))
                if self._kalshi_ws is not None
                else (
                    self._sdk_recorder_host.queue_high_watermark
                    if self._sdk_recorder_host is not None
                    else 0
                )
            ),
            kalshi_ws_queue_capacity=(
                int(getattr(self._kalshi_ws.diagnostics, "receive_queue_capacity", 0))
                if self._kalshi_ws is not None
                else 0
            ),
            kalshi_ws_queue_depth=(
                int(getattr(self._kalshi_ws.diagnostics, "receive_queue_depth", 0))
                if self._kalshi_ws is not None
                else 0
            ),
            kalshi_ws_queue_enqueued=(
                int(getattr(self._kalshi_ws.diagnostics, "receive_queue_enqueued", 0))
                if self._kalshi_ws is not None
                else 0
            ),
            kalshi_ws_queue_dequeued=(
                int(getattr(self._kalshi_ws.diagnostics, "receive_queue_dequeued", 0))
                if self._kalshi_ws is not None
                else 0
            ),
            kalshi_ws_queue_full_waits=(
                int(getattr(self._kalshi_ws.diagnostics, "receive_queue_full_waits", 0))
                if self._kalshi_ws is not None
                else 0
            ),
            kalshi_ws_queue_dropped=(
                int(getattr(self._kalshi_ws.diagnostics, "receive_queue_dropped", 0))
                if self._kalshi_ws is not None
                else 0
            ),
            kalshi_ws_queue_max_backlog_seconds=(
                float(
                    getattr(
                        self._kalshi_ws.diagnostics,
                        "receive_queue_max_backlog_seconds",
                        0.0,
                    )
                )
                if self._kalshi_ws is not None
                else 0.0
            ),
            kalshi_ws_queue_above_50_seconds=(
                float(getattr(self._kalshi_ws.diagnostics, "receive_queue_above_50_seconds", 0.0))
                if self._kalshi_ws is not None
                else 0.0
            ),
            kalshi_ws_queue_above_75_seconds=(
                float(getattr(self._kalshi_ws.diagnostics, "receive_queue_above_75_seconds", 0.0))
                if self._kalshi_ws is not None
                else 0.0
            ),
            kalshi_ws_queue_above_90_seconds=(
                float(getattr(self._kalshi_ws.diagnostics, "receive_queue_above_90_seconds", 0.0))
                if self._kalshi_ws is not None
                else 0.0
            ),
            kalshi_ws_receive_persist_latency_ms=(
                self._health.kalshi_ws_receive_persist_latency_ms
            ),
            kalshi_rest_fallback_status=(
                "healthy"
                if any(asset in self._health.last_quotes for asset in self._health.current)
                else "unavailable"
            ),
            ws_archive_metrics=dict(self._health.ws_archive_metrics),
        )

    def _expected_worker_thresholds(self) -> dict[str, float]:
        thresholds = {
            "coinbase": self._settings.recorder_coinbase_stale_seconds * 3,
            "checkpoint": self._settings.recorder_checkpoint_interval_seconds * 2,
        }
        for asset in Asset:
            thresholds[f"kalshi_discovery:{asset.value}"] = (
                self._settings.native_discovery_poll_interval_seconds * 3
            )
            thresholds[f"kalshi_quote:{asset.value}"] = max(
                15.0, self._settings.official_quote_poll_interval_seconds * 3
            )
            thresholds[f"kalshi_settlement:{asset.value}"] = (
                self._settings.settlement_followup_interval_seconds * 3
            )
        if self._settings.enable_pyth_underlying:
            thresholds["pyth"] = self._settings.recorder_pyth_stale_seconds * 3
        if self._settings.enable_secondary_underlying:
            for asset in (Asset.BNB, Asset.HYPE):
                thresholds[f"secondary:{asset.value}"] = (
                    self._settings.recorder_secondary_stale_seconds * 3
                )
        if self._settings.enable_kalshi_production_websocket:
            thresholds["kalshi_ws"] = self._settings.kalshi_websocket_stale_seconds * 3
            thresholds["kalshi_ws_persistence"] = 1.0
        if self._archive_service is not None:
            thresholds["ws_archive"] = max(
                60.0, self._settings.ws_archive_poll_interval_seconds * 30
            )
        if self._settings.enable_robinhood_reference and self._robinhood is not None:
            thresholds["robinhood_reference"] = self._settings.robinhood_poll_interval_seconds * 3
        return thresholds

    def _worker_advanced(self, key: str, observed: datetime | None = None) -> None:
        self._health.worker_progress[key] = observed or self._utc_now()

    def _observe_gap(
        self,
        source: GapSource,
        asset: Asset,
        received: datetime,
        *,
        source_health_key: str,
    ) -> None:
        """Close an append-only active gap when the next observation arrives."""

        stream = self._gap_streams[(source, asset)]
        received = received.astimezone(UTC)
        previous = self._gap_last.get((source, asset))
        key = (source, asset)
        recovered_active_ranges: set[tuple[datetime, datetime]] = set()
        unresolved: list[DataGap] = []
        for active in self._active_gaps.get(key, ()):
            if received <= active.gap_start:
                unresolved.append(active)
                continue
            intervals = self._gap_open_intervals(stream, active.gap_start, received)
            if not intervals:
                # A closed market cannot supply the real observation needed to
                # close this fact.  Keep it fail-closed rather than inventing an
                # endpoint.
                unresolved.append(active)
                continue
            gap_start, gap_end = intervals[0]
            if gap_start != active.gap_start or gap_end <= gap_start:
                unresolved.append(active)
                continue
            self._recover_gap(active, gap_end)
            recovered_active_ranges.add((gap_start, gap_end))
        if unresolved:
            self._active_gaps[key] = unresolved
        else:
            self._active_gaps.pop(key, None)
        if previous is not None and received > previous:
            for gap_start, gap_end in self._gap_open_intervals(stream, previous, received):
                if (gap_start, gap_end) in recovered_active_ranges:
                    continue
                if timedelta_seconds(gap_end - gap_start) <= stream.threshold_seconds:
                    continue
                opened = self._open_gap(
                    stream,
                    gap_start,
                    source_health_key=source_health_key,
                    detected_at=self._utc_now(),
                )
                self._recover_gap(opened, gap_end)
                remaining = [
                    active for active in self._active_gaps.get(key, ()) if active != opened
                ]
                if remaining:
                    self._active_gaps[key] = remaining
                else:
                    self._active_gaps.pop(key, None)
        if previous is None or received > previous:
            self._gap_last[(source, asset)] = received

    def _recover_gap(self, active: DataGap, gap_end: datetime) -> None:
        recovered = DataGap(
            source=active.source,
            asset=active.asset,
            instrument=active.instrument,
            gap_start=active.gap_start,
            gap_end=gap_end,
            detected_at=self._utc_now(),
            threshold_seconds=active.threshold_seconds,
            reason=active.reason,
            error_type=active.error_type,
            recovered=True,
            recorder_session_id=active.recorder_session_id,
            incident_id=active.incident_id,
        )
        if self._store.append_data_gap(recovered):
            self._wrote("data_gaps")

    @staticmethod
    def _gap_open_intervals(
        stream: GapStream, start: datetime, end: datetime
    ) -> tuple[tuple[datetime, datetime], ...]:
        # Session semantics belong to the asset, not only to one provider.
        # When a commodity has no live 15-minute market, Kalshi REST/WS and
        # its predictive underlying are all intentionally quiet. Treating the
        # closure as a source outage corrupts reliability and quarantine facts.
        return open_intervals_for_asset(stream.asset, start, end)

    def _open_gap(
        self,
        stream: GapStream,
        gap_start: datetime,
        *,
        source_health_key: str,
        detected_at: datetime,
        reason_override: GapReason | None = None,
    ) -> DataGap:
        reason = reason_override or GapReason.OBSERVATION_INTERVAL
        incident_id = None
        if reason_override is not None:
            incident_id = f"kalshi-ws:{self._health.started_at.isoformat()}"
        elif gap_start < self._health.started_at <= detected_at:
            reason = GapReason.RESTART
            incident_id = f"recorder-session:{self._health.started_at.isoformat()}"
        elif Decimal(str(self._health.event_loop_lag_seconds)) > stream.threshold_seconds:
            reason = GapReason.RUNTIME_STALL
            incident_id = f"event-loop-lag:{self._health.started_at.isoformat()}"
        elif source_health_key in self._health.source_failures:
            reason = GapReason.SOURCE_OUTAGE
        active = DataGap(
            source=stream.source,
            asset=stream.asset,
            instrument=stream.instrument,
            gap_start=gap_start,
            gap_end=None,
            detected_at=detected_at,
            threshold_seconds=stream.threshold_seconds,
            reason=reason,
            error_type=self._health.source_failures.get(source_health_key),
            recovered=False,
            recorder_session_id=self._health.started_at.isoformat(),
            incident_id=incident_id,
        )
        if self._store.append_data_gap(active):
            self._wrote("data_gaps")
        key = (stream.source, stream.asset)
        active_gaps = self._active_gaps.setdefault(key, [])
        if active not in active_gaps:
            active_gaps.append(active)
            active_gaps.sort(key=lambda gap: (gap.gap_start, gap.detected_at))
        return active

    def _open_due_gaps(self, observed: datetime) -> None:
        """Persist stale intervals once, before a source eventually recovers."""

        for key, previous in tuple(self._gap_last.items()):
            stream = self._gap_streams[key]
            unresolved: list[DataGap] = []
            for active in self._active_gaps.get(key, ()):
                intervals = self._gap_open_intervals(stream, active.gap_start, observed)
                if intervals and intervals[0][1] < observed:
                    self._recover_gap(active, intervals[0][1])
                else:
                    unresolved.append(active)
            if unresolved:
                self._active_gaps[key] = unresolved
            else:
                self._active_gaps.pop(key, None)
            if unresolved or not self._gap_stream_enabled(stream):
                continue
            intervals = self._gap_open_intervals(stream, previous, observed)
            if not intervals:
                continue
            gap_start, gap_end = intervals[-1]
            if (
                gap_end != observed
                or timedelta_seconds(gap_end - gap_start) <= stream.threshold_seconds
            ):
                continue
            self._open_gap(
                stream,
                gap_start,
                source_health_key=self._gap_source_health_key(stream),
                detected_at=observed,
            )

    def _gap_stream_enabled(self, stream: GapStream) -> bool:
        if stream.source is GapSource.KALSHI_WS:
            return self._settings.enable_kalshi_production_websocket
        if stream.source is GapSource.PYTH:
            return self._settings.enable_pyth_underlying
        if stream.source in {GapSource.BINANCE, GapSource.HYPERLIQUID}:
            return self._settings.enable_secondary_underlying
        return True

    @staticmethod
    def _gap_source_health_key(stream: GapStream) -> str:
        if stream.source is GapSource.KALSHI_REST:
            return f"kalshi_quote:{stream.asset.value}"
        if stream.source is GapSource.KALSHI_WS:
            return f"kalshi_ws:{stream.asset.value}"
        if stream.source is GapSource.COINBASE:
            return "coinbase"
        if stream.source is GapSource.PYTH:
            return f"pyth:{stream.asset.value}"
        return f"secondary:{stream.asset.value}"

    async def run(self) -> None:
        self._stop_event.clear()
        phase_started = self._startup_phase_started()
        tasks = [
            asyncio.create_task(self._record_coinbase(), name="coinbase-predictive"),
            asyncio.create_task(self._report_health(), name="kalshi-native-health"),
            asyncio.create_task(self._checkpoint(), name="sqlite-checkpoint"),
        ]
        if self._archive_service is not None:
            tasks.append(asyncio.create_task(self._archive_ws_retention(), name="ws-archive"))
        if self._settings.enable_pyth_underlying:
            tasks.append(asyncio.create_task(self._record_pyth(), name="pyth-predictive"))
            tasks.append(asyncio.create_task(self._monitor_pyth_liveness(), name="pyth-liveness"))
        if self._settings.enable_secondary_underlying:
            tasks.extend(
                asyncio.create_task(
                    self._record_secondary(asset),
                    name=f"secondary-{asset.value.lower()}",
                )
                for asset in (Asset.BNB, Asset.HYPE)
            )
        if self._kalshi_ws is not None:
            tasks.append(asyncio.create_task(self._record_kalshi_ws(), name="kalshi-ws"))
            tasks.append(
                asyncio.create_task(
                    self._publish_kalshi_ws_live_books(), name="kalshi-ws-live-projection"
                )
            )
            tasks.append(
                asyncio.create_task(self._flush_kalshi_ws_loop(), name="kalshi-ws-persistence")
            )
            tasks.append(
                asyncio.create_task(self._monitor_kalshi_ws_liveness(), name="kalshi-ws-liveness")
            )
        elif self._sdk_recorder_host is not None:
            tasks.append(asyncio.create_task(self._record_sdk_kalshi_ws(), name="kalshi-sdk-ws"))
            tasks.append(
                asyncio.create_task(
                    self._publish_kalshi_ws_live_books(), name="kalshi-ws-live-projection"
                )
            )
        for asset in KALSHI_15MIN_SERIES:
            tasks.extend(
                (
                    asyncio.create_task(
                        self._record_lifecycle_asset(asset),
                        name=f"kalshi-lifecycle-{asset.value}",
                    ),
                    asyncio.create_task(
                        self._record_quotes_asset(asset),
                        name=f"kalshi-quotes-{asset.value}",
                    ),
                    asyncio.create_task(
                        self._record_settlements_asset(asset),
                        name=f"kalshi-settlement-{asset.value}",
                    ),
                )
            )
        if self._settings.enable_robinhood_reference and self._robinhood is not None:
            tasks.append(
                asyncio.create_task(self._record_robinhood_reference(), name="robinhood-reference")
            )
        stop_task = asyncio.create_task(self._stop_event.wait(), name="recorder-stop")
        self._mark_startup_phase("worker_startup", phase_started)
        logger.info(
            "Kalshi-native recorder started",
            extra={
                "event": "kalshi_native_recorder_started",
                "database": str(self._store.path),
                "recovered_current": len(self._health.current),
                "recovered_followups": self._health.active_settlement_followups,
                "robinhood_reference_enabled": self._settings.enable_robinhood_reference,
            },
        )
        try:
            done, _ = await asyncio.wait([stop_task, *tasks], return_when=asyncio.FIRST_COMPLETED)
            completed_workers = [task for task in done if task is not stop_task]
            failed = next(
                (
                    task
                    for task in completed_workers
                    if not task.cancelled() and task.exception() is not None
                ),
                None,
            )
            if failed is not None:
                try:
                    failed.result()
                except Exception as error:
                    self._record_fatal_task(failed, error)
                    logger.exception(
                        "Recorder worker failed; shutting down",
                        extra={
                            "event": "recorder_worker_failed",
                            "task": failed.get_name(),
                            "error_type": type(error).__name__,
                        },
                    )
                    raise
            if stop_task not in done and completed_workers:
                exited = completed_workers[0]
                error = RuntimeError("recorder worker exited unexpectedly")
                self._record_fatal_task(exited, error)
                logger.error(
                    "Recorder worker exited unexpectedly; shutting down",
                    extra={
                        "event": "recorder_worker_exited",
                        "task": exited.get_name(),
                        "error_type": type(error).__name__,
                    },
                )
                raise error
        finally:
            stop_task.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(stop_task, *tasks, return_exceptions=True)
            drained = await asyncio.to_thread(
                self._wait_for_operations, self._settings.recorder_operation_timeout_seconds
            )
            if not drained:
                logger.error(
                    "Recorder shutdown timed out waiting for read-only HTTP operations",
                    extra={"event": "recorder_operation_drain_timeout"},
                )
            for client in self._owned_clients:
                client.close()
            if isinstance(self._robinhood, Robinhood15MinuteProvider):
                self._robinhood.close()
            if self._kalshi_ws is not None:
                await self._kalshi_ws.close()
                self._flush_kalshi_ws_pending()
            self._write_health_file(self.health())
            logger.info(
                "Kalshi-native recorder stopped",
                extra={"event": "kalshi_native_recorder_stopped", **self._health_fields()},
            )

    def _record_fatal_task(self, task: asyncio.Task[object], error: BaseException) -> None:
        self._health.fatal_task = task.get_name()
        self._health.fatal_error_type = type(error).__name__
        if isinstance(error, SettlementConflictError):
            event_type = RecorderEventType.SETTLEMENT_CONFLICT
        elif isinstance(error, MarketIdentityConflictError):
            event_type = RecorderEventType.MAPPING_CONFLICT
        elif isinstance(error, RecorderStorageError):
            event_type = RecorderEventType.SQLITE_FAILURE
        else:
            event_type = RecorderEventType.FATAL_TASK
        try:
            self._store.append_recorder_event(
                observed_timestamp=self._utc_now(),
                severity=RecorderEventSeverity.FATAL,
                event_type=event_type,
                source=task.get_name(),
                error_type=type(error).__name__,
                message=(
                    f"Recorder worker stopped on a correctness failure: {error}"
                    if isinstance(error, KalshiWsPayloadError)
                    else "Recorder worker stopped on a correctness failure"
                ),
            )
        except (RecorderStorageError, ValueError):
            logger.error(
                "Could not persist fatal recorder diagnostic",
                extra={"event": "fatal_diagnostic_unavailable"},
            )

    def _accept_market(self, market: KalshiMarket) -> None:
        prior = self._health.states.get(market.ticker)
        persisted = self._store.latest_kalshi_state(market.ticker)
        if persisted is not None:
            self._validate_market_identity(persisted, market)
            self._validate_finalized_result(persisted, market)
        if prior is None:
            prior = persisted.lifecycle if persisted is not None else None
        if prior is not None and KalshiLifecycleStateMachine.is_stale_regression(
            prior, market.lifecycle
        ):
            self._store.append_recorder_event(
                observed_timestamp=market.fetched_timestamp,
                severity=RecorderEventSeverity.WARNING,
                event_type=RecorderEventType.LIFECYCLE_REGRESSION,
                asset=market.asset,
                source="kalshi_settlement",
                error_type="StaleLifecycleRegression",
                message=f"Ignored stale {market.lifecycle.value}; retained {prior.value}",
                dedup_key=(
                    f"lifecycle-regression:{market.ticker}:{prior.value}:{market.lifecycle.value}"
                ),
            )
            return
        pending_states = {KalshiLifecycle.CLOSED, KalshiLifecycle.SETTLEMENT_PENDING}
        was_pending = prior in pending_states
        settlement_existed = (
            self._store.has_kalshi_settlement(market.ticker)
            if market.settlement is not None
            else False
        )
        for observation in KalshiLifecycleStateMachine.observations(prior, market):
            if self._store.append_kalshi_market(observation):
                self._wrote("kalshi_market_lifecycle")
            self._health.states[market.ticker] = observation.lifecycle
        is_pending = self._health.states.get(market.ticker) in pending_states
        if is_pending and not was_pending:
            self._health.active_settlement_followups += 1
        elif was_pending and not is_pending:
            if self._health.active_settlement_followups <= 0:
                raise RecorderStorageError("settlement follow-up count would move below zero")
            self._health.active_settlement_followups -= 1
        if market.settlement is not None:
            if not settlement_existed:
                self._wrote("kalshi_settlements")
            self._health.last_finalized[market.asset] = (
                f"{market.ticker}:{market.settlement.result.value}"
            )

    def _validate_market_identity(
        self, persisted: KalshiMarketRecord, observed: KalshiMarket
    ) -> None:
        if (
            persisted.asset is not observed.asset
            or persisted.series != observed.series
            or persisted.ticker != observed.ticker
            or persisted.event_ticker != observed.event_ticker
            or persisted.window_start != observed.window_start
            or persisted.window_end != observed.window_end
            or persisted.target != observed.target
        ):
            self._store.append_recorder_event(
                observed_timestamp=observed.fetched_timestamp,
                severity=RecorderEventSeverity.FATAL,
                event_type=RecorderEventType.MAPPING_CONFLICT,
                asset=observed.asset,
                source="kalshi_market",
                error_type="MarketIdentityConflict",
                message="Conflicting official market identity rejected",
                dedup_key=f"market-identity-conflict:{observed.ticker}",
            )
            raise KalshiPublicApiError("conflicting official market identity")

    def _validate_finalized_result(
        self, persisted: KalshiMarketRecord, observed: KalshiMarket
    ) -> None:
        expected = {
            KalshiLifecycle.SETTLED_YES: "yes",
            KalshiLifecycle.SETTLED_NO: "no",
        }.get(persisted.lifecycle)
        observed_result = (
            observed.settlement.result.value
            if observed.settlement is not None
            else (
                observed.determination_result.value
                if observed.determination_result is not None
                else None
            )
        )
        if expected is None or observed_result is None or observed_result == expected:
            return
        self._store.append_recorder_event(
            observed_timestamp=observed.fetched_timestamp,
            severity=RecorderEventSeverity.FATAL,
            event_type=RecorderEventType.SETTLEMENT_CONFLICT,
            asset=observed.asset,
            source="kalshi_settlement",
            error_type="OfficialResultConflict",
            message="Conflicting official result rejected; immutable settlement retained",
            dedup_key=f"official-result-conflict:{observed.ticker}:{observed_result}",
        )
        raise KalshiPublicApiError("conflicting official result after settlement")

    def _wrote(self, table: str) -> None:
        self._health.written_records += 1
        self._health.row_counts[table] += 1

    def _accept_discovery(self, discovery: KalshiDiscovery) -> None:
        now = self._utc_now()
        self._health.last_discovery[discovery.asset] = discovery.fetched_timestamp
        for market in discovery.valid_markets:
            self._accept_market(market)
        relevant_tickers = {market.ticker for market in discovery.valid_markets}
        series_prefix = f"{KALSHI_15MIN_SERIES[discovery.asset]}-"
        for ticker in tuple(self._health.states):
            if ticker.startswith(series_prefix) and ticker not in relevant_tickers:
                self._health.states.pop(ticker, None)
        market = discovery.current
        previous = self._health.current.get(discovery.asset)
        if (
            market is not None
            and market.lifecycle is KalshiLifecycle.OPEN
            and now < market.window_end
        ):
            self._health.current[discovery.asset] = market
            if previous is None or previous.ticker != market.ticker:
                logger.info(
                    "Kalshi-native market rollover",
                    extra={
                        "event": "kalshi_native_rollover",
                        "asset": discovery.asset,
                        "previous_ticker": previous.ticker if previous else None,
                        "current_ticker": market.ticker,
                        "window_start": market.window_start,
                        "rollover_observation_latency_seconds": max(
                            0.0,
                            (discovery.fetched_timestamp - market.window_start).total_seconds(),
                        ),
                    },
                )
        else:
            self._health.current.pop(discovery.asset, None)

    def _source_failed(self, key: str, error: BaseException) -> None:
        self._health.retry_counts[key] = self._health.retry_counts.get(key, 0) + 1
        consecutive = self._health.consecutive_failures.get(key, 0) + 1
        self._health.consecutive_failures[key] = consecutive
        self._health.source_failures[key] = type(error).__name__
        if consecutive == 1 or consecutive & (consecutive - 1) == 0:
            asset = next(
                (item for item in Asset if key.endswith(f":{item.value}")),
                None,
            )
            self._store.append_recorder_event(
                observed_timestamp=self._utc_now(),
                severity=RecorderEventSeverity.WARNING,
                event_type=RecorderEventType.SOURCE_UNAVAILABLE,
                asset=asset,
                source=key,
                error_type=type(error).__name__,
                message="Source temporarily unavailable; bounded retry scheduled",
                dedup_key=f"source-unavailable:{key}:{type(error).__name__}:{consecutive}",
            )
            logger.warning(
                "Recorder source temporarily unavailable",
                extra={
                    "event": "recorder_source_unavailable",
                    "source_key": key,
                    "consecutive_failures": consecutive,
                    "error_type": type(error).__name__,
                },
            )

    def _source_ok(self, key: str) -> None:
        self._health.consecutive_failures.pop(key, None)
        self._health.source_failures.pop(key, None)

    def _retry_delay(self, key: str, base: float) -> float:
        failures = self._health.consecutive_failures.get(key, 0)
        if failures == 0:
            return base
        return min(
            self._settings.recorder_max_backoff_seconds,
            base * 2 ** min(failures - 1, 8),
        )

    async def _wait(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    async def _call(
        self, function: Callable[..., object], *args: object, **kwargs: object
    ) -> object:
        return await asyncio.wait_for(
            asyncio.to_thread(self._invoke, function, args, kwargs),
            timeout=self._settings.recorder_operation_timeout_seconds,
        )

    def _invoke(
        self,
        function: Callable[..., object],
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> object:
        with self._operation_condition:
            self._active_operations += 1
        try:
            return function(*args, **kwargs)
        finally:
            with self._operation_condition:
                self._active_operations -= 1
                self._operation_condition.notify_all()

    def _wait_for_operations(self, timeout: float) -> bool:
        with self._operation_condition:
            return self._operation_condition.wait_for(
                lambda: self._active_operations == 0, timeout=timeout
            )

    async def _record_lifecycle_asset(self, asset: Asset) -> None:
        while not self._stop_event.is_set():
            key = f"kalshi_discovery:{asset.value}"
            try:
                result = await self._call(self._discoveries[asset].discover, asset)
                if not isinstance(result, KalshiDiscovery):
                    raise KalshiPublicApiError("discovery returned an invalid object")
                self._accept_discovery(result)
                self._source_ok(key)
            except asyncio.CancelledError:
                raise
            except (
                requests.RequestException,
                TimeoutError,
                KalshiTargetUnavailableError,
            ) as error:
                self._source_failed(key, error)
            except (KalshiPublicApiError, RecorderStorageError, ValueError):
                raise
            self._worker_advanced(key)
            if await self._wait(
                self._retry_delay(key, self._settings.native_discovery_poll_interval_seconds)
            ):
                return

    async def _record_quotes_asset(self, asset: Asset) -> None:
        while not self._stop_event.is_set():
            market = self._health.current.get(asset)
            if market is not None:
                key = f"kalshi_quote:{asset.value}"
                try:
                    result = await self._call(self._quotes[asset].quote_native, market)
                    if not isinstance(result, KalshiNativeQuote):
                        raise KalshiPublicApiError("quote source returned an invalid object")
                    quote = result
                    if quote.ticker != market.ticker:
                        raise KalshiPublicApiError("quote source returned another instrument")
                    if quote.received_timestamp >= market.window_end:
                        self._health.current.pop(asset, None)
                    else:
                        if self._store.append_kalshi_quote(quote):
                            self._wrote("kalshi_prediction_quotes")
                            self._observe_gap(
                                GapSource.KALSHI_REST,
                                asset,
                                quote.received_timestamp,
                                source_health_key=key,
                            )
                        self._health.last_quotes[asset] = quote.received_timestamp
                        self._source_ok(key)
                except asyncio.CancelledError:
                    raise
                except (
                    requests.RequestException,
                    TimeoutError,
                    KalshiTargetUnavailableError,
                ) as error:
                    self._source_failed(key, error)
                except (KalshiPublicApiError, RecorderStorageError, ValueError):
                    raise
            key = f"kalshi_quote:{asset.value}"
            self._worker_advanced(key)
            if await self._wait(
                self._retry_delay(key, self._settings.official_quote_poll_interval_seconds)
            ):
                return

    async def _record_settlements_asset(self, asset: Asset) -> None:
        while not self._stop_event.is_set():
            now = self._utc_now()
            cursor = self._followup_cursors[asset]
            batch = self._store.unsettled_kalshi_markets(
                now=now,
                asset=asset,
                after_ticker=cursor,
                limit=self._settings.settlement_followup_batch_size,
            )
            if not batch and cursor is not None:
                self._followup_cursors[asset] = None
                batch = self._store.unsettled_kalshi_markets(
                    now=now,
                    asset=asset,
                    limit=self._settings.settlement_followup_batch_size,
                )
            for record in batch:
                self._followup_cursors[asset] = record.ticker
                key = f"kalshi_settlement:{asset.value}"
                try:
                    try:
                        market = await self._call(
                            self._settlements[asset].get_market,
                            asset,
                            record.ticker,
                        )
                    except requests.HTTPError as error:
                        if error.response is None or error.response.status_code != 404:
                            raise
                        market = await self._call(
                            self._settlements[asset].get_market,
                            asset,
                            record.ticker,
                            historical=True,
                        )
                    if not isinstance(market, KalshiMarket):
                        raise KalshiPublicApiError("settlement follow-up returned invalid object")
                    self._accept_market(market)
                    self._source_ok(key)
                except asyncio.CancelledError:
                    raise
                except (
                    requests.RequestException,
                    TimeoutError,
                    KalshiTargetUnavailableError,
                ) as error:
                    self._source_failed(key, error)
                except (KalshiPublicApiError, RecorderStorageError, ValueError):
                    raise
            key = f"kalshi_settlement:{asset.value}"
            self._worker_advanced(key)
            if await self._wait(
                self._retry_delay(key, self._settings.settlement_followup_interval_seconds)
            ):
                return

    async def _record_coinbase(self) -> None:
        while not self._stop_event.is_set():
            try:
                async for tick in self._coinbase_factory().ticks():
                    if self._store.append_coinbase(tick):
                        self._wrote("coinbase_ticks")
                        asset = next(
                            (
                                candidate
                                for candidate, product in COINBASE_PRODUCT_BY_ASSET.items()
                                if product == tick.symbol
                            ),
                            None,
                        )
                        if asset is not None:
                            self._observe_gap(
                                GapSource.COINBASE,
                                asset,
                                tick.received_at,
                                source_health_key="coinbase",
                            )
                    self._health.last_coinbase[tick.symbol] = tick.received_at
                    self._source_ok("coinbase")
                    self._worker_advanced("coinbase")
                    if self._stop_event.is_set():
                        return
                    # WebSocket implementations can satisfy recv() immediately
                    # from an internal backlog. Cooperatively yield after each
                    # durable tick so timers/control and other sources progress.
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except (RecorderStorageError, ValueError):
                raise
            except Exception as error:
                self._source_failed("coinbase", error)
                self._worker_advanced("coinbase")
            if await self._wait(self._settings.reconnect_delay_seconds):
                return

    async def _record_pyth(self) -> None:
        """Consume one five-feed SSE stream, with one batch-REST request per outage cycle."""

        demultiplexer = PythFeedDemultiplexer()
        stream_key = "pyth:stream"
        client = self._underlying_factory()
        try:
            while not self._stop_event.is_set():
                retry_after = 0.0
                rate_limited = False
                try:
                    iterator = client.stream_batches()
                    while not self._stop_event.is_set():
                        batch = await asyncio.to_thread(_next_pyth_batch, iterator)
                        if batch is None:
                            raise PythNetworkError("Pyth stream closed")
                        accepted = self._accept_pyth_batch(demultiplexer.accept(batch))
                        if accepted:
                            self._source_ok(stream_key)
                            self._worker_advanced("pyth")
                except asyncio.CancelledError:
                    raise
                except (RecorderStorageError, ValueError) as error:
                    if not isinstance(error, PythPayloadError):
                        raise
                    self._source_failed(stream_key, error)
                except PythNetworkError as error:
                    self._source_failed(stream_key, error)
                    if isinstance(error, PythRateLimitError):
                        retry_after = error.retry_after_seconds
                        rate_limited = True

                if self._stop_event.is_set():
                    return
                if not rate_limited:
                    try:
                        batch = await asyncio.to_thread(client.latest_batch)
                        accepted = self._accept_pyth_batch(demultiplexer.accept(batch))
                        if accepted:
                            self._source_ok("pyth:rest_fallback")
                            self._worker_advanced("pyth")
                    except asyncio.CancelledError:
                        raise
                    except (RecorderStorageError, ValueError) as error:
                        if not isinstance(error, PythPayloadError):
                            raise
                        self._source_failed("pyth:rest_fallback", error)
                    except PythNetworkError as error:
                        self._source_failed("pyth:rest_fallback", error)
                        if isinstance(error, PythRateLimitError):
                            retry_after = max(retry_after, error.retry_after_seconds)
                delay = max(
                    retry_after,
                    self._retry_delay(
                        stream_key, self._settings.pyth_rest_fallback_interval_seconds
                    ),
                )
                if await self._wait(delay):
                    return
        finally:
            # Must run even while this task is already cancelled; awaiting another
            # to_thread here can re-raise cancellation before the SSE response closes.
            client.close()

    async def _monitor_pyth_liveness(self) -> None:
        """Escalate a live-but-stalled Pyth worker to the service supervisor.

        Worker progress is advanced only after a valid observation batch is
        accepted.  If neither SSE nor REST fallback produces one within the
        existing bounded worker threshold, raising here deliberately terminates
        the recorder so WinSW can perform one clean, supervised restart.
        """

        interval = min(5.0, max(1.0, self._settings.recorder_pyth_stale_seconds))
        while not await self._wait(interval):
            health = self.health()
            if "pyth" not in health.stale_workers:
                continue
            error = health.source_failures.get("pyth:stream") or health.source_failures.get(
                "pyth:rest_fallback"
            )
            detail = f" ({error})" if error else ""
            raise RuntimeError(f"Pyth worker made no meaningful progress{detail}")

    def _accept_pyth_batch(self, batch: PythUpdateBatch) -> int:
        accepted = 0
        for issue in batch.issues:
            if issue.code == "duplicate":
                continue
            key = f"pyth:{issue.asset.value}" if issue.asset is not None else "pyth:stream"
            self._source_failed(key, PythPayloadError(issue.code))
        for observation in batch.observations:
            accepted += 1
            if self._store.append_underlying(observation):
                self._wrote("underlying_observations")
                self._observe_gap(
                    GapSource.PYTH,
                    observation.asset,
                    observation.received_timestamp,
                    source_health_key=f"pyth:{observation.asset.value}",
                )
            self._health.last_additional_underlying[observation.asset] = (
                observation.received_timestamp
            )
            self._health.additional_underlying_freshness[observation.asset] = observation.freshness
            self._source_ok(f"pyth:{observation.asset.value}")
        return accepted

    async def _record_secondary(self, asset: Asset) -> None:
        """Persist one isolated venue-native secondary stream in the same recorder."""

        factory = self._secondary_factories[asset]
        failures = 0
        completed_reconnects = 0
        completed_malformed = 0
        while not self._stop_event.is_set():
            source = factory()
            key = f"secondary:{asset.value}"
            try:
                async for tick in source.ticks():
                    self._health.secondary_diagnostics[f"{asset.value}:reconnects"] = (
                        completed_reconnects + source.diagnostics.reconnects
                    )
                    self._health.secondary_diagnostics[f"{asset.value}:malformed"] = (
                        completed_malformed + source.diagnostics.malformed_messages
                    )
                    observation = secondary_from_benchmark_tick(
                        tick,
                        max_source_age_seconds=self._settings.recorder_secondary_stale_seconds,
                    )
                    if observation.asset is not asset:
                        raise ValueError("secondary source returned another asset")
                    status = self._store.append_secondary_underlying(observation)
                    health_key = f"{asset.value}:{observation.provider.value}"
                    if status is SecondaryAppendStatus.INSERTED:
                        self._wrote("secondary_underlying_observations")
                        self._observe_gap(
                            (GapSource.BINANCE if asset is Asset.BNB else GapSource.HYPERLIQUID),
                            asset,
                            observation.received_timestamp,
                            source_health_key=key,
                        )
                        stored = self._store.latest_secondary_underlying(
                            asset, observation.provider
                        )
                        if stored is not None and stored.receive_persist_latency_ms is not None:
                            self._health.secondary_persist_latency_ms[health_key] = str(
                                stored.receive_persist_latency_ms
                            )
                    elif status is SecondaryAppendStatus.DUPLICATE:
                        self._increment_secondary_diagnostic(asset, "duplicates")
                        self._source_ok(key)
                        self._worker_advanced(key)
                        # A busy venue stream can have an already-buffered next message.
                        # Yield explicitly so health, lifecycle, and the other sources
                        # cannot be starved while this queue drains.
                        await asyncio.sleep(0)
                        continue
                    elif status is SecondaryAppendStatus.OUT_OF_ORDER:
                        self._increment_secondary_diagnostic(asset, "out_of_order")
                        self._source_failed(key, ValueError("out-of-order source observation"))
                        self._worker_advanced(key)
                        await asyncio.sleep(0)
                        continue
                    self._health.last_secondary_underlying[health_key] = (
                        observation.received_timestamp
                    )
                    self._source_ok(key)
                    self._worker_advanced(key)
                    failures = 0
                    if self._stop_event.is_set():
                        return
                    await asyncio.sleep(0)
                if not self._stop_event.is_set():
                    raise ConnectionError("secondary source stream ended")
            except asyncio.CancelledError:
                raise
            except RecorderStorageError:
                raise
            except (
                BenchmarkPayloadError,
                ConnectionError,
                OSError,
                TimeoutError,
                ValueError,
            ) as error:
                self._source_failed(key, error)
                self._worker_advanced(key)
            finally:
                completed_reconnects += source.diagnostics.reconnects
                completed_malformed += source.diagnostics.malformed_messages
                self._health.secondary_diagnostics[f"{asset.value}:reconnects"] = (
                    completed_reconnects
                )
                self._health.secondary_diagnostics[f"{asset.value}:malformed"] = completed_malformed
                await source.close()
            failures += 1
            delay = min(
                self._settings.recorder_max_backoff_seconds,
                self._settings.reconnect_delay_seconds * (2 ** min(failures - 1, 8)),
            )
            if await self._wait(delay):
                return

    def _increment_secondary_diagnostic(self, asset: Asset, name: str) -> None:
        key = f"{asset.value}:{name}"
        self._health.secondary_diagnostics[key] = self._health.secondary_diagnostics.get(key, 0) + 1

    def synchronized_kalshi_ws_book(self, ticker: str) -> SynchronizedKalshiOrderBook:
        """Return the live primary only while the official WS state is synchronized."""

        coordinator = self._kalshi_ws_coordinator
        if self._sdk_recorder_host is not None:
            if (
                self._health.kalshi_ws_state is not KalshiWsRuntimeState.SYNCHRONIZED
                or not self._kalshi_ws_transport_fresh(self._utc_now())
            ):
                raise KalshiUnsynchronizedBookError("Kalshi SDK WS primary is unavailable")
            for book in self._kalshi_ws_books.values():
                if book.ticker == ticker:
                    return book
            raise KalshiUnsynchronizedBookError("Kalshi SDK WS ticker is unavailable")
        diagnostics = None if self._kalshi_ws is None else self._kalshi_ws.diagnostics
        transport_state = getattr(diagnostics, "transport_state", None)
        dropped = int(getattr(diagnostics, "receive_queue_dropped", 0))
        if (
            coordinator is None
            or self._health.kalshi_ws_state is not KalshiWsRuntimeState.SYNCHRONIZED
            or transport_state
            in {KalshiWsRuntimeState.CONNECTING, KalshiWsRuntimeState.RECONNECTING}
            or dropped != 0
            or not self._kalshi_ws_transport_fresh(self._utc_now())
        ):
            raise KalshiUnsynchronizedBookError("Kalshi WS primary is unavailable")
        return coordinator.book(ticker)

    def _kalshi_ws_transport_fresh(self, observed: datetime) -> bool:
        """Keep a quiet book usable only while the official WS transport is live."""

        if self._sdk_recorder_host is not None:
            received = self._sdk_recorder_host.last_received_at
            if received is None:
                return False
            age = (observed - received.astimezone(UTC)).total_seconds()
            return 0 <= age <= self._settings.kalshi_websocket_stale_seconds
        source = self._kalshi_ws
        if source is None:
            return False
        diagnostics = getattr(source, "diagnostics", None)
        if diagnostics is None:
            return True
        received = getattr(diagnostics, "last_message_received_at", None)
        # Deterministic legacy test sources do not expose transport diagnostics.
        if received is None:
            return not hasattr(diagnostics, "last_message_received_at")
        if not isinstance(received, datetime) or received.tzinfo is None:
            return False
        age = (observed - received.astimezone(UTC)).total_seconds()
        return 0 <= age <= self._settings.kalshi_websocket_stale_seconds

    async def _enforce_kalshi_ws_liveness(self, observed: datetime) -> bool:
        """Fail closed when transport or the initial snapshot set stops progressing."""

        if self._kalshi_ws is None:
            return False
        state = self._health.kalshi_ws_state
        transport_stale = (
            state is KalshiWsRuntimeState.SYNCHRONIZED
            and not self._kalshi_ws_transport_fresh(observed)
        )
        snapshot_stalled = (
            state in {KalshiWsRuntimeState.WAITING_SNAPSHOT, KalshiWsRuntimeState.UNSYNCHRONIZED}
            and self._kalshi_ws_waiting_since_monotonic is not None
            and self._monotonic() - self._kalshi_ws_waiting_since_monotonic
            > self._settings.kalshi_websocket_stale_seconds
        )
        if not transport_stale and not snapshot_stalled:
            return False
        self._mark_kalshi_ws_unsynchronized(GapReason.RECONNECT)
        # One reconnect request is enough.  The next connection establishes a
        # new monotonic snapshot deadline; do not busy-loop while close drains.
        self._kalshi_ws_waiting_since_monotonic = None
        await self._kalshi_ws.request_reconnect()
        return True

    async def _monitor_kalshi_ws_liveness(self) -> None:
        interval = min(1.0, self._settings.kalshi_websocket_stale_seconds / 2)
        while not await self._wait(interval):
            await self._enforce_kalshi_ws_liveness(self._utc_now())

    def _flush_kalshi_ws_pending(self) -> None:
        if not self._kalshi_ws_pending:
            return
        pending = tuple(self._kalshi_ws_pending)
        inserted, latency = self._store.write_kalshi_ws_persistence_event_batch_atomic(
            tuple(
                KalshiWsPersistenceEvent.from_legacy(message, sync_status_after=sync_status_after)
                for message, sync_status_after in pending
            )
        )
        del self._kalshi_ws_pending[: len(pending)]
        if inserted:
            self._health.row_counts["kalshi_ws_orderbook_events"] = (
                self._health.row_counts.get("kalshi_ws_orderbook_events", 0) + inserted
            )
            self._health.written_records += inserted
        if latency is not None:
            self._health.kalshi_ws_receive_persist_latency_ms = str(latency)

    async def _flush_kalshi_ws_loop(self) -> None:
        """Bound durable latency without committing every high-rate delta separately."""

        while not await self._wait(0.025):
            self._flush_kalshi_ws_pending()
            self._worker_advanced("kalshi_ws_persistence")

    def _kalshi_ws_live_projection(self) -> dict[str, object]:
        """One immutable cross-process snapshot of current synchronized books."""

        observed = self._utc_now().astimezone(UTC)
        transport_received = self._health.worker_progress.get("kalshi_ws")
        transport_age = (
            None if transport_received is None else timedelta_seconds(observed - transport_received)
        )
        transport_fresh = transport_age is not None and Decimal(0) <= transport_age <= Decimal(
            str(self._settings.kalshi_websocket_stale_seconds)
        )
        synchronized = (
            self._health.kalshi_ws_state is KalshiWsRuntimeState.SYNCHRONIZED and transport_fresh
        )
        books: dict[str, object] = {}
        if synchronized:
            for asset, book in sorted(
                self._kalshi_ws_books.items(), key=lambda item: item[0].value
            ):
                if self._health.kalshi_ws_synchronized.get(asset) != book.ticker:
                    continue
                books[book.ticker] = {
                    "ticker": book.ticker,
                    "asset": asset.value,
                    "connection_id": book.connection_id,
                    "subscription_id": book.subscription_id,
                    "sequence": book.sequence,
                    "status": book.status.value.upper(),
                    "provenance": book.provenance,
                    "source_timestamp": (
                        None if book.source_timestamp is None else book.source_timestamp.isoformat()
                    ),
                    "book_received_at": book.received_timestamp.isoformat(),
                    "yes_bids": [
                        [str(level.price), str(level.quantity)]
                        for level in sorted(
                            book.yes_bids, key=lambda level: level.price, reverse=True
                        )[:_LIVE_WS_PROJECTION_DEPTH]
                    ],
                    "no_bids": [
                        [str(level.price), str(level.quantity)]
                        for level in sorted(
                            book.no_bids, key=lambda level: level.price, reverse=True
                        )[:_LIVE_WS_PROJECTION_DEPTH]
                    ],
                }
        return {
            "schema_version": 1,
            "state": "SYNCHRONIZED" if synchronized else "UNSYNCHRONIZED",
            "published_at": observed.isoformat(),
            "transport_received_at": (
                None if transport_received is None else transport_received.isoformat()
            ),
            "current_tickers": sorted(market.ticker for market in self._health.current.values()),
            "books": books,
        }

    async def _publish_kalshi_ws_live_books(self) -> None:
        """Publish coalesced live books without blocking the recorder event loop."""

        path = self._settings.recorder_health_path.with_name("kalshi-live-ws-books.json")
        while not self._stop_event.is_set():
            payload = self._kalshi_ws_live_projection()
            try:
                await asyncio.to_thread(atomic_json, path, payload)
            except OSError as error:
                logger.warning(
                    "Kalshi live-book projection write failed",
                    extra={
                        "event": "kalshi_ws_live_projection_write_failed",
                        "error_type": type(error).__name__,
                    },
                )
            if await self._wait(0.2):
                return

    def _mark_kalshi_ws_unsynchronized(self, reason: GapReason) -> None:
        self._health.kalshi_ws_state = (
            KalshiWsRuntimeState.RECONNECTING
            if reason is GapReason.RECONNECT
            else KalshiWsRuntimeState.UNSYNCHRONIZED
        )
        self._health.kalshi_ws_synchronized.clear()
        self._kalshi_ws_books.clear()
        detected = self._utc_now()
        for asset in self._health.current:
            key = (GapSource.KALSHI_WS, asset)
            if self._active_gaps.get(key):
                continue
            start = self._gap_last.get(key, self._health.kalshi_ws_last_books.get(asset, detected))
            self._open_gap(
                self._gap_streams[key],
                start,
                source_health_key=f"kalshi_ws:{asset.value}",
                detected_at=detected,
                reason_override=reason,
            )

    async def _send_kalshi_ws_payload(self, payload: str) -> None:
        if self._kalshi_ws is None:
            raise KalshiReadOnlyWsError("Kalshi WebSocket is not configured")
        decoded = json.loads(payload)
        if not isinstance(decoded, dict) or not isinstance(decoded.get("id"), int):
            raise KalshiReadOnlyWsError("invalid typed Kalshi WebSocket command")
        await self._kalshi_ws.send_command(KalshiSubscriptionCommand(decoded["id"], payload))

    async def _record_kalshi_ws(self) -> None:
        """Keep transport outages isolated while correctness/storage failures remain fatal."""

        while not self._stop_event.is_set():
            try:
                await self._record_kalshi_ws_session()
                if self._stop_event.is_set():
                    return
                raise ConnectionError("Kalshi WebSocket stream ended")
            except asyncio.CancelledError:
                raise
            except RecorderStorageError:
                self._health.kalshi_ws_state = KalshiWsRuntimeState.UNSYNCHRONIZED
                self._health.kalshi_ws_synchronized.clear()
                self._kalshi_ws_books.clear()
                raise
            except (KalshiBookInvariantError, KalshiReadOnlyWsError, ValueError):
                self._mark_kalshi_ws_unsynchronized(GapReason.SOURCE_OUTAGE)
                raise
            except (ConnectionError, OSError, TimeoutError) as error:
                self._source_failed("kalshi_ws", error)
                self._mark_kalshi_ws_unsynchronized(GapReason.RECONNECT)
                self._worker_advanced("kalshi_ws")
                if await self._wait(
                    self._retry_delay("kalshi_ws", self._settings.reconnect_delay_seconds)
                ):
                    return

    def _sdk_ws_universe(self) -> Mapping[Asset, str]:
        """Current recorder-owned market identity for the SDK transport host."""

        return {asset: market.ticker for asset, market in self._health.current.items()}

    def _on_sdk_ws_transport_state(self, state: str, observed_at: datetime) -> None:
        """Project transport state into existing Recorder health/gap semantics."""

        normalized = state.lower()
        if normalized in {"reconnecting", "disconnected", "closed"}:
            self._mark_kalshi_ws_unsynchronized(GapReason.RECONNECT)
        elif normalized in {"connecting", "connected"} and (
            self._health.kalshi_ws_state is not KalshiWsRuntimeState.SYNCHRONIZED
        ):
            self._health.kalshi_ws_state = KalshiWsRuntimeState.WAITING_SNAPSHOT
        self._worker_advanced("kalshi_ws", observed_at)

    def _on_sdk_ws_committed(self, events: tuple[RecorderMarketDataEvent, ...]) -> None:
        """Expose only durably committed, adapter-authoritative SDK books."""

        # RecorderMarketDataConsumer invokes this callback only after the
        # shared neutral batch writer has committed.  Keep the established
        # legacy health key on that same success-only durability boundary;
        # failed/rolled-back SDK batches never reach this callback.
        if events:
            self._worker_advanced("kalshi_ws_persistence")
        for event in events:
            book = event.book
            if not event.authoritative or book is None:
                continue
            asset = event.canonical.asset
            current = self._health.current.get(asset)
            if current is None or current.ticker != book.ticker:
                continue
            self._kalshi_ws_books[asset] = book
            self._capture_forward_shadow_checkpoint(asset, book)
            self._health.kalshi_ws_synchronized[asset] = book.ticker
            self._health.kalshi_ws_last_books[asset] = book.received_timestamp
            self._observe_gap(
                GapSource.KALSHI_WS,
                asset,
                book.received_timestamp,
                source_health_key=f"kalshi_ws:{asset.value}",
            )
            self._source_ok(f"kalshi_ws:{asset.value}")
            if event.canonical.event_type.value == "SNAPSHOT":
                if self._store.append_kalshi_ws_checkpoint(book):
                    self._wrote("kalshi_ws_book_checkpoints")
        current_assets = set(self._health.current)
        if self._sdk_recorder_host is not None:
            self._health.kalshi_ws_seq_gaps = self._sdk_recorder_host.gap_count
        if current_assets and current_assets.issubset(self._health.kalshi_ws_synchronized):
            self._health.kalshi_ws_state = KalshiWsRuntimeState.SYNCHRONIZED
            self._kalshi_ws_waiting_since_monotonic = None
            if not self._startup_ws_synchronized_reported:
                self._startup_ws_synchronized_reported = True
                self._mark_startup_phase("kalshi_ws_synchronized", self._startup_phase_started())
        self._worker_advanced("kalshi_ws")

    async def _record_sdk_kalshi_ws(self) -> None:
        """Production SDK transport; legacy WS code is never entered on this path."""

        host = self._sdk_recorder_host
        if host is None:
            return
        await host.run(self._stop_event)

    def _capture_forward_shadow_checkpoint(
        self, asset: Asset, book: SynchronizedKalshiOrderBook
    ) -> None:
        """Persist a bounded, pre-decision WS book for frozen forward validation.

        Raw deltas remain the lossless truth.  This sparse checkpoint is only a
        locally materialized as-of boundary: it is captured before, never after,
        each configured decision timestamp.  At most one extra checkpoint is
        written per ``asset/ticker/decision bucket``.
        """

        market = self._health.current.get(asset)
        if market is None or market.ticker != book.ticker:
            return
        key = (asset, book.ticker)
        completed = self._forward_shadow_checkpointed.setdefault(key, set())
        for stale_key in tuple(self._forward_shadow_checkpointed):
            if stale_key[0] is asset and stale_key != key:
                self._forward_shadow_checkpointed.pop(stale_key, None)
        # The receive pump is high-rate, so a five-second pre-decision window
        # reliably captures an actual book without accepting a future update.
        capture_window = timedelta(seconds=5)
        for offset in self._settings.dataset_decision_offsets_seconds:
            if offset in completed:
                continue
            decision = market.window_end - timedelta(seconds=offset)
            if not decision - capture_window <= book.received_timestamp <= decision:
                continue
            if self._store.append_kalshi_ws_checkpoint(book):
                self._wrote("kalshi_ws_book_checkpoints")
            completed.add(offset)

    async def _record_kalshi_ws_session(self) -> None:
        """Persist one official stream and expose only synchronized atomic books."""

        source = self._kalshi_ws
        if source is None:
            return
        while not self._stop_event.is_set() and not self._health.current:
            self._health.kalshi_ws_state = KalshiWsRuntimeState.CONNECTING
            self._worker_advanced("kalshi_ws")
            if await self._wait(0.1):
                return
        desired_by_asset = {asset: market.ticker for asset, market in self._health.current.items()}
        source.set_reconnect_tickers(tuple(desired_by_asset.values()))
        connection_id: str | None = None
        coordinator: KalshiAtomicOrderBookCoordinator | None = None
        processor: KalshiAtomicSessionProcessor | None = None
        ticker_assets = {ticker: asset for asset, ticker in desired_by_asset.items()}
        pending_removals: dict[str, str] = {}
        pending_delete_requests: dict[int, str] = {}
        predecessor_by_asset: dict[Asset, str] = {}
        request_id = 20_000
        async for message in source.messages(tuple(desired_by_asset.values())):
            if self._stop_event.is_set():
                return
            if not isinstance(message, KalshiWsPayloadIssue):
                self._source_ok("kalshi_ws")
            message_connection = getattr(message, "connection_id", None)
            if isinstance(message_connection, str) and message_connection != connection_id:
                if connection_id is not None:
                    self._mark_kalshi_ws_unsynchronized(GapReason.RECONNECT)
                connection_id = message_connection
                current_at_connect = {
                    asset: market.ticker for asset, market in self._health.current.items()
                }
                if current_at_connect:
                    desired_by_asset = current_at_connect
                ticker_assets = {ticker: asset for asset, ticker in desired_by_asset.items()}
                source.set_reconnect_tickers(tuple(desired_by_asset.values()))
                coordinator = KalshiAtomicOrderBookCoordinator(
                    connection_id, tuple(desired_by_asset.values())
                )
                self._kalshi_ws_coordinator = coordinator
                processor = KalshiAtomicSessionProcessor(
                    coordinator,
                    self._send_kalshi_ws_payload,
                    first_request_id=request_id,
                    monotonic=self._monotonic,
                )
                request_id += 1_000
                pending_removals.clear()
                pending_delete_requests.clear()
                self._health.kalshi_ws_state = KalshiWsRuntimeState.WAITING_SNAPSHOT
                self._kalshi_ws_waiting_since_monotonic = self._monotonic()
            if isinstance(message, KalshiWsErrorMessage):
                raise KalshiReadOnlyWsError(
                    f"official Kalshi WebSocket command failed with code {message.code}"
                )
            if isinstance(message, KalshiSubscribed):
                self._health.kalshi_ws_state = KalshiWsRuntimeState.WAITING_SNAPSHOT
                self._worker_advanced("kalshi_ws")
                await asyncio.sleep(0)
                continue
            if isinstance(message, KalshiWsProtocolNotice):
                self._store.append_recorder_event(
                    observed_timestamp=message.socket_received_timestamp,
                    severity=RecorderEventSeverity.INFO,
                    event_type=RecorderEventType.WS_PROTOCOL_NOTICE,
                    source="kalshi-ws",
                    error_type="KalshiWsProtocolNotice",
                    message=(
                        "Ignored non-data Kalshi WS message "
                        f"type={message.message_type} channel={message.channel or 'unknown'} "
                        f"shape={message.payload_shape_hash}"
                    ),
                    dedup_key=(
                        f"kalshi-ws-notice:{message.message_type}:{message.payload_shape_hash}"
                    ),
                )
                self._worker_advanced("kalshi_ws", message.socket_received_timestamp)
                await asyncio.sleep(0)
                continue
            if isinstance(message, KalshiTickerUpdate) and (
                coordinator is None or processor is None
            ):
                self._worker_advanced("kalshi_ws", message.socket_received_timestamp)
                await asyncio.sleep(0)
                continue
            if coordinator is None or processor is None:
                raise KalshiReadOnlyWsError("Kalshi WebSocket data preceded connection identity")

            if isinstance(message, KalshiWsPayloadIssue):
                self._source_failed("kalshi_ws", KalshiWsPayloadError(message.reason))
                key_summary = ",".join(message.schema_keys[:6]) or "none"
                self._store.append_recorder_event(
                    observed_timestamp=message.socket_received_timestamp,
                    severity=RecorderEventSeverity.WARNING,
                    event_type=RecorderEventType.WS_PAYLOAD_RECOVERY,
                    source="kalshi-ws",
                    error_type="KalshiWsPayloadIssue",
                    message=(
                        f"WS payload isolated type={message.message_type} "
                        f"ticker={message.ticker or 'unknown'} sid={message.subscription_id} "
                        f"seq={message.sequence or 'unknown'} stage={message.parser_stage} "
                        f"reason={message.reason} keys={key_summary} "
                        f"shape={message.payload_shape_hash}"
                    )[:240],
                    dedup_key=(
                        f"kalshi-ws-payload:{message.message_type}:{message.payload_shape_hash}"
                    ),
                )
                if message.affects_orderbook:
                    await processor.recover_payload_issue(message)
                    self._mark_kalshi_ws_unsynchronized(GapReason.SOURCE_OUTAGE)
                    self._health.kalshi_ws_state = KalshiWsRuntimeState.UNSYNCHRONIZED
                self._worker_advanced("kalshi_ws", message.socket_received_timestamp)
                await asyncio.sleep(0)
                continue

            latest = {asset: market.ticker for asset, market in self._health.current.items()}
            for asset in tuple(desired_by_asset.keys() - latest.keys()):
                predecessor_by_asset[asset] = desired_by_asset.pop(asset)
                self._health.kalshi_ws_state = KalshiWsRuntimeState.WAITING_SNAPSHOT
                self._health.kalshi_ws_synchronized.pop(asset, None)
                self._kalshi_ws_books.pop(asset, None)
                gap_key = (GapSource.KALSHI_WS, asset)
                if not self._active_gaps.get(gap_key):
                    observed = self._utc_now()
                    self._open_gap(
                        self._gap_streams[gap_key],
                        observed,
                        source_health_key=f"kalshi_ws:{asset.value}",
                        detected_at=observed,
                        reason_override=GapReason.SOURCE_OUTAGE,
                    )
                if desired_by_asset:
                    source.set_reconnect_tickers(tuple(desired_by_asset.values()))
            subscription_id = coordinator.subscription_id
            if subscription_id is not None:
                for asset, successor in latest.items():
                    predecessor = desired_by_asset.get(asset) or predecessor_by_asset.get(asset)
                    if successor == predecessor or successor in coordinator.subscribed_tickers:
                        continue
                    coordinator.add_expected_ticker(successor)
                    ticker_assets[successor] = asset
                    pending_removals[successor] = (
                        predecessor if predecessor in coordinator.subscribed_tickers else ""
                    )
                    self._health.kalshi_ws_state = KalshiWsRuntimeState.WAITING_SNAPSHOT
                    if self._kalshi_ws_waiting_since_monotonic is None:
                        self._kalshi_ws_waiting_since_monotonic = self._monotonic()
                    self._health.kalshi_ws_synchronized.pop(asset, None)
                    self._kalshi_ws_books.pop(asset, None)
                    gap_key = (GapSource.KALSHI_WS, asset)
                    if not self._active_gaps.get(gap_key):
                        observed = self._utc_now()
                        self._open_gap(
                            self._gap_streams[gap_key],
                            observed,
                            source_health_key=f"kalshi_ws:{asset.value}",
                            detected_at=observed,
                            reason_override=GapReason.SOURCE_OUTAGE,
                        )
                    await source.send_command(
                        update_subscription_command(
                            request_id, subscription_id, "add_markets", (successor,)
                        )
                    )
                    request_id += 1
                    desired_by_asset[asset] = successor
                    predecessor_by_asset.pop(asset, None)
                    source.set_reconnect_tickers(tuple(desired_by_asset.values()))

            if isinstance(message, KalshiTickerUpdate):
                self._worker_advanced("kalshi_ws", message.socket_received_timestamp)
                await asyncio.sleep(0)
                continue

            requests_before = processor.diagnostics.requests
            book = await processor.process(message)
            if isinstance(message, KalshiCommandAcknowledged):
                predecessor = pending_delete_requests.pop(message.request_id, None)
                if predecessor is not None:
                    coordinator.remove_expected_ticker(predecessor)
                    ticker_assets.pop(predecessor, None)
            if processor.diagnostics.requests > requests_before:
                self._health.kalshi_ws_seq_gaps += 1
                self._mark_kalshi_ws_unsynchronized(GapReason.SOURCE_OUTAGE)
            synchronized = set(coordinator.synchronized_tickers)
            sync_status = (
                KalshiBookSyncStatus.SYNCHRONIZED
                if getattr(message, "ticker", None) in synchronized
                else KalshiBookSyncStatus.UNSYNCHRONIZED
            )
            if isinstance(message, (KalshiOrderBookSnapshot, KalshiOrderBookDelta)) or (
                isinstance(message, KalshiCommandAcknowledged)
                and message.subscription_id is not None
            ):
                self._kalshi_ws_pending.append((message, sync_status))
                if len(self._kalshi_ws_pending) >= 128:
                    self._flush_kalshi_ws_pending()

            if book is not None:
                asset = ticker_assets.get(book.ticker)
                if asset is None:
                    raise KalshiReadOnlyWsError(
                        "Kalshi WebSocket ticker has no exact asset mapping"
                    )
                if desired_by_asset.get(asset) == book.ticker:
                    self._kalshi_ws_books[asset] = book
                    self._capture_forward_shadow_checkpoint(asset, book)
                    self._health.kalshi_ws_synchronized[asset] = book.ticker
                    self._health.kalshi_ws_last_books[asset] = book.received_timestamp
                    self._observe_gap(
                        GapSource.KALSHI_WS,
                        asset,
                        book.received_timestamp,
                        source_health_key=f"kalshi_ws:{asset.value}",
                    )
                    self._source_ok(f"kalshi_ws:{asset.value}")
                    if isinstance(message, KalshiOrderBookSnapshot):
                        if self._store.append_kalshi_ws_checkpoint(book):
                            self._wrote("kalshi_ws_book_checkpoints")
                predecessor = pending_removals.pop(book.ticker, None)
                if predecessor:
                    delete_request_id = request_id
                    await source.send_command(
                        update_subscription_command(
                            delete_request_id,
                            book.subscription_id,
                            "delete_markets",
                            (predecessor,),
                        )
                    )
                    request_id += 1
                    pending_delete_requests[delete_request_id] = predecessor

            # A delta already returns the one reconstructed book it changed. Rebuilding and
            # sorting every other market on every hot-stream message is both redundant and
            # capable of starving the receive pump. The full refresh is needed only once
            # after a multi-market resync, because intermediate recovery snapshots are
            # intentionally withheld until the complete subscription is synchronized.
            if len(synchronized) == len(desired_by_asset) and len(
                self._health.kalshi_ws_synchronized
            ) < len(desired_by_asset):
                for ticker in synchronized:
                    synchronized_book = coordinator.book(ticker)
                    asset = ticker_assets.get(ticker)
                    if asset is None or desired_by_asset.get(asset) != ticker:
                        continue
                    self._kalshi_ws_books[asset] = synchronized_book
                    self._capture_forward_shadow_checkpoint(asset, synchronized_book)
                    self._health.kalshi_ws_synchronized[asset] = ticker
                    self._health.kalshi_ws_last_books[asset] = synchronized_book.received_timestamp
                    self._observe_gap(
                        GapSource.KALSHI_WS,
                        asset,
                        synchronized_book.received_timestamp,
                        source_health_key=f"kalshi_ws:{asset.value}",
                    )
                    if isinstance(message, KalshiOrderBookSnapshot):
                        if self._store.append_kalshi_ws_checkpoint(synchronized_book):
                            self._wrote("kalshi_ws_book_checkpoints")

            if latest and len(self._health.kalshi_ws_synchronized) == len(latest):
                self._health.kalshi_ws_state = KalshiWsRuntimeState.SYNCHRONIZED
                self._kalshi_ws_waiting_since_monotonic = None
                if not self._startup_ws_synchronized_reported:
                    self._startup_ws_synchronized_reported = True
                    self._mark_startup_phase(
                        "kalshi_ws_synchronized", self._startup_phase_started()
                    )
            elif processor.diagnostics.requests:
                self._health.kalshi_ws_state = KalshiWsRuntimeState.UNSYNCHRONIZED
            else:
                self._health.kalshi_ws_state = KalshiWsRuntimeState.WAITING_SNAPSHOT
            self._health.kalshi_ws_resync_count = processor.diagnostics.completed
            self._worker_advanced("kalshi_ws")
            await asyncio.sleep(0)

    async def _record_robinhood_reference(self) -> None:
        assert self._robinhood is not None
        while not self._stop_event.is_set():
            try:
                contracts = await asyncio.to_thread(self._robinhood.discover)
                for contract in contracts:
                    if contract.fetched_at < contract.end_time:
                        self._store.append_robinhood(contract)
                self._health.robinhood_reference_healthy = True
                self._worker_advanced("robinhood_reference")
            except asyncio.CancelledError:
                raise
            except (RecorderStorageError, ValueError):
                raise
            except Exception as error:
                self._health.robinhood_reference_healthy = False
                self._source_failed("robinhood_reference", error)
                self._worker_advanced("robinhood_reference")
            if await self._wait(self._settings.robinhood_poll_interval_seconds):
                return

    async def _checkpoint(self) -> None:
        while not self._stop_event.is_set():
            self._store.checkpoint()
            self._worker_advanced("checkpoint")
            # PRAGMA quick_check(1) limits error rows, not pages inspected: it is
            # still a full-database scan. Running it synchronously on the recorder
            # event loop can starve every source once the raw database is large.
            # Full integrity is checked only on an offline/read-only snapshot;
            # startup reuses that verified result when available. The live loop
            # performs only the bounded passive WAL checkpoint.
            if await self._wait(self._settings.recorder_checkpoint_interval_seconds):
                return

    async def _archive_ws_retention(self) -> None:
        assert self._archive_service is not None
        assert self._purge_service is not None
        key = "ws_archive"
        quota = DiskQuota()
        while not self._stop_event.is_set():
            observed = self._utc_now()
            try:
                if self._ws_archive_backpressure_active():
                    poll_mode, poll_seconds = self._archive_poll_schedule(
                        backlog_events=0, backpressure=True
                    )
                    self._health.ws_archive_metrics = {
                        **self._health.ws_archive_metrics,
                        "enabled": True,
                        "deferred_for_ws_backpressure": True,
                        "archive_poll_mode": poll_mode,
                        "archive_next_poll_seconds": poll_seconds,
                    }
                    self._source_ok(key)
                    self._worker_advanced(key, observed)
                    if await self._wait(poll_seconds):
                        return
                    continue
                result = await asyncio.to_thread(self._archive_service.run_once, now=observed)
                eligibility = None
                if result.backlog_events <= 0:
                    eligibility = await asyncio.to_thread(
                        self._archive_service.eligibility, observed
                    )
                manifest_metrics = self._archive_service.manifest.metrics()
                if self._ws_archive_backpressure_active():
                    poll_mode, poll_seconds = self._archive_poll_schedule(
                        backlog_events=result.backlog_events, backpressure=True
                    )
                    self._health.ws_archive_metrics = {
                        **self._health.ws_archive_metrics,
                        **manifest_metrics,
                        "enabled": True,
                        "hot_retention_seconds": self._settings.ws_archive_hot_retention_seconds,
                        "archive_backlog_events": result.backlog_events,
                        "archive_backlog_capped": result.backlog_events > 0,
                        "archive_throughput_events_per_second": result.events_per_second,
                        "archive_elapsed_seconds": result.elapsed_seconds,
                        "deferred_for_ws_backpressure": True,
                        "archive_poll_mode": poll_mode,
                        "archive_next_poll_seconds": poll_seconds,
                    }
                    self._source_ok(key)
                    self._worker_advanced(key, self._utc_now())
                    if await self._wait(poll_seconds):
                        return
                    continue
                hot_metrics = await asyncio.to_thread(self._archive_service.hot_metrics, observed)
                disk = shutil.disk_usage(self._store.path.parent)
                disk_state = quota.classify(
                    total_bytes=disk.total,
                    free_bytes=disk.free,
                )
                verified = int(manifest_metrics.get("verified") or 0)
                purge_result = None
                shadow_passed = verified >= self._settings.ws_archive_shadow_chunks
                recorder_core_healthy = self._retention_core_healthy()
                if shadow_passed and recorder_core_healthy:
                    purge_result = await asyncio.to_thread(
                        self._purge_service.run_once, now=observed
                    )
                    manifest_metrics = self._archive_service.manifest.metrics()
                storage_metrics = await asyncio.to_thread(
                    self._archive_service.manifest.storage_metrics, self._store.path
                )
                storage_growth = await asyncio.to_thread(
                    self._archive_service.manifest.record_storage_sample,
                    storage_metrics,
                    observed_at=observed,
                )
                adaptive_status = None
                if self._adaptive_retention is not None:
                    diagnostics = (
                        self._kalshi_ws.diagnostics if self._kalshi_ws is not None else None
                    )
                    adaptive_observation = AdaptiveRetentionObservation(
                        observed_at=observed,
                        verified_chunks=int(manifest_metrics.get("retention_verified") or 0),
                        failed_chunks=int(manifest_metrics.get("failed") or 0),
                        physical_database_bytes=storage_metrics.physical_database_bytes,
                        hot_used_bytes=storage_metrics.hot_sqlite_used_bytes,
                        freelist_reusable_bytes=storage_metrics.freelist_reusable_bytes,
                        cold_archive_bytes=storage_metrics.cold_archive_bytes,
                        cold_growth_bytes_per_day=storage_metrics.cold_archive_growth_bytes_per_day,
                        raw_ws_growth_bytes_per_day=storage_metrics.raw_ws_growth_bytes_per_day,
                        raw_ws_observation_window_seconds=(
                            storage_metrics.raw_ws_observation_window_seconds
                        ),
                        disk_free_bytes=disk.free,
                        disk_total_bytes=disk.total,
                        event_loop_lag_seconds=self._health.event_loop_lag_seconds,
                        ws_queue_depth=int(getattr(diagnostics, "receive_queue_depth", 0)),
                        ws_queue_capacity=int(getattr(diagnostics, "receive_queue_capacity", 0)),
                        ws_sequence_gaps=int(getattr(diagnostics, "sequence_gaps", 0)),
                        ws_resyncs=int(getattr(diagnostics, "resyncs", 0)),
                        ws_reconnects=int(getattr(diagnostics, "reconnects", 0)),
                        data_gap_incidents=int(self._health.row_counts.get("data_gaps", 0)),
                        unresolved_data_gaps=sum(len(gaps) for gaps in self._active_gaps.values()),
                        archive_or_replay_failure=int(manifest_metrics.get("failed") or 0) > 0,
                        serious_runtime_incident=(
                            self._health.fatal_task is not None
                            or self._health.fatal_error_type is not None
                        ),
                        disk_threshold_state=disk_state.value,
                        # These remain UNKNOWN until explicit recovery/access
                        # instrumentation reports them. Missing evidence must never be
                        # converted to zero and used to shorten retention.
                        recovery_lookback_seconds=(self._startup_ws_recovery_lookback_seconds),
                        hot_access_age_seconds=(
                            self._startup_ws_recovery_lookback_seconds
                            if self._hot_ws_access_evidence_complete
                            else None
                        ),
                        recovery_session_id=(
                            self._health.started_at.isoformat()
                            if self._startup_ws_recovery_lookback_seconds is not None
                            else None
                        ),
                        hot_access_evidence_complete=(self._hot_ws_access_evidence_complete),
                    )
                    try:
                        adaptive_status = await asyncio.to_thread(
                            self._adaptive_retention.evaluate_once,
                            adaptive_observation,
                            actual_retention_seconds=int(
                                self._archive_service.hot_retention.total_seconds()
                            ),
                            allow_adjustment=True,
                        )
                    except sqlite3.DatabaseError as error:
                        raise AdaptiveRetentionStateError(
                            "adaptive retention state database is unavailable or corrupt"
                        ) from error
                    self._archive_service.hot_retention = timedelta(
                        seconds=adaptive_status.current_retention_seconds
                    )
                    adaptive_status = replace(
                        adaptive_status,
                        actual_applied_retention_seconds=(
                            adaptive_status.current_retention_seconds
                        ),
                    )
                    await asyncio.to_thread(
                        write_adaptive_retention_status,
                        self._settings.adaptive_retention_status_path
                        or (self._store.path.parent / "adaptive-retention.json"),
                        adaptive_status,
                    )
                latest = self._archive_service.manifest.latest()
                uncompressed = int(manifest_metrics.get("uncompressed") or 0)
                compressed = int(manifest_metrics.get("compressed") or 0)
                self._health.ws_archive_metrics = {
                    **manifest_metrics,
                    **hot_metrics,
                    "enabled": True,
                    "hot_retention_seconds": int(
                        self._archive_service.hot_retention.total_seconds()
                    ),
                    "adaptive_retention": (
                        None if adaptive_status is None else adaptive_status.as_dict()
                    ),
                    "archive_backlog_events": result.backlog_events,
                    "archive_backlog_capped": result.backlog_events > 0,
                    "archive_throughput_events_per_second": result.events_per_second,
                    "archive_elapsed_seconds": result.elapsed_seconds,
                    "archive_lag_seconds": (
                        None
                        if latest is None
                        else max(
                            0.0,
                            (observed - latest.last_received_timestamp).total_seconds(),
                        )
                    ),
                    "compression_ratio": (None if compressed == 0 else uncompressed / compressed),
                    "last_purge_deleted_events": (
                        0 if purge_result is None else purge_result.deleted_events
                    ),
                    "last_purge_transaction_seconds": (
                        0.0 if purge_result is None else purge_result.transaction_seconds
                    ),
                    "last_purge_reusable_bytes": (
                        0 if purge_result is None else purge_result.reusable_bytes_increase
                    ),
                    "hot_sqlite_used_bytes": storage_metrics.hot_sqlite_used_bytes,
                    "freelist_reusable_bytes": storage_metrics.freelist_reusable_bytes,
                    "physical_database_bytes": storage_metrics.physical_database_bytes,
                    "wal_bytes": storage_metrics.wal_bytes,
                    "cold_archive_bytes": storage_metrics.cold_archive_bytes,
                    "cold_archive_growth_bytes_per_hour": (
                        storage_metrics.cold_archive_growth_bytes_per_hour
                    ),
                    "cold_archive_growth_bytes_per_day": (
                        storage_metrics.cold_archive_growth_bytes_per_day
                    ),
                    "raw_ws_growth_bytes_per_hour": storage_metrics.raw_ws_growth_bytes_per_hour,
                    "raw_ws_growth_bytes_per_day": storage_metrics.raw_ws_growth_bytes_per_day,
                    "raw_ws_observation_window_seconds": (
                        storage_metrics.raw_ws_observation_window_seconds
                    ),
                    "net_disk_growth_sample_seconds": (storage_growth.sample_interval_seconds),
                    "net_disk_growth_bytes_per_hour": (
                        storage_growth.net_disk_growth_bytes_per_hour
                    ),
                    "net_disk_growth_bytes_per_day": (storage_growth.net_disk_growth_bytes_per_day),
                    "disk_total_bytes": disk.total,
                    "disk_free_bytes": disk.free,
                    "disk_threshold_state": disk_state.value,
                    "shadow_acceptance_passed": shadow_passed,
                    "deferred_for_ws_backpressure": False,
                }
                poll_mode, poll_seconds = self._archive_poll_schedule(
                    backlog_events=result.backlog_events,
                    next_eligible_at=(
                        None if eligibility is None else eligibility.next_eligible_at
                    ),
                    eligibility_status=(None if eligibility is None else eligibility.status),
                )
                self._health.ws_archive_metrics.update(
                    {
                        "archive_poll_mode": poll_mode,
                        "archive_next_poll_seconds": poll_seconds,
                    }
                )
                if (
                    adaptive_status is not None
                    and adaptive_status.controller_mode is AdaptiveRetentionMode.FAIL_SAFE
                    and self._controlled_pause is not None
                ):
                    self._controlled_pause(adaptive_status.reason)
                    self.request_stop()
                    self._source_failed(key, RecorderStorageError(adaptive_status.reason))
                    self._worker_advanced(key, observed)
                    return
                if disk_state.value == "fail_safe":
                    raise RecorderStorageError(
                        "disk fail-safe threshold reached; unverified raw data was preserved"
                    )
                self._source_ok(key)
                self._worker_advanced(key, observed)
            except (OSError, sqlite3.OperationalError, WsRetentionError) as error:
                self._source_failed(key, error)
                self._worker_advanced(key, observed)
                if await self._wait(self._retry_delay(key, 1.0)):
                    return
                continue
            except AdaptiveRetentionError as error:
                self._adaptive_retention = None
                self._archive_service.hot_retention = timedelta(
                    seconds=self._settings.adaptive_retention_max_seconds
                )
                self._source_failed("adaptive_retention", error)
                self._worker_advanced(key, observed)
                if await self._wait(self._settings.ws_archive_poll_interval_seconds):
                    return
                continue
            if await self._wait(poll_seconds):
                return

    def _archive_poll_schedule(
        self,
        *,
        backlog_events: int,
        next_eligible_at: datetime | None = None,
        eligibility_status: str | None = None,
        backpressure: bool = False,
    ) -> tuple[str, float]:
        """Return a bounded, deterministic archive polling mode and delay."""

        if backpressure:
            return "BACKPRESSURE", 2.0
        if backlog_events >= self._archive_service.chunk_records:
            return "CATCH_UP", 2.0
        if backlog_events >= max(1, self._archive_service.chunk_records // 4):
            return "ACTIVE", 5.0
        if backlog_events > 0:
            return "NEAR_CAUGHT_UP", 10.0
        if (
            eligibility_status == "WAITING_FOR_RETENTION_ELIGIBILITY"
            and next_eligible_at is not None
        ):
            seconds = (next_eligible_at - self._utc_now()).total_seconds()
            return "IDLE", max(2.0, min(60.0, seconds))
        return "IDLE", 60.0

    def _ws_archive_backpressure_active(self) -> bool:
        if self._kalshi_ws is None:
            return False
        current_assets = set(self._health.current)
        if current_assets and not current_assets.issubset(self._health.kalshi_ws_synchronized):
            return True
        diagnostics = self._kalshi_ws.diagnostics
        capacity = int(getattr(diagnostics, "receive_queue_capacity", 0))
        depth = int(getattr(diagnostics, "receive_queue_depth", 0))
        return capacity > 0 and depth * 4 >= capacity

    def _retention_core_healthy(self) -> bool:
        if self._health.fatal_task is not None or self._health.fatal_error_type is not None:
            return False
        if not self._settings.enable_kalshi_production_websocket:
            return True
        current_assets = set(self._health.current)
        return bool(current_assets) and current_assets.issubset(self._health.kalshi_ws_synchronized)

    async def _report_health(self) -> None:
        self._write_health_file(self.health())
        self._mark_startup_phase("first_heartbeat", self._startup_phase_started())
        interval = self._settings.recorder_health_interval_seconds
        deadline = self._monotonic() + interval
        while not await self._wait(interval):
            observed_monotonic = self._monotonic()
            self._health.event_loop_lag_seconds = max(0.0, observed_monotonic - deadline)
            deadline = observed_monotonic + interval
            self._open_due_gaps(self._utc_now())
            health = self.health()
            stale = set(health.stale_sources)
            for source in sorted(stale - self._reported_stale_sources):
                asset = next((item for item in Asset if source.endswith(f":{item.value}")), None)
                self._store.append_recorder_event(
                    observed_timestamp=health.observed_at,
                    severity=RecorderEventSeverity.WARNING,
                    event_type=RecorderEventType.SOURCE_STALE,
                    asset=asset,
                    source=source,
                    error_type="StaleSource",
                    message="Source freshness threshold exceeded",
                    dedup_key=f"source-stale:{source}:{health.started_at.isoformat()}",
                )
            self._reported_stale_sources = stale
            self._write_health_file(health)
            logger.info(
                "Kalshi-native recorder health",
                extra={"event": "kalshi_native_health", **health.as_dict()},
            )

    def _write_health_file(self, health: KalshiNativeHealth) -> None:
        path = self._settings.recorder_health_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(health.as_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _health_fields(self) -> dict[str, object]:
        return self.health().as_dict()
