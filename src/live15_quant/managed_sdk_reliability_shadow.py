"""Independent kalshi-sdk -> reliability adapter -> shadow recorder runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from live15_quant.config import Settings, load_settings
from live15_quant.kalshi_gateway.canonical_ws import (
    canonical_from_sdk,
    unknown_lifecycle_event,
)
from live15_quant.kalshi_gateway.client import (
    KalshiEnvironment,
    KalshiGatewayConfig,
    build_sdk_client,
    production_credentials,
)
from live15_quant.kalshi_gateway.market_data import KalshiMarketDataGateway
from live15_quant.kalshi_gateway.reliability import (
    KalshiReliabilityAdapter,
    assert_shadow_path_isolated,
)
from live15_quant.kalshi_gateway.shadow_recorder import (
    RestSanityResult,
    RestSanityStatus,
    SdkReliabilityShadowRecorder,
    compare_rest_orderbook,
)
from live15_quant.kalshi_gateway.websocket import (
    GatewayReceivedMessage,
    GatewayWireDiagnostic,
    KalshiWebSocketGateway,
)
from live15_quant.logging_config import configure_logging
from live15_quant.models import Asset
from live15_quant.runtime_status import RuntimePidLease, atomic_json, read_json, utc_timestamp

logger = logging.getLogger(__name__)

REST_ALIGNMENT_SECONDS = 0.25
REDUNDANT_SDK_DRAIN_YIELD_EVERY = 256
AUTHORITATIVE_PUMP_YIELD_EVERY = 32
IMMUTABLE_ORDERBOOK_QUEUE_MAXSIZE = 100_000
SHADOW_DELTA_COMMIT_BATCH_SIZE = 4_096
SHADOW_HEARTBEAT_SECONDS = 5.0


def _event_ticker(market_ticker: str) -> str:
    head, separator, _contract = market_ticker.rpartition("-")
    return head if separator else market_ticker


def classify_event_lifecycle(
    diagnostic: GatewayWireDiagnostic,
    *,
    active_tickers: tuple[str, ...],
    previous_event_tickers: frozenset[str],
) -> str:
    """Classify the documented global event-creation lifecycle envelope."""

    event_ticker = diagnostic.event_ticker
    if not event_ticker:
        return "EVENT_LIFECYCLE_MALFORMED"
    current_events = {_event_ticker(ticker) for ticker in active_tickers}
    if event_ticker in current_events:
        return "EVENT_LIFECYCLE_CURRENT_WINDOW"
    if event_ticker in previous_event_tickers:
        return "STALE_LIFECYCLE"
    current_series = {ticker.split("-", 1)[0] for ticker in active_tickers}
    if diagnostic.series_ticker in current_series:
        return "EVENT_LIFECYCLE_NONCURRENT_WINDOW"
    return "EVENT_LIFECYCLE_UNRELATED"


def _paths(root: Path) -> tuple[Path, Path, Path]:
    return (
        root / "runtime" / "sdk-reliability-shadow-status.json",
        root / "runtime" / "sdk-reliability-shadow.pid",
        root / "data" / "sdk_reliability_shadow.sqlite3",
    )


def _current_universe(settings: Settings) -> dict[str, Asset]:
    health = read_json(settings.recorder_health_path)
    raw = health.get("current_markets") if isinstance(health, dict) else None
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Asset] = {}
    for asset in Asset:
        ticker = raw.get(asset.value)
        if isinstance(ticker, str) and ticker:
            result[ticker] = asset
    return result


def _sanitized_error(error: BaseException) -> str:
    parts = [type(error).__name__]
    cause = error.__cause__
    if cause is not None:
        parts.append(type(cause).__name__)
        response = getattr(cause, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            parts.append(f"HTTP_{status}")
    return "/".join(parts)


class SdkReliabilityShadowRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        recorder: SdkReliabilityShadowRecorder,
        status_path: Path,
        duration_seconds: float | None = None,
        rest_interval_seconds: float = 60.0,
        validation_reconnect_after_seconds: float | None = None,
    ) -> None:
        if duration_seconds is not None and duration_seconds <= 0:
            raise ValueError("shadow duration must be positive")
        if rest_interval_seconds <= 0:
            raise ValueError("REST sanity cadence must be positive")
        self.settings = settings
        self.recorder = recorder
        self.status_path = status_path
        self.duration_seconds = duration_seconds
        self.rest_interval_seconds = rest_interval_seconds
        self.validation_reconnect_after_seconds = validation_reconnect_after_seconds
        self.started_monotonic: float | None = None
        self.stop_event = asyncio.Event()
        self.adapter: KalshiReliabilityAdapter | None = None
        self.active_tickers: tuple[str, ...] = ()
        self.rollover_count = 0
        self.session_count = 0
        self.controlled_reconnect_count = 0
        self.last_rollover_reason: str | None = None
        self.last_error: str | None = None
        self.previous_event_tickers: set[str] = set()
        self._status_base: dict[str, object] = {}

    def status_payload(self, status: str) -> dict[str, object]:
        health = (
            self.adapter.health(datetime.now(UTC))
            if self.adapter is not None
            else {
                "connected_status": "disconnected",
                "subscribed_assets": len(self.active_tickers),
                "synchronized_count": 0,
                "assets": {},
                "metrics": self.recorder.summary(),
            }
        )
        payload = dict(self._status_base)
        payload.update(
            {
                "status": status,
                "last_heartbeat": utc_timestamp(),
                "last_error": self.last_error,
                "process_alive": status not in {"STOPPED", "ERROR"},
                "active_tickers": list(self.active_tickers),
                "connected_status": health["connected_status"],
                "subscribed_assets": health["subscribed_assets"],
                "synchronized_count": health["synchronized_count"],
                "rollover_count": self.rollover_count,
                "session_count": self.session_count,
                "controlled_reconnect_count": self.controlled_reconnect_count,
                "last_rollover_reason": self.last_rollover_reason,
                "health": health,
            }
        )
        return payload

    async def _heartbeat(self) -> None:
        while not self.stop_event.is_set():
            state = "RUNNING" if self.adapter is not None else "WAITING_TICKERS"
            atomic_json(self.status_path, self.status_payload(state))
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=SHADOW_HEARTBEAT_SECONDS)
            except TimeoutError:
                continue

    async def _duration_guard(self) -> None:
        if self.duration_seconds is None:
            await self.stop_event.wait()
            return
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=self.duration_seconds)
        except TimeoutError:
            self.stop_event.set()

    async def _watch_rollover(
        self,
        original: tuple[str, ...],
        changed: asyncio.Event,
    ) -> None:
        while not self.stop_event.is_set() and not changed.is_set():
            await asyncio.sleep(1.0)
            current = tuple(sorted(_current_universe(self.settings)))
            if len(current) == len(Asset) and current != original:
                observed_at = datetime.now(UTC)
                self.previous_event_tickers.update(_event_ticker(ticker) for ticker in original)
                self.recorder.record_rollover(
                    observed_at=observed_at,
                    reason="RECORDER_UNIVERSE_CHANGED",
                    old_tickers=original,
                    new_tickers=current,
                )
                self.rollover_count += 1
                self.last_rollover_reason = "RECORDER_UNIVERSE_CHANGED"
                adapter = self.adapter
                self.recorder.record_reconnect(
                    observed_at=observed_at,
                    initiator="LIVE15_INITIATED",
                    close_code=None,
                    close_reason="TICKER_ROLLOVER",
                    exception_type=None,
                    last_frame_age_seconds=(
                        None if adapter is None else adapter.maximum_last_frame_age(observed_at)
                    ),
                    affected_assets=tuple(sorted(Asset, key=lambda item: item.value)),
                    rollover_in_progress=True,
                )
                changed.set()

    async def _controlled_reconnect(self, changed: asyncio.Event) -> None:
        delay = self.validation_reconnect_after_seconds
        if delay is None or self.controlled_reconnect_count > 0:
            await self.stop_event.wait()
            return
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
        except TimeoutError:
            self.controlled_reconnect_count += 1
            self.last_rollover_reason = "BOUNDED_SHADOW_RECONNECT_VALIDATION"
            observed_at = datetime.now(UTC)
            adapter = self.adapter
            self.recorder.record_reconnect(
                observed_at=observed_at,
                initiator="LIVE15_INITIATED",
                close_code=None,
                close_reason="BOUNDED_VALIDATION_RECONNECT",
                exception_type=None,
                last_frame_age_seconds=(
                    None if adapter is None else adapter.maximum_last_frame_age(observed_at)
                ),
                affected_assets=tuple(sorted(Asset, key=lambda item: item.value)),
                rollover_in_progress=False,
            )
            changed.set()

    async def _pump(
        self,
        stream: Any,
        *,
        rollover: asyncio.Event,
        ignore_unknown_ticker: bool = False,
    ) -> None:
        processed = 0
        async for received in stream:
            if self.stop_event.is_set():
                return
            if isinstance(received, GatewayReceivedMessage):
                message = received.message
                received_at = received.received_at
            else:
                message = received
                received_at = datetime.now(UTC)
            if str(getattr(message, "type", "")) == "event_fee_update":
                continue
            adapter = self.adapter
            if adapter is None:
                raise RuntimeError("reliability adapter is unavailable")
            ticker = str(getattr(getattr(message, "msg", None), "market_ticker", ""))
            if ticker not in adapter.asset_by_ticker:
                if ignore_unknown_ticker:
                    diagnostic_type = (
                        "STALE_LIFECYCLE"
                        if _event_ticker(ticker) in self.previous_event_tickers
                        else "UNRELATED_MARKET_LIFECYCLE"
                    )
                    self.recorder.record_diagnostic(
                        observed_at=received_at,
                        diagnostic_type=diagnostic_type,
                        detail=str(getattr(message, "type", ""))[:120],
                        ticker=ticker or None,
                    )
                    await asyncio.sleep(0)
                    continue
                current = tuple(sorted(_current_universe(self.settings)))
                self.previous_event_tickers.update(
                    _event_ticker(value) for value in self.active_tickers
                )
                self.recorder.record_rollover(
                    observed_at=received_at,
                    reason="SDK_TICKER_OUTSIDE_CURRENT_UNIVERSE",
                    old_tickers=self.active_tickers,
                    new_tickers=current,
                )
                self.rollover_count += 1
                self.last_rollover_reason = "SDK_TICKER_OUTSIDE_CURRENT_UNIVERSE"
                rollover.set()
                return
            event = canonical_from_sdk(
                message,
                asset_by_ticker=adapter.asset_by_ticker,
                connection_id=adapter.connection_id,
                received_at=received_at,
            )
            adapter.accept(event)
            # Queue.get() completes synchronously while the high-volume feed
            # is non-empty. Yield in bounded batches: yielding every frame
            # halves persistence throughput and overflows the strict immutable
            # queue, while never yielding starves SDK ping/pong tasks.
            processed += 1
            if processed % AUTHORITATIVE_PUMP_YIELD_EVERY == 0:
                await asyncio.sleep(0)

    async def _pump_wire_diagnostics(self, stream: Any) -> None:
        async for diagnostic in stream:
            if self.stop_event.is_set():
                return
            if not isinstance(diagnostic, GatewayWireDiagnostic):
                continue
            adapter = self.adapter
            if adapter is None:
                continue
            ticker = diagnostic.market_ticker
            if diagnostic.diagnostic_kind == "MALFORMED_ORDERBOOK":
                adapter.payload_invalidated(
                    ticker=ticker,
                    subscription_id=diagnostic.subscription_id,
                    sequence=diagnostic.sequence,
                    observed_at=datetime.now(UTC),
                    reason=diagnostic.wire_type,
                )
                continue
            if diagnostic.diagnostic_kind == "EVENT_LIFECYCLE":
                classification = classify_event_lifecycle(
                    diagnostic,
                    active_tickers=self.active_tickers,
                    previous_event_tickers=frozenset(self.previous_event_tickers),
                )
                current_by_event = {
                    _event_ticker(value): (value, asset)
                    for value, asset in adapter.asset_by_ticker.items()
                }
                identity = current_by_event.get(diagnostic.event_ticker or "")
                self.recorder.record_diagnostic(
                    observed_at=diagnostic.received_at,
                    diagnostic_type=classification,
                    detail=json.dumps(
                        {
                            "wire_type": diagnostic.wire_type,
                            "event_ticker": diagnostic.event_ticker,
                            "series_ticker": diagnostic.series_ticker,
                            "exchange_index": diagnostic.exchange_index,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    asset=None if identity is None else identity[1],
                    ticker=None if identity is None else identity[0],
                )
                await asyncio.sleep(0)
                continue
            asset = adapter.asset_by_ticker.get(ticker or "")
            if asset is None or ticker is None:
                self.recorder.record_diagnostic(
                    observed_at=diagnostic.received_at,
                    diagnostic_type="GENUINELY_UNKNOWN_LIFECYCLE",
                    detail=diagnostic.wire_type,
                    ticker=ticker,
                )
                await asyncio.sleep(0)
                continue
            event = unknown_lifecycle_event(
                asset=asset,
                ticker=ticker,
                connection_id=adapter.connection_id,
                observed_at=diagnostic.received_at,
                wire_type=diagnostic.wire_type,
            )
            adapter.accept(event)
            await asyncio.sleep(0)

    async def _pump_lifecycle_channel(
        self,
        session: Any,
        stream: Any,
        *,
        rollover: asyncio.Event,
    ) -> None:
        """Keep a server-ended lifecycle channel on the same SDK socket."""

        current = stream
        while not self.stop_event.is_set() and not rollover.is_set():
            await self._pump(
                current,
                rollover=rollover,
                ignore_unknown_ticker=True,
            )
            if self.stop_event.is_set() or rollover.is_set():
                return
            self.recorder.record_diagnostic(
                observed_at=datetime.now(UTC),
                diagnostic_type="SDK_LIFECYCLE_CHANNEL_ENDED",
                detail="SDK_PUBLIC_CHANNEL_RESUBSCRIBE",
            )
            # This is a channel-level public SDK subscribe, not a transport
            # reconnect. The SDK remains the sole owner of the socket.
            await asyncio.sleep(0.25)
            current = await session.subscribe_market_lifecycle(
                tickers=list(self.active_tickers),
                maxsize=1_000,
            )

    async def _pump_ticker_channel(
        self,
        session: Any,
        stream: Any,
        *,
        rollover: asyncio.Event,
    ) -> None:
        """Keep a server-ended ticker channel on the same SDK socket."""

        current = stream
        while not self.stop_event.is_set() and not rollover.is_set():
            await self._pump(current, rollover=rollover)
            if self.stop_event.is_set() or rollover.is_set():
                return
            self.recorder.record_diagnostic(
                observed_at=datetime.now(UTC),
                diagnostic_type="SDK_TICKER_CHANNEL_ENDED",
                detail="SDK_PUBLIC_CHANNEL_RESUBSCRIBE",
            )
            await asyncio.sleep(0.25)
            current = await session.subscribe_ticker(
                tickers=list(self.active_tickers),
                maxsize=2_000,
            )

    async def _drain_orderbook_channel(
        self,
        session: Any,
        stream: Any,
        *,
        rollover: asyncio.Event,
    ) -> None:
        """Quarantine then resubscribe an ended orderbook channel in-place."""

        current = stream
        while not self.stop_event.is_set() and not rollover.is_set():
            await self._drain(current)
            if self.stop_event.is_set() or rollover.is_set():
                return
            observed_at = datetime.now(UTC)
            adapter = self.adapter
            if adapter is not None:
                # A channel sentinel leaves continuity unknown. Quarantine all
                # assets until authoritative replacement snapshots arrive;
                # never keep consuming the previous books as synchronized.
                adapter.payload_invalidated(
                    ticker=None,
                    subscription_id=None,
                    sequence=None,
                    observed_at=observed_at,
                    reason="SDK_ORDERBOOK_CHANNEL_ENDED",
                )
            self.recorder.record_diagnostic(
                observed_at=observed_at,
                diagnostic_type="SDK_ORDERBOOK_CHANNEL_ENDED",
                detail="SDK_PUBLIC_CHANNEL_RESUBSCRIBE",
            )
            await asyncio.sleep(0.25)
            current = await session.subscribe_orderbook_delta(
                tickers=list(self.active_tickers),
                maxsize=10_000,
            )

    async def _drain(self, stream: Any) -> None:
        drained = 0
        async for _message in stream:
            if self.stop_event.is_set():
                return
            # The authoritative immutable feed already retains and validates
            # every wire orderbook frame. This SDK iterator exists only to
            # keep the SDK's mandatory parsed subscription queue empty. Drain
            # it in bounded batches; yielding every item lets a high-volume
            # duplicate queue fill and makes its backpressure policy tear down
            # the otherwise healthy transport.
            drained += 1
            if drained % REDUNDANT_SDK_DRAIN_YIELD_EVERY == 0:
                await asyncio.sleep(0)

    @staticmethod
    def _unwrap_orderbook(value: object) -> object:
        nested = getattr(value, "orderbook", None)
        return nested if nested is not None else value

    async def _await_ws_alignment_watermark(
        self,
        adapter: KalshiReliabilityAdapter,
        target: datetime,
        *,
        timeout_seconds: float = 1.0,
    ) -> None:
        """Wait boundedly for the consumer, not the socket, to cross target."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while not adapter.orderbook_processed_through(target) and loop.time() < deadline:
            await asyncio.sleep(0.01)

    async def _rest_sanity(self, market_data: KalshiMarketDataGateway) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(self.rest_interval_seconds)
            adapter = self.adapter
            if adapter is None:
                continue
            for asset in sorted(adapter.assets, key=lambda item: item.value):
                if self.stop_event.is_set() or adapter is not self.adapter:
                    return
                ticker = adapter.ticker_by_asset[asset]
                request_started_at = datetime.now(UTC)
                try:
                    adapter.book(asset)
                    raw = await asyncio.to_thread(market_data.orderbook, ticker, depth=10)
                    response_received_at = datetime.now(UTC)
                    # Allow only the documented narrow alignment window for a
                    # WS update already in flight. REST never mutates the book.
                    await self._await_ws_alignment_watermark(
                        adapter,
                        response_received_at + timedelta(seconds=REST_ALIGNMENT_SECONDS),
                    )
                    if adapter is not self.adapter:
                        return
                    after = adapter.book(asset)
                    aligned = adapter.nearest_price_sample(
                        asset,
                        target=response_received_at,
                        tolerance_seconds=REST_ALIGNMENT_SECONDS,
                    )
                    result = compare_rest_orderbook(
                        asset=asset,
                        ticker=ticker,
                        checked_at=response_received_at,
                        ws_book=after,
                        rest_orderbook=self._unwrap_orderbook(raw),
                        request_started_at=request_started_at,
                        response_received_at=response_received_at,
                        aligned_sample=aligned,
                        interval_samples=adapter.price_samples(
                            asset,
                            since=request_started_at - timedelta(seconds=REST_ALIGNMENT_SECONDS),
                            until=response_received_at + timedelta(seconds=REST_ALIGNMENT_SECONDS),
                        ),
                        alignment_tolerance_seconds=REST_ALIGNMENT_SECONDS,
                    )
                except Exception as error:
                    response_received_at = datetime.now(UTC)
                    result = RestSanityResult(
                        asset=asset,
                        ticker=ticker,
                        checked_at=response_received_at,
                        status=RestSanityStatus.UNAVAILABLE,
                        ws_sequence=None,
                        ws_yes_bid=None,
                        ws_yes_ask=None,
                        ws_no_bid=None,
                        ws_no_ask=None,
                        rest_yes_bid=None,
                        rest_yes_ask=None,
                        rest_no_bid=None,
                        rest_no_ask=None,
                        reason=type(error).__name__,
                        request_started_at=request_started_at,
                        response_received_at=response_received_at,
                    )
                self.recorder.record_rest_sanity(result)

    async def _run_session(
        self,
        universe: dict[str, Asset],
        market_data: KalshiMarketDataGateway,
    ) -> None:
        credentials = production_credentials(self.settings)
        config = KalshiGatewayConfig.for_environment(
            KalshiEnvironment.PRODUCTION,
            timeout_seconds=self.settings.kalshi_websocket_read_timeout_seconds,
            read_retries=3,
        )
        connection_id = f"sdk-reliability-{uuid.uuid4().hex}"
        self.adapter = KalshiReliabilityAdapter(
            universe,
            self.recorder,
            connection_id=connection_id,
            stale_seconds=self.settings.kalshi_websocket_stale_seconds,
        )
        rollover = asyncio.Event()

        async def state_change(old: Any, new: Any) -> None:
            adapter = self.adapter
            if adapter is None:
                return
            observed_at = datetime.now(UTC)
            old_value = str(getattr(old, "value", old))
            new_value = str(getattr(new, "value", new))
            if new_value.lower() == "reconnecting":
                self.recorder.record_reconnect(
                    observed_at=observed_at,
                    initiator="SDK_INTERNAL",
                    close_code=None,
                    close_reason="SDK_PUBLIC_CALLBACK_DOES_NOT_EXPOSE_CLOSE_METADATA",
                    exception_type="ConnectionClosed",
                    last_frame_age_seconds=adapter.maximum_last_frame_age(observed_at),
                    affected_assets=tuple(sorted(adapter.assets, key=lambda item: item.value)),
                    rollover_in_progress=rollover.is_set(),
                )
            adapter.connection_state_changed(old_value, new_value, observed_at)

        async def on_error(error: Any) -> None:
            self.last_error = _sanitized_error(error)

        gateway = KalshiWebSocketGateway(config, credentials)
        immutable_orderbook = gateway.immutable_orderbook_stream(
            maxsize=IMMUTABLE_ORDERBOOK_QUEUE_MAXSIZE
        )
        diagnostics = gateway.wire_diagnostic_stream()
        websocket = gateway.build(on_state_change=state_change, on_error=on_error)
        self.active_tickers = tuple(sorted(universe))
        self.session_count += 1
        async with websocket.connect() as session:
            # The JSON hook receives initial orderbook frames while the SDK is
            # still waiting for the subscribe acknowledgement. Start the
            # immutable consumer first so a Production burst cannot fill the
            # strict queue before subscribe_orderbook_delta() returns.
            early_tasks = {
                asyncio.create_task(
                    self._pump(immutable_orderbook, rollover=rollover),
                    name="reliability-orderbook",
                ),
                asyncio.create_task(
                    self._pump_wire_diagnostics(diagnostics), name="wire-diagnostics"
                ),
            }
            try:
                ticker = await session.subscribe_ticker(
                    tickers=list(self.active_tickers), maxsize=2_000
                )
                lifecycle = await session.subscribe_market_lifecycle(
                    tickers=list(self.active_tickers), maxsize=1_000
                )
                sdk_orderbook = await session.subscribe_orderbook_delta(
                    tickers=list(self.active_tickers), maxsize=10_000
                )
                tasks = early_tasks | {
                    asyncio.create_task(
                        self._drain_orderbook_channel(
                            session,
                            sdk_orderbook,
                            rollover=rollover,
                        ),
                        name="sdk-orderbook-channel",
                    ),
                    asyncio.create_task(
                        self._pump_ticker_channel(
                            session,
                            ticker,
                            rollover=rollover,
                        ),
                        name="reliability-ticker",
                    ),
                    asyncio.create_task(
                        self._pump_lifecycle_channel(
                            session,
                            lifecycle,
                            rollover=rollover,
                        ),
                        name="reliability-lifecycle",
                    ),
                    asyncio.create_task(
                        self._watch_rollover(self.active_tickers, rollover),
                        name="rollover-watch",
                    ),
                    asyncio.create_task(
                        self._controlled_reconnect(rollover), name="controlled-reconnect"
                    ),
                    asyncio.create_task(self._rest_sanity(market_data), name="rest-sanity"),
                    asyncio.create_task(self.stop_event.wait(), name="stop"),
                    asyncio.create_task(rollover.wait(), name="rollover"),
                }
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                # Cooperative stop/rollover makes every channel wrapper exit.
                # Which completed task asyncio returns first is nondeterministic;
                # do not misclassify a sibling's expected exit as transport
                # failure merely because its name is not "rollover"/"stop".
                if self.stop_event.is_set() or rollover.is_set():
                    return
                for task in done:
                    if task.get_name() in {"stop", "rollover"}:
                        continue
                    if task.get_name() in {"rollover-watch", "controlled-reconnect"} and (
                        rollover.is_set()
                    ):
                        continue
                    error = task.exception()
                    if error is not None:
                        raise error
                    self.recorder.record_diagnostic(
                        observed_at=datetime.now(UTC),
                        diagnostic_type="UNEXPECTED_STREAM_COMPLETION",
                        detail=task.get_name(),
                    )
                    raise RuntimeError(f"SDK stream ended unexpectedly: {task.get_name()}")
            finally:
                for task in early_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*early_tasks, return_exceptions=True)

    async def run(self) -> None:
        config = KalshiGatewayConfig.for_environment(
            KalshiEnvironment.PRODUCTION,
            timeout_seconds=self.settings.kalshi_websocket_read_timeout_seconds,
            read_retries=3,
        )
        credentials = production_credentials(self.settings)
        client = build_sdk_client(config, credentials=credentials)
        market_data = KalshiMarketDataGateway(client)
        heartbeat = asyncio.create_task(self._heartbeat(), name="reliability-heartbeat")
        duration = asyncio.create_task(self._duration_guard(), name="duration")
        try:
            backoff = 1.0
            while not self.stop_event.is_set():
                universe = _current_universe(self.settings)
                if len(universe) != len(Asset):
                    self.active_tickers = tuple(sorted(universe))
                    await asyncio.sleep(1.0)
                    continue
                try:
                    self.last_error = None
                    await self._run_session(universe, market_data)
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    observed_at = datetime.now(UTC)
                    adapter = self.adapter
                    self.last_error = _sanitized_error(error)
                    self.recorder.record_reconnect(
                        observed_at=observed_at,
                        initiator="LIVE15_INITIATED",
                        close_code=None,
                        close_reason="SDK_FATAL_STATE_BOUNDED_RECOVERY",
                        exception_type=type(error).__name__,
                        last_frame_age_seconds=(
                            None if adapter is None else adapter.maximum_last_frame_age(observed_at)
                        ),
                        affected_assets=tuple(sorted(Asset, key=lambda item: item.value)),
                        rollover_in_progress=False,
                    )
                    logger.warning(
                        "SDK reliability shadow session failed",
                        exc_info=True,
                        extra={"event": "sdk_reliability_shadow_failure"},
                    )
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=backoff)
                    except TimeoutError:
                        pass
                    backoff = min(15.0, backoff * 2)
                finally:
                    self.adapter = None
        finally:
            self.stop_event.set()
            heartbeat.cancel()
            duration.cancel()
            await asyncio.gather(heartbeat, duration, return_exceptions=True)


