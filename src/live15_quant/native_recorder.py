"""Continuous Kalshi-native training-data recorder."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import requests

from live15_quant.config import Settings
from live15_quant.kalshi_lifecycle import (
    KalshiDiscovery,
    KalshiLifecycle,
    KalshiLifecycleStateMachine,
    KalshiMarket,
    KalshiNativeMarketProvider,
)
from live15_quant.models import (
    Asset,
    FifteenMinuteContract,
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
from live15_quant.providers.robinhood_15min import Robinhood15MinuteProvider
from live15_quant.records import KalshiMarketRecord
from live15_quant.storage import (
    MarketIdentityConflictError,
    RecorderStorageError,
    RecorderStore,
    SettlementConflictError,
)

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True, slots=True)
class KalshiNativeHealth:
    started_at: datetime
    observed_at: datetime
    current_markets: dict[Asset, str | None]
    last_discovery: dict[Asset, datetime]
    last_quotes: dict[Asset, datetime]
    last_coinbase: dict[str, datetime]
    active_settlement_followups: int
    settlement_count: int
    database_bytes: int
    wal_bytes: int
    row_counts: dict[str, int]
    retry_counts: dict[str, int]
    source_failures: dict[str, str]
    stale_sources: tuple[str, ...]
    last_finalized_settlement: dict[Asset, str]
    written_records: int
    integrity: str
    robinhood_reference_healthy: bool | None
    fatal_task: str | None
    fatal_error_type: str | None

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

        status = (
            "storage_error"
            if self.integrity != "ok"
            else "degraded"
            if self.source_failures or self.stale_sources
            else "healthy"
        )
        return {
            "status": status,
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
            "active_settlement_followups": self.active_settlement_followups,
            "settlement_count": self.settlement_count,
            "database_bytes": self.database_bytes,
            "wal_bytes": self.wal_bytes,
            "row_counts": self.row_counts,
            "retry_counts": self.retry_counts,
            "source_failures": self.source_failures,
            "stale_sources": self.stale_sources,
            "last_finalized_settlement": {
                str(key): value for key, value in self.last_finalized_settlement.items()
            },
            "written_records": self.written_records,
            "integrity": self.integrity,
            "robinhood_reference_healthy": self.robinhood_reference_healthy,
            "fatal_task": self.fatal_task,
            "fatal_error_type": self.fatal_error_type,
        }


@dataclass(slots=True)
class _MutableHealth:
    started_at: datetime
    current: dict[Asset, KalshiMarket] = field(default_factory=dict)
    states: dict[str, KalshiLifecycle] = field(default_factory=dict)
    last_discovery: dict[Asset, datetime] = field(default_factory=dict)
    last_quotes: dict[Asset, datetime] = field(default_factory=dict)
    last_coinbase: dict[str, datetime] = field(default_factory=dict)
    retry_counts: dict[str, int] = field(default_factory=dict)
    consecutive_failures: dict[str, int] = field(default_factory=dict)
    source_failures: dict[str, str] = field(default_factory=dict)
    last_finalized: dict[Asset, str] = field(default_factory=dict)
    written_records: int = 0
    row_counts: dict[str, int] = field(default_factory=dict)
    integrity: str = "not_checked"
    robinhood_reference_healthy: bool | None = None
    fatal_task: str | None = None
    fatal_error_type: str | None = None


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
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._now = now or (lambda: datetime.now(UTC))
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
        self._robinhood = robinhood_reference
        if settings.enable_robinhood_reference and self._robinhood is None:
            self._robinhood = Robinhood15MinuteProvider(settings)
        observed = self._utc_now()
        records = store.latest_kalshi_states(
            window_end_at_or_after=observed - timedelta(minutes=30),
            window_end_before=observed + timedelta(hours=2),
        )
        quote_cursors, tick_cursors = store.latest_native_cursors()
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
            row_counts=store.row_counts(),
            integrity=store.quick_check(),
        )
        self._stop_event = asyncio.Event()
        self._followup_cursors: dict[Asset, str | None] = {asset: None for asset in Asset}
        self._operation_condition = threading.Condition()
        self._active_operations = 0
        self._reported_stale_sources: set[str] = set()

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
                    f"coinbase:{product}"
                    for product in self._settings.products
                    if product not in self._health.last_coinbase
                    or (observed - self._health.last_coinbase[product]).total_seconds()
                    > self._settings.recorder_coinbase_stale_seconds
                ]
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
            active_settlement_followups=self._store.unsettled_kalshi_count(now=observed),
            settlement_count=self._health.row_counts["kalshi_settlements"],
            database_bytes=database_bytes,
            wal_bytes=wal_bytes,
            row_counts=dict(self._health.row_counts),
            retry_counts=dict(self._health.retry_counts),
            source_failures=dict(self._health.source_failures),
            stale_sources=stale_sources,
            last_finalized_settlement=dict(self._health.last_finalized),
            written_records=self._health.written_records,
            integrity=self._health.integrity,
            robinhood_reference_healthy=self._health.robinhood_reference_healthy,
            fatal_task=self._health.fatal_task,
            fatal_error_type=self._health.fatal_error_type,
        )

    async def run(self) -> None:
        self._stop_event.clear()
        tasks = [
            asyncio.create_task(self._record_coinbase(), name="coinbase-predictive"),
            asyncio.create_task(self._report_health(), name="kalshi-native-health"),
            asyncio.create_task(self._checkpoint(), name="sqlite-checkpoint"),
        ]
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
        logger.info(
            "Kalshi-native recorder started",
            extra={
                "event": "kalshi_native_recorder_started",
                "database": str(self._store.path),
                "recovered_current": len(self._health.current),
                "recovered_followups": self._store.unsettled_kalshi_count(now=self._utc_now()),
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
                message="Recorder worker stopped on a correctness failure",
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
        for observation in KalshiLifecycleStateMachine.observations(prior, market):
            if self._store.append_kalshi_market(observation):
                self._wrote("kalshi_market_lifecycle")
            self._health.states[market.ticker] = observation.lifecycle
        if market.settlement is not None:
            persisted_count = self._store.count("kalshi_settlements")
            delta = persisted_count - self._health.row_counts["kalshi_settlements"]
            if delta < 0:
                raise RecorderStorageError("settlement row count moved backwards")
            self._health.row_counts["kalshi_settlements"] = persisted_count
            self._health.written_records += delta
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
                    self._health.last_coinbase[tick.symbol] = tick.received_at
                    self._source_ok("coinbase")
                    if self._stop_event.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except (RecorderStorageError, ValueError):
                raise
            except Exception as error:
                self._source_failed("coinbase", error)
            if await self._wait(self._settings.reconnect_delay_seconds):
                return

    async def _record_robinhood_reference(self) -> None:
        assert self._robinhood is not None
        while not self._stop_event.is_set():
            try:
                contracts = await asyncio.to_thread(self._robinhood.discover)
                for contract in contracts:
                    if contract.fetched_at < contract.end_time:
                        self._store.append_robinhood(contract)
                self._health.robinhood_reference_healthy = True
            except asyncio.CancelledError:
                raise
            except (RecorderStorageError, ValueError):
                raise
            except Exception as error:
                self._health.robinhood_reference_healthy = False
                self._source_failed("robinhood_reference", error)
            if await self._wait(self._settings.robinhood_poll_interval_seconds):
                return

    async def _checkpoint(self) -> None:
        while not self._stop_event.is_set():
            self._store.checkpoint()
            self._health.integrity = self._store.quick_check()
            if self._health.integrity != "ok":
                raise RecorderStorageError(
                    f"periodic SQLite quick check failed: {self._health.integrity}"
                )
            if await self._wait(self._settings.recorder_checkpoint_interval_seconds):
                return

    async def _report_health(self) -> None:
        self._write_health_file(self.health())
        while not await self._wait(self._settings.recorder_health_interval_seconds):
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
