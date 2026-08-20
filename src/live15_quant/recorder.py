"""Continuous recorder for public Robinhood events and Coinbase predictive ticks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from live15_quant.config import Settings
from live15_quant.models import (
    Asset,
    FifteenMinuteContract,
    FreshnessState,
    LifecycleState,
    MarketTick,
    PredictionMarketQuote,
    RecorderDiagnosticKind,
)
from live15_quant.providers.coinbase import CoinbaseWebSocketClient
from live15_quant.providers.kalshi import KalshiOfficialQuoteProvider
from live15_quant.providers.robinhood_15min import Robinhood15MinuteProvider
from live15_quant.storage import RecorderStore

logger = logging.getLogger(__name__)


class ContractDiscovery(Protocol):
    def discover(self) -> Sequence[FifteenMinuteContract]: ...


class TickStream(Protocol):
    def ticks(self) -> AsyncIterator[MarketTick]: ...


class OfficialQuoteSource(Protocol):
    def quotes(
        self, contracts: Sequence[FifteenMinuteContract]
    ) -> Sequence[PredictionMarketQuote]: ...


@dataclass(frozen=True, slots=True)
class RecorderHealth:
    """Immutable public health snapshot."""

    tracked_event_count: int
    last_robinhood_snapshot: datetime | None
    coinbase_last_updates: dict[str, datetime]
    official_quote_last_updates: dict[Asset, datetime]
    stale_source_count: int
    written_record_count: int
    rollover_gaps: tuple[RolloverGapStatus, ...]


@dataclass(frozen=True, slots=True)
class RolloverGapStatus:
    """Current durable gap between an ended event and its observed successor."""

    asset: Asset
    previous_event_id: str
    started_at: datetime
    duration_seconds: int


@dataclass(slots=True)
class _MutableHealth:
    tracked_events: dict[str, FifteenMinuteContract] = field(default_factory=dict)
    last_robinhood_snapshot: datetime | None = None
    coinbase_last_updates: dict[str, datetime] = field(default_factory=dict)
    official_quote_last_updates: dict[Asset, datetime] = field(default_factory=dict)
    stale_official_quotes: set[Asset] = field(default_factory=set)
    stale_robinhood_events: set[str] = field(default_factory=set)
    written_record_count: int = 0
    rollover_gaps: dict[Asset, RolloverGapStatus] = field(default_factory=dict)


class HistoricalRecorder:
    """Coordinate independent public sources into one append-only store."""

    def __init__(
        self,
        settings: Settings,
        store: RecorderStore,
        *,
        robinhood: ContractDiscovery | None = None,
        coinbase_factory: Callable[[], TickStream] | None = None,
        official_quotes: OfficialQuoteSource | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._robinhood = robinhood or Robinhood15MinuteProvider(settings)
        self._owns_robinhood = robinhood is None
        self._coinbase_factory = coinbase_factory or (
            lambda: CoinbaseWebSocketClient(settings, products=settings.products)
        )
        self._official_quotes = official_quotes or KalshiOfficialQuoteProvider(settings)
        self._owns_official_quotes = official_quotes is None
        self._now = now or (lambda: datetime.now(UTC))
        self._health = _MutableHealth()
        for asset, diagnostic in self._store.open_rollover_gaps().items():
            self._health.rollover_gaps[asset] = RolloverGapStatus(
                asset=asset,
                previous_event_id=diagnostic.event_id,
                started_at=diagnostic.event_end_time,
                duration_seconds=max(
                    0, int((self._now() - diagnostic.event_end_time).total_seconds())
                ),
            )
            logger.info(
                "Recovered open Robinhood rollover gap",
                extra={
                    "event": "robinhood_rollover_gap_recovered",
                    "asset": asset,
                    "previous_event_id": diagnostic.event_id,
                    "gap_started_at": diagnostic.event_end_time,
                },
            )
        self._stop_event = asyncio.Event()

    def request_stop(self) -> None:
        """Request a cooperative stop; pending tasks are then cancelled and flushed."""

        self._stop_event.set()

    def health(self) -> RecorderHealth:
        now = self._now()
        stale_coinbase = sum(
            (now - updated).total_seconds() > self._settings.recorder_coinbase_stale_seconds
            for updated in self._health.coinbase_last_updates.values()
        )
        missing_coinbase = len(
            set(self._settings.products) - self._health.coinbase_last_updates.keys()
        )
        robinhood_stale = (
            self._health.last_robinhood_snapshot is None
            or (now - self._health.last_robinhood_snapshot).total_seconds()
            > self._settings.robinhood_max_source_age_seconds
            or bool(self._health.stale_robinhood_events)
            or bool(self._health.rollover_gaps)
        )
        tracked_assets = {contract.asset for contract in self._health.tracked_events.values()}
        missing_official_quotes = len(
            tracked_assets - self._health.official_quote_last_updates.keys()
        )
        aged_official_quotes = {
            asset
            for asset, updated in self._health.official_quote_last_updates.items()
            if (now - updated).total_seconds()
            > self._settings.official_quote_max_source_age_seconds
        }
        stale_official_quotes = len(
            (self._health.stale_official_quotes | aged_official_quotes) & tracked_assets
        )
        gaps = tuple(
            RolloverGapStatus(
                asset=gap.asset,
                previous_event_id=gap.previous_event_id,
                started_at=gap.started_at,
                duration_seconds=max(0, int((now - gap.started_at).total_seconds())),
            )
            for gap in sorted(
                self._health.rollover_gaps.values(), key=lambda item: item.asset.value
            )
        )
        return RecorderHealth(
            tracked_event_count=len(self._health.tracked_events),
            last_robinhood_snapshot=self._health.last_robinhood_snapshot,
            coinbase_last_updates=dict(self._health.coinbase_last_updates),
            official_quote_last_updates=dict(self._health.official_quote_last_updates),
            stale_source_count=(
                int(robinhood_stale)
                + stale_coinbase
                + missing_coinbase
                + missing_official_quotes
                + stale_official_quotes
            ),
            written_record_count=self._health.written_record_count,
            rollover_gaps=gaps,
        )

    async def run(self) -> None:
        """Run until request_stop, cancellation, or Ctrl+C cancellation from asyncio.run."""

        self._stop_event.clear()
        tasks = [
            asyncio.create_task(self._record_robinhood(), name="robinhood-recorder"),
            asyncio.create_task(self._record_coinbase(), name="coinbase-recorder"),
            asyncio.create_task(self._record_official_quotes(), name="official-quote-recorder"),
            asyncio.create_task(self._report_health(), name="recorder-health"),
        ]
        logger.info(
            "Historical recorder started",
            extra={
                "event": "recorder_started",
                "database": str(self._store.path),
                "products": self._settings.products,
            },
        )
        try:
            await self._stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._owns_robinhood:
                assert isinstance(self._robinhood, Robinhood15MinuteProvider)
                self._robinhood.close()
            if self._owns_official_quotes:
                assert isinstance(self._official_quotes, KalshiOfficialQuoteProvider)
                self._official_quotes.close()
            logger.info(
                "Historical recorder stopped",
                extra={"event": "recorder_stopped", **self._health_fields()},
            )

    async def _record_robinhood(self) -> None:
        while True:
            try:
                contracts = await asyncio.to_thread(self._robinhood.discover)
                self._accept_contracts(contracts)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception(
                    "Robinhood recorder poll failed",
                    extra={"event": "recorder_robinhood_error", "error": str(error)},
                )
            await asyncio.sleep(self._settings.robinhood_poll_interval_seconds)

    def _accept_contracts(self, contracts: Sequence[FifteenMinuteContract]) -> None:
        observed_ids = {contract.event_id for contract in contracts}
        now = (
            max(contract.fetched_at for contract in contracts)
            if contracts
            else self._now().astimezone(UTC)
        )
        expired = {
            event_id
            for event_id, contract in self._health.tracked_events.items()
            if contract.end_time <= now and event_id not in observed_ids
        }
        for event_id in expired:
            contract = self._health.tracked_events.pop(event_id)
            self._health.stale_robinhood_events.discard(event_id)
            self._start_rollover_gap(contract, now)

        by_asset: dict[Asset, list[FifteenMinuteContract]] = {}
        for contract in contracts:
            by_asset.setdefault(contract.asset, []).append(contract)

        for asset, asset_contracts in by_asset.items():
            post_end = [
                contract for contract in asset_contracts if contract.fetched_at >= contract.end_time
            ]
            eligible = [
                contract for contract in asset_contracts if contract.fetched_at < contract.end_time
            ]
            for contract in post_end:
                self._record_post_end_diagnostic(contract)
                self._health.tracked_events.pop(contract.event_id, None)
                self._health.stale_robinhood_events.discard(contract.event_id)
            if post_end and not eligible:
                latest = max(post_end, key=lambda item: item.end_time)
                newer_event_still_active = any(
                    tracked.asset is asset
                    and tracked.end_time > latest.end_time
                    and tracked.end_time > latest.fetched_at
                    for tracked in self._health.tracked_events.values()
                )
                if not newer_event_still_active:
                    self._start_rollover_gap(latest, latest.fetched_at)

            for contract in eligible:
                if self._store.append_robinhood(contract):
                    self._health.written_record_count += 1
                    logger.debug(
                        "Robinhood training snapshot written",
                        extra={
                            "event": "robinhood_snapshot_written",
                            "asset": contract.asset,
                            "event_id": contract.event_id,
                            "fetched_at": contract.fetched_at,
                        },
                    )
                self._health.last_robinhood_snapshot = contract.fetched_at
                if contract.freshness_state is FreshnessState.STALE:
                    self._health.stale_robinhood_events.add(contract.event_id)
                else:
                    self._health.stale_robinhood_events.discard(contract.event_id)
                if contract.lifecycle_state in {
                    LifecycleState.CLOSED,
                    LifecycleState.SETTLED,
                }:
                    self._health.tracked_events.pop(contract.event_id, None)
                else:
                    self._health.tracked_events[contract.event_id] = contract
                gap = self._health.rollover_gaps.get(asset)
                if (
                    gap is not None
                    and contract.event_id != gap.previous_event_id
                    and contract.start_time >= gap.started_at
                ):
                    self._end_rollover_gap(gap, contract)

    def _record_post_end_diagnostic(self, contract: FifteenMinuteContract) -> None:
        diagnostic_written = self._store.append_robinhood_diagnostic(
            kind=RecorderDiagnosticKind.POST_END_EVENT_RETURNED,
            asset=contract.asset,
            event_id=contract.event_id,
            contract_id=contract.contract_id,
            observed_at=contract.fetched_at,
            event_end_time=contract.end_time,
            source_url=contract.source_url,
        )
        if diagnostic_written:
            self._health.written_record_count += 1
            logger.warning(
                "Upstream public page returned a post-end event; training snapshot suppressed",
                extra={
                    "event": "robinhood_post_end_event_returned",
                    "asset": contract.asset,
                    "event_id": contract.event_id,
                    "event_end_time": contract.end_time,
                    "observed_at": contract.fetched_at,
                    "seconds_post_end": int(
                        (contract.fetched_at - contract.end_time).total_seconds()
                    ),
                },
            )

    def _start_rollover_gap(self, previous: FifteenMinuteContract, observed_at: datetime) -> None:
        if previous.asset in self._health.rollover_gaps:
            return
        gap = RolloverGapStatus(
            asset=previous.asset,
            previous_event_id=previous.event_id,
            started_at=previous.end_time,
            duration_seconds=max(0, int((observed_at - previous.end_time).total_seconds())),
        )
        if self._store.append_robinhood_diagnostic(
            kind=RecorderDiagnosticKind.ROLLOVER_GAP_STARTED,
            asset=previous.asset,
            event_id=previous.event_id,
            contract_id=previous.contract_id,
            observed_at=observed_at,
            event_end_time=previous.end_time,
            source_url=previous.source_url,
        ):
            self._health.written_record_count += 1
        self._health.rollover_gaps[previous.asset] = gap
        logger.warning(
            "Robinhood event rollover gap started",
            extra={
                "event": "robinhood_rollover_gap_started",
                "asset": previous.asset,
                "previous_event_id": previous.event_id,
                "gap_started_at": previous.end_time,
                "gap_duration_seconds": gap.duration_seconds,
            },
        )

    def _end_rollover_gap(self, gap: RolloverGapStatus, successor: FifteenMinuteContract) -> None:
        duration = max(0, int((successor.fetched_at - gap.started_at).total_seconds()))
        if self._store.append_robinhood_diagnostic(
            kind=RecorderDiagnosticKind.ROLLOVER_GAP_ENDED,
            asset=gap.asset,
            event_id=gap.previous_event_id,
            contract_id=successor.contract_id,
            observed_at=successor.fetched_at,
            event_end_time=gap.started_at,
            related_event_id=successor.event_id,
            source_url=successor.source_url,
        ):
            self._health.written_record_count += 1
        self._health.rollover_gaps.pop(gap.asset, None)
        logger.info(
            "Robinhood event rollover gap ended",
            extra={
                "event": "robinhood_rollover_gap_ended",
                "asset": gap.asset,
                "previous_event_id": gap.previous_event_id,
                "next_event_id": successor.event_id,
                "gap_started_at": gap.started_at,
                "gap_duration_seconds": duration,
            },
        )

    async def _record_coinbase(self) -> None:
        while True:
            try:
                async for tick in self._coinbase_factory().ticks():
                    if self._store.append_coinbase(tick):
                        self._health.written_record_count += 1
                    self._health.coinbase_last_updates[tick.symbol] = tick.received_at
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception(
                    "Coinbase recorder stream failed",
                    extra={"event": "recorder_coinbase_error", "error": str(error)},
                )
            await asyncio.sleep(self._settings.reconnect_delay_seconds)

    async def _record_official_quotes(self) -> None:
        while True:
            try:
                now = self._now().astimezone(UTC)
                contracts = tuple(
                    contract
                    for contract in self._health.tracked_events.values()
                    if now < contract.end_time
                    and contract.lifecycle_state
                    not in {LifecycleState.CLOSED, LifecycleState.SETTLED}
                )
                if contracts:
                    quotes = await asyncio.to_thread(self._official_quotes.quotes, contracts)
                    self._accept_official_quotes(quotes)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception(
                    "Official quote recorder poll failed",
                    extra={"event": "recorder_official_quote_error", "error": str(error)},
                )
            await asyncio.sleep(self._settings.official_quote_poll_interval_seconds)

    def _accept_official_quotes(self, quotes: Sequence[PredictionMarketQuote]) -> None:
        for quote in quotes:
            tracked = self._health.tracked_events.get(quote.robinhood_event_id)
            if (
                tracked is None
                or tracked.contract_id != quote.robinhood_contract_id
                or tracked.asset is not quote.asset
            ):
                logger.warning(
                    "Official quote did not match the active Robinhood contract",
                    extra={
                        "event": "official_quote_contract_mismatch",
                        "asset": quote.asset,
                        "event_id": quote.robinhood_event_id,
                        "venue_ticker": quote.venue_ticker,
                    },
                )
                continue
            if quote.received_timestamp >= tracked.end_time:
                logger.warning(
                    "Post-end official quote suppressed",
                    extra={
                        "event": "official_quote_post_end_suppressed",
                        "asset": quote.asset,
                        "event_id": quote.robinhood_event_id,
                        "venue_ticker": quote.venue_ticker,
                    },
                )
                continue
            if self._store.append_prediction_quote(quote):
                self._health.written_record_count += 1
                logger.debug(
                    "Official venue quote written",
                    extra={
                        "event": "official_quote_written",
                        "asset": quote.asset,
                        "event_id": quote.robinhood_event_id,
                        "venue": quote.venue,
                        "venue_ticker": quote.venue_ticker,
                        "received_at": quote.received_timestamp,
                    },
                )
            self._health.official_quote_last_updates[quote.asset] = quote.received_timestamp
            if quote.freshness is FreshnessState.STALE:
                self._health.stale_official_quotes.add(quote.asset)
            else:
                self._health.stale_official_quotes.discard(quote.asset)

    async def _report_health(self) -> None:
        while True:
            await asyncio.sleep(self._settings.recorder_health_interval_seconds)
            logger.info(
                "Historical recorder health",
                extra={"event": "recorder_health", **self._health_fields()},
            )

    def _health_fields(self) -> dict[str, object]:
        health = self.health()
        return {
            "tracked_event_count": health.tracked_event_count,
            "last_robinhood_snapshot": health.last_robinhood_snapshot,
            "coinbase_last_updates": health.coinbase_last_updates,
            "official_quote_last_updates": health.official_quote_last_updates,
            "stale_source_count": health.stale_source_count,
            "written_record_count": health.written_record_count,
            "rollover_gap_count": len(health.rollover_gaps),
            "rollover_gaps": tuple(
                {
                    "asset": gap.asset.value,
                    "previous_event_id": gap.previous_event_id,
                    "started_at": gap.started_at,
                    "duration_seconds": gap.duration_seconds,
                }
                for gap in health.rollover_gaps
            ),
        }
