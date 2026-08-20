"""Kalshi-native recorder; Robinhood is an optional reference task only."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from live15_quant.config import Settings
from live15_quant.kalshi_lifecycle import (
    KalshiDiscovery,
    KalshiLifecycle,
    KalshiLifecycleStateMachine,
    KalshiMarket,
    KalshiNativeMarketProvider,
)
from live15_quant.models import Asset, FifteenMinuteContract, KalshiNativeQuote, MarketTick
from live15_quant.providers.coinbase import CoinbaseWebSocketClient
from live15_quant.providers.kalshi import KalshiOfficialQuoteProvider
from live15_quant.providers.robinhood_15min import Robinhood15MinuteProvider
from live15_quant.storage import RecorderStore

logger = logging.getLogger(__name__)


class NativeDiscovery(Protocol):
    def discover_all(self, now: datetime | None = None) -> Sequence[KalshiDiscovery]: ...


class NativeQuoteSource(Protocol):
    def quotes_native(self, markets: Sequence[KalshiMarket]) -> Sequence[KalshiNativeQuote]: ...


class TickStream(Protocol):
    def ticks(self) -> AsyncIterator[MarketTick]: ...


class RobinhoodReference(Protocol):
    def discover(self) -> Sequence[FifteenMinuteContract]: ...


@dataclass(frozen=True, slots=True)
class KalshiNativeHealth:
    current_markets: dict[Asset, str]
    last_discovery: datetime | None
    last_quotes: dict[Asset, datetime]
    last_coinbase: dict[str, datetime]
    settlement_count: int
    written_records: int
    robinhood_reference_healthy: bool | None


@dataclass(slots=True)
class _MutableHealth:
    current: dict[Asset, KalshiMarket] = field(default_factory=dict)
    states: dict[str, KalshiLifecycle] = field(default_factory=dict)
    last_discovery: datetime | None = None
    last_quotes: dict[Asset, datetime] = field(default_factory=dict)
    last_coinbase: dict[str, datetime] = field(default_factory=dict)
    written_records: int = 0
    robinhood_reference_healthy: bool | None = None


class KalshiNativeRecorder:
    """Persist official Kalshi lifecycle/quotes and Coinbase predictive input."""

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
        self._quote_client = KalshiOfficialQuoteProvider(settings) if quotes is None else None
        self._discovery_client = (
            KalshiOfficialQuoteProvider(settings) if discovery is None else None
        )
        self._quotes = quotes or self._quote_client
        assert self._quotes is not None
        self._discovery = discovery or KalshiNativeMarketProvider(self._discovery_client)
        self._coinbase_factory = coinbase_factory or (
            lambda: CoinbaseWebSocketClient(settings, products=settings.products)
        )
        self._robinhood = robinhood_reference
        if settings.enable_robinhood_reference and self._robinhood is None:
            self._robinhood = Robinhood15MinuteProvider(settings)
        self._health = _MutableHealth(
            states={
                record.ticker: record.lifecycle
                for record in store.latest_kalshi_states(
                    window_end_at_or_after=self._now().astimezone(UTC) - timedelta(hours=2)
                )
            }
        )
        self._stop_event = asyncio.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    def health(self) -> KalshiNativeHealth:
        return KalshiNativeHealth(
            current_markets={
                asset: market.ticker for asset, market in self._health.current.items()
            },
            last_discovery=self._health.last_discovery,
            last_quotes=dict(self._health.last_quotes),
            last_coinbase=dict(self._health.last_coinbase),
            settlement_count=self._store.count("kalshi_settlements"),
            written_records=self._health.written_records,
            robinhood_reference_healthy=self._health.robinhood_reference_healthy,
        )

    async def run(self) -> None:
        self._stop_event.clear()
        tasks = [
            asyncio.create_task(self._record_lifecycle(), name="kalshi-native-lifecycle"),
            asyncio.create_task(self._record_quotes(), name="kalshi-native-quotes"),
            asyncio.create_task(self._record_coinbase(), name="coinbase-predictive"),
            asyncio.create_task(self._report_health(), name="kalshi-native-health"),
        ]
        if self._settings.enable_robinhood_reference and self._robinhood is not None:
            tasks.append(
                asyncio.create_task(self._record_robinhood_reference(), name="robinhood-reference")
            )
        logger.info(
            "Kalshi-native recorder started",
            extra={
                "event": "kalshi_native_recorder_started",
                "database": str(self._store.path),
                "robinhood_reference_enabled": self._settings.enable_robinhood_reference,
            },
        )
        try:
            await self._stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._quote_client is not None:
                self._quote_client.close()
            if self._discovery_client is not None:
                self._discovery_client.close()
            if isinstance(self._robinhood, Robinhood15MinuteProvider):
                self._robinhood.close()
            logger.info(
                "Kalshi-native recorder stopped",
                extra={"event": "kalshi_native_recorder_stopped", **self._health_fields()},
            )

    def _accept_discoveries(self, discoveries: Sequence[KalshiDiscovery]) -> None:
        now = self._now().astimezone(UTC)
        current: dict[Asset, KalshiMarket] = {}
        relevant_tickers: set[str] = set()
        for discovery in discoveries:
            self._health.last_discovery = discovery.fetched_timestamp
            for market in discovery.valid_markets:
                relevant_tickers.add(market.ticker)
                prior = self._health.states.get(market.ticker)
                if prior is None:
                    persisted = self._store.latest_kalshi_state(market.ticker)
                    prior = persisted.lifecycle if persisted is not None else None
                states = (
                    (market.lifecycle,)
                    if prior is None
                    else KalshiLifecycleStateMachine.transition(prior, market.lifecycle)
                )
                for state in states:
                    observation = replace(market, lifecycle=state)
                    if self._store.append_kalshi_market(observation):
                        self._health.written_records += 1
                    self._health.states[market.ticker] = state
            market = discovery.current
            if (
                market is not None
                and market.lifecycle is KalshiLifecycle.OPEN
                and now < market.window_end
            ):
                previous = self._health.current.get(discovery.asset)
                current[discovery.asset] = market
                if previous is None or previous.ticker != market.ticker:
                    logger.info(
                        "Kalshi-native market rollover",
                        extra={
                            "event": "kalshi_native_rollover",
                            "asset": discovery.asset,
                            "previous_ticker": previous.ticker if previous else None,
                            "current_ticker": market.ticker,
                            "window_start": market.window_start,
                            "rollover_latency_seconds": max(
                                0.0,
                                (discovery.fetched_timestamp - market.window_start).total_seconds(),
                            ),
                        },
                    )
        self._health.current = current
        self._health.states = {
            ticker: state
            for ticker, state in self._health.states.items()
            if ticker in relevant_tickers
        }

    async def _record_lifecycle(self) -> None:
        while True:
            try:
                discoveries = await asyncio.to_thread(self._discovery.discover_all)
                self._accept_discoveries(discoveries)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception(
                    "Kalshi-native discovery failed",
                    extra={"event": "kalshi_native_discovery_error", "error": str(error)},
                )
            await asyncio.sleep(self._settings.robinhood_poll_interval_seconds)

    async def _record_quotes(self) -> None:
        while True:
            try:
                markets = tuple(self._health.current.values())
                if markets:
                    quotes = await asyncio.to_thread(self._quotes.quotes_native, markets)
                    for quote in quotes:
                        market = next(
                            (item for item in markets if item.ticker == quote.ticker), None
                        )
                        if market is None or quote.received_timestamp >= market.window_end:
                            logger.warning(
                                "Post-end or unmapped native quote suppressed",
                                extra={
                                    "event": "kalshi_native_quote_suppressed",
                                    "ticker": quote.ticker,
                                },
                            )
                            continue
                        if self._store.append_kalshi_quote(quote):
                            self._health.written_records += 1
                        self._health.last_quotes[quote.asset] = quote.received_timestamp
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception(
                    "Kalshi-native quote recorder failed",
                    extra={"event": "kalshi_native_quote_error", "error": str(error)},
                )
            await asyncio.sleep(self._settings.official_quote_poll_interval_seconds)

    async def _record_coinbase(self) -> None:
        while True:
            try:
                async for tick in self._coinbase_factory().ticks():
                    if self._store.append_coinbase(tick):
                        self._health.written_records += 1
                    self._health.last_coinbase[tick.symbol] = tick.received_at
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception(
                    "Coinbase predictive stream failed",
                    extra={"event": "kalshi_native_coinbase_error", "error": str(error)},
                )
            await asyncio.sleep(self._settings.reconnect_delay_seconds)

    async def _record_robinhood_reference(self) -> None:
        assert self._robinhood is not None
        while True:
            try:
                contracts = await asyncio.to_thread(self._robinhood.discover)
                for contract in contracts:
                    if contract.fetched_at < contract.end_time:
                        self._store.append_robinhood(contract)
                self._health.robinhood_reference_healthy = True
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._health.robinhood_reference_healthy = False
                logger.warning(
                    "Optional Robinhood reference failed; Kalshi core continues",
                    extra={"event": "robinhood_reference_error", "error": str(error)},
                )
            await asyncio.sleep(self._settings.robinhood_poll_interval_seconds)

    async def _report_health(self) -> None:
        while True:
            await asyncio.sleep(self._settings.recorder_health_interval_seconds)
            logger.info(
                "Kalshi-native recorder health",
                extra={"event": "kalshi_native_health", **self._health_fields()},
            )

    def _health_fields(self) -> dict[str, object]:
        health = self.health()
        return {
            "current_markets": health.current_markets,
            "current_market_count": len(health.current_markets),
            "last_discovery": health.last_discovery,
            "last_quotes": health.last_quotes,
            "last_coinbase": health.last_coinbase,
            "settlement_count": health.settlement_count,
            "written_records": health.written_records,
            "robinhood_reference_healthy": health.robinhood_reference_healthy,
        }
