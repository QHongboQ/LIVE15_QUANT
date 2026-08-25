"""SDK-native market-data host for the Production Recorder.

This host deliberately owns only the transport side of the Recorder.  The
existing recorder continues to own discovery, official REST quotes, lifecycle
and settlement follow-up, health aggregation, checkpoints, and downstream
coordination.  The SDK owns its socket lifecycle; the reliability adapter is
the sole book/sequence/resync owner; the consumer owns durable batching.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from live15_quant.config import Settings
from live15_quant.kalshi_gateway.canonical_ws import canonical_from_sdk
from live15_quant.kalshi_gateway.client import (
    KalshiEnvironment,
    KalshiGatewayConfig,
    production_credentials,
)
from live15_quant.kalshi_gateway.recorder_consumer import (
    RecorderMarketDataConsumer,
    RecorderStoreDomainWriter,
)
from live15_quant.kalshi_gateway.recorder_provider import (
    RecorderMarketDataEvent,
    SdkRecorderMarketDataProvider,
)
from live15_quant.kalshi_gateway.websocket import KalshiWebSocketGateway
from live15_quant.models import Asset
from live15_quant.storage import RecorderStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SdkProductionRecorderHostSummary:
    """Final isolated-host facts captured only after its durable flush."""

    synchronized_count: int
    gap_count: int
    reconnect_count: int
    rows: dict[str, int]
    checkpoint_connection_id: str | None
    checkpoint_subscription_id: int | None
    checkpoint_sequence: int | None
    flushed_events: int


class SdkProductionRecorderHost:
    """Run the SDK transport path without importing legacy websocket classes."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: RecorderStore,
        universe: Callable[[], Mapping[Asset, str]],
        on_committed: Callable[[tuple[RecorderMarketDataEvent, ...]], None],
        on_transport_state: Callable[[str, datetime], None],
    ) -> None:
        self._settings = settings
        self._store = store
        self._universe = universe
        self._on_committed = on_committed
        self._on_transport_state = on_transport_state
        self._provider: SdkRecorderMarketDataProvider | None = None
        self._consumer: RecorderMarketDataConsumer | None = None
        self._last_received_at: datetime | None = None
        self._reconnect_count = 0
        self._queue_high_watermark = 0
        self._running = False
        self._final_summary: SdkProductionRecorderHostSummary | None = None

    @property
    def provider(self) -> SdkRecorderMarketDataProvider | None:
        return self._provider

    @property
    def last_received_at(self) -> datetime | None:
        return self._last_received_at

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def queue_high_watermark(self) -> int:
        return self._queue_high_watermark

    @property
    def synchronized_count(self) -> int:
        return 0 if self._provider is None else self._provider.synchronized_count

    @property
    def gap_count(self) -> int:
        return 0 if self._provider is None else self._provider.gap_count

    @property
    def running(self) -> bool:
        return self._running

    @property
    def final_summary(self) -> SdkProductionRecorderHostSummary | None:
        return self._final_summary

    async def _accept_typed_orderbook(
        self,
        message: Any,
        provider: SdkRecorderMarketDataProvider,
    ) -> None:
        """Copy one SDK typed orderbook event before its live maps mutate.

        ``kalshi-sdk`` documents snapshot ``yes``/``no`` maps as live
        OrderbookManager state.  Its public callback is invoked by the SDK
        dispatcher before the same model reaches the subscription iterator;
        materialize the immutable LIVE15 DTO here.  The iterator remains
        SDK-owned and is drained below solely to prevent its bounded queue
        from applying backpressure to the receive loop.
        """

        received_at = datetime.now(UTC)
        asset_by_ticker = {ticker: asset for asset, ticker in provider.current_universe.items()}
        event = canonical_from_sdk(
            message,
            asset_by_ticker=asset_by_ticker,
            connection_id=provider.connection_id,
            received_at=received_at,
        )
        provider.accept(event)
        self._last_received_at = received_at

    async def _consume(self, provider: SdkRecorderMarketDataProvider) -> None:
        assert self._consumer is not None
        async for event in provider.events():
            self._consumer.consume(event)

    def _capture_final_summary(self, consumer: RecorderMarketDataConsumer, flushed: int) -> None:
        checkpoint = consumer.checkpoint
        self._final_summary = SdkProductionRecorderHostSummary(
            synchronized_count=self.synchronized_count,
            gap_count=self.gap_count,
            reconnect_count=self._reconnect_count,
            rows=dict(self._store.bounded_row_count_estimates()),
            checkpoint_connection_id=(None if checkpoint is None else checkpoint.connection_id),
            checkpoint_subscription_id=(None if checkpoint is None else checkpoint.subscription_id),
            checkpoint_sequence=(None if checkpoint is None else checkpoint.sequence),
            flushed_events=flushed,
        )

    async def _drain(self, stream: Any, stop: asyncio.Event) -> None:
        count = 0
        async for _ in stream:
            if stop.is_set():
                return
            count += 1
            if count % 256 == 0:
                await asyncio.sleep(0)

    async def _watch_universe(
        self,
        initial: Mapping[Asset, str],
        session: Any,
        orderbook_client_id: int,
        gateway: KalshiWebSocketGateway,
        stop: asyncio.Event,
        changed: asyncio.Event,
    ) -> None:
        expected = dict(initial)
        while not stop.is_set() and not changed.is_set():
            await asyncio.sleep(1.0)
            updated = dict(self._universe())
            if updated == expected:
                continue
            # The existing Reliability Adapter is bound to one 10-market
            # universe.  A 15m rollover therefore remains a safe session
            # boundary until its explicit universe-replacement contract is
            # added.  Do not create a second subscription or locally route
            # frames here; request the server-side deletion first so the
            # current authoritative stream is closed fail-closed.
            removed = tuple(sorted(set(expected.values()) - set(updated.values())))
            if removed:
                await gateway.update_orderbook_subscription(
                    session,
                    client_id=orderbook_client_id,
                    delete_tickers=removed,
                )
            changed.set()
            return

    async def _run_session(self, asset_to_ticker: Mapping[Asset, str], stop: asyncio.Event) -> None:
        ticker_to_asset = {ticker: asset for asset, ticker in asset_to_ticker.items()}
        if len(ticker_to_asset) != len(Asset):
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except TimeoutError:
                pass
            return
        provider = SdkRecorderMarketDataProvider.isolated(
            asset_by_ticker=ticker_to_asset,
            connection_id=f"sdk-recorder-{uuid.uuid4().hex}",
            stale_seconds=self._settings.kalshi_websocket_stale_seconds,
        )

        def committed_current_session(events: tuple[RecorderMarketDataEvent, ...]) -> None:
            # A batch accepted before a reconnect may finish its durable
            # transaction after SDK has replaced the stream session.  Preserve
            # that historical write, but never let it repopulate the active
            # Recorder health/book projection after the old session has been
            # quarantined.
            current = provider.connection_id
            accepted = tuple(event for event in events if event.canonical.connection_id == current)
            if accepted:
                self._on_committed(accepted)

        consumer = RecorderMarketDataConsumer(
            RecorderStoreDomainWriter(self._store),
            batch_size=128,
            flush_interval_seconds=1.0,
            on_committed=committed_current_session,
        )
        self._provider, self._consumer = provider, consumer
        await provider.start()
        credentials = production_credentials(self._settings)
        config = KalshiGatewayConfig.for_environment(
            KalshiEnvironment.PRODUCTION,
            timeout_seconds=self._settings.kalshi_websocket_read_timeout_seconds,
            read_retries=3,
        )
        changed = asyncio.Event()
        timer_stop = asyncio.Event()
        accepting_orderbook_events = True

        async def state_change(_old: Any, new: Any) -> None:
            observed = datetime.now(UTC)
            value = str(getattr(new, "value", new))
            if value.lower() == "reconnecting":
                # SDK has declared the transport session replaced.  It owns
                # reconnect/resubscribe/new SID; LIVE15 replaces only its
                # session-local coordinator and waits for fresh snapshots.
                await provider.begin_reconnect_session(
                    connection_id=f"sdk-recorder-{uuid.uuid4().hex}",
                    observed_at=observed,
                    old_state=str(getattr(_old, "value", _old)),
                )
            else:
                await provider.connection_state_changed(
                    str(getattr(_old, "value", _old)), value, observed
                )
            self._on_transport_state(value, observed)

        gateway = KalshiWebSocketGateway(config, credentials)
        websocket = gateway.build(on_state_change=state_change, capture_pre_dispatch=False)

        @websocket.on("orderbook_delta")
        async def copy_typed_orderbook(message: Any) -> None:
            # The SDK may finish dispatching a buffered typed message while
            # its connection context is closing.  Once this host begins its
            # orderly shutdown, the provider is no longer an event sink.
            # Ignore only those late shutdown callbacks; normal frames still
            # enter through this immediate typed-message boundary.
            if not accepting_orderbook_events:
                return
            await self._accept_typed_orderbook(message, provider)

        tickers = sorted(ticker_to_asset)
        async with websocket.connect() as session:
            ticker = await session.subscribe_ticker(tickers=tickers, maxsize=2_000)
            lifecycle = await session.subscribe_market_lifecycle(tickers=tickers, maxsize=1_000)
            orderbook = await session.subscribe_orderbook_delta(tickers=tickers, maxsize=10_000)
            orderbook_client_id = gateway.orderbook_subscription_id(session)
            # Keep the SDK's one durable subscription queue empty.  The
            # callback above is the sole LIVE15 orderbook handoff and runs
            # during SDK dispatch, before future deltas mutate a snapshot's
            # live SDK maps.
            orderbook_drain_task = asyncio.create_task(
                self._drain(orderbook, stop), name="sdk-recorder-orderbook-drain"
            )
            consumer_task = asyncio.create_task(
                self._consume(provider), name="sdk-recorder-consumer"
            )
            timer_task = asyncio.create_task(
                consumer.run_idle_flush(timer_stop), name="sdk-recorder-idle-flush"
            )
            ancillary = {
                asyncio.create_task(self._drain(ticker, stop), name="sdk-recorder-ticker-drain"),
                asyncio.create_task(
                    self._drain(lifecycle, stop), name="sdk-recorder-lifecycle-drain"
                ),
                asyncio.create_task(
                    self._watch_universe(
                        asset_to_ticker,
                        session,
                        orderbook_client_id,
                        gateway,
                        stop,
                        changed,
                    ),
                    name="sdk-recorder-rollover",
                ),
            }
            stop_task = asyncio.create_task(stop.wait(), name="sdk-recorder-stop")
            changed_task = asyncio.create_task(changed.wait(), name="sdk-recorder-universe-changed")
            all_tasks = ancillary | {
                orderbook_drain_task,
                consumer_task,
                timer_task,
                stop_task,
                changed_task,
            }
            done, _pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)

            # Producer first: no more provider events may arrive after this
            # point.  The consumer is intentionally *not* cancelled yet.
            accepting_orderbook_events = False
            for task in ancillary | {orderbook_drain_task}:
                if task not in done:
                    task.cancel()
            await asyncio.gather(*(ancillary | {orderbook_drain_task}), return_exceptions=True)

            # Close the provider queue, then let consumer drain every event
            # already accepted by reliability before stopping its timer.
            await provider.stop()
            await asyncio.wait_for(consumer_task, timeout=10.0)
            timer_stop.set()
            await asyncio.wait_for(timer_task, timeout=10.0)
            flushed = consumer.close()
            self._capture_final_summary(consumer, flushed)

            for task in {stop_task, changed_task}:
                if task not in done:
                    task.cancel()
            await asyncio.gather(stop_task, changed_task, return_exceptions=True)
            if changed.is_set() and not stop.is_set():
                # A new 15-minute ticker invalidates the prior authoritative
                # set before the next SDK session obtains replacement snapshots.
                self._on_transport_state("reconnecting", datetime.now(UTC))
            if not stop.is_set() and not changed.is_set():
                for task in done:
                    if task.get_name() not in {
                        "sdk-recorder-stop",
                        "sdk-recorder-universe-changed",
                    }:
                        error = task.exception()
                        if error is not None:
                            raise error
                        raise RuntimeError(f"SDK recorder stream ended: {task.get_name()}")

    async def run(self, stop: asyncio.Event) -> None:
        self._running = True
        backoff = 1.0
        try:
            while not stop.is_set():
                try:
                    await self._run_session(self._universe(), stop)
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._reconnect_count += 1
                    self._on_transport_state("reconnecting", datetime.now(UTC))
                    logger.warning(
                        "SDK recorder session failed",
                        exc_info=True,
                        extra={
                            "event": "sdk_recorder_session_failure",
                            "error_type": type(error).__name__,
                        },
                    )
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=backoff)
                    except TimeoutError:
                        backoff = min(backoff * 2, 15.0)
        finally:
            if self._consumer is not None:
                self._consumer.close()
            if self._provider is not None:
                await self._provider.stop()
            self._running = False