async def _run(args: argparse.Namespace) -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    root = Path.cwd().resolve()
    status_path, lease_path, store_path = _paths(root)
    assert_shadow_path_isolated(store_path, settings.recorder_data_path)
    lease = RuntimePidLease(lease_path)
    lease.acquire()
    recorder = SdkReliabilityShadowRecorder(
        store_path,
        official_recorder_path=settings.recorder_data_path,
        commit_batch_size=SHADOW_DELTA_COMMIT_BATCH_SIZE,
    )
    runner = SdkReliabilityShadowRunner(
        settings=settings,
        recorder=recorder,
        status_path=status_path,
        duration_seconds=args.duration,
        rest_interval_seconds=args.rest_interval,
        validation_reconnect_after_seconds=args.validation_reconnect_after,
    )
    started = utc_timestamp()
    runner._status_base = {
        "pid": os.getpid(),
        "started_at": started,
        "expected_mode": "SDK_RELIABILITY_SHADOW_NO_RECORDER_WRITES",
        "store_path": str(store_path),
        "official_recorder_writes": False,
        "sdk_endpoint": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    }
    atomic_json(status_path, runner.status_payload("STARTING"))
    try:
        await runner.run()
        atomic_json(status_path, runner.status_payload("STOPPED"))
    except Exception as error:
        runner.last_error = type(error).__name__
        atomic_json(status_path, runner.status_payload("ERROR"))
        raise
    finally:
        recorder.close()
        lease.release()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--duration", type=float)
    value.add_argument("--rest-interval", type=float, default=60.0)
    value.add_argument("--validation-reconnect-after", type=float)
    return value


def main() -> None:
    asyncio.run(_run(parser().parse_args()))


if __name__ == "__main__":
    main()
