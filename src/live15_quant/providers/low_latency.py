"""Read-only official low-latency underlying streams used for isolated benchmarks."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from live15_quant.models import Asset
from live15_quant.providers.pyth import PythHermesClient, read_pyth_api_key

BINANCE_BNB_WEBSOCKET_URL = "wss://data-stream.binance.vision:443/ws/bnbusdt@aggTrade"
HYPERLIQUID_WEBSOCKET_URL = "wss://api.hyperliquid.xyz/ws"
PYTH_PRO_WEBSOCKET_URLS = (
    "wss://pyth-lazer-0.dourolabs.app/v1/stream",
    "wss://pyth-lazer-1.dourolabs.app/v1/stream",
    "wss://pyth-lazer-2.dourolabs.app/v1/stream",
)


class LowLatencyProvider(StrEnum):
    BINANCE_SPOT = "binance_spot"
    HYPERLIQUID_PERP = "hyperliquid_perp"
    PYTH_CORE = "pyth_core"
    PYTH_PRO = "pyth_pro"


class SourceTimestampSemantics(StrEnum):
    TRADE_TIME = "venue_trade_time"
    BBO_TIME = "venue_bbo_time"
    PYTH_PUBLISH_TIME = "pyth_publish_time"
    PYTH_FEED_UPDATE_TIME = "pyth_feed_update_timestamp"


class BenchmarkPayloadError(ValueError):
    """An official payload violates the documented identity or schema."""


class BenchmarkNetworkError(ConnectionError):
    """A sanitized, retryable benchmark transport failure."""


@dataclass(frozen=True, slots=True)
class BenchmarkTick:
    """One source-specific predictive observation with local pipeline clocks."""

    asset: Asset
    provider: LowLatencyProvider
    symbol: str
    instrument_id: str
    price: Decimal
    source_timestamp: datetime
    socket_received_timestamp: datetime
    parse_completed_timestamp: datetime
    timestamp_semantics: SourceTimestampSemantics
    source_event_id: str
    provenance: str
    confidence: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    socket_received_monotonic_ns: int = 0
    parse_completed_monotonic_ns: int = 0

    def __post_init__(self) -> None:
        if not all((self.symbol, self.instrument_id, self.source_event_id, self.provenance)):
            raise ValueError("benchmark identifiers and provenance must not be empty")
        for timestamp in (
            self.source_timestamp,
            self.socket_received_timestamp,
            self.parse_completed_timestamp,
        ):
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("benchmark timestamps must be timezone-aware")
        if (
            self.socket_received_monotonic_ns < 0
            or self.parse_completed_monotonic_ns < self.socket_received_monotonic_ns
        ):
            raise ValueError("benchmark monotonic stage timestamps are invalid")
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("benchmark price must be finite and positive")
        if self.confidence is not None and (not self.confidence.is_finite() or self.confidence < 0):
            raise ValueError("benchmark confidence must be finite and non-negative")
        if (self.bid is None) != (self.ask is None):
            raise ValueError("benchmark bid and ask must be present together")
        if self.bid is not None and (self.bid <= 0 or self.ask is None or self.ask < self.bid):
            raise ValueError("benchmark bid/ask must form a valid positive book")


@dataclass(slots=True)
class SourceDiagnostics:
    connection_attempts: int = 0
    reconnects: int = 0
    malformed_messages: int = 0
    transport_errors: int = 0


class BenchmarkSource(Protocol):
    diagnostics: SourceDiagnostics

    async def ticks(self) -> AsyncIterator[BenchmarkTick]: ...

    async def close(self) -> None: ...


Connector = Callable[..., Any]
Clock = Callable[[], datetime]
MonotonicClock = Callable[[], int]
Sleeper = Callable[[float], Awaitable[None]]


def _aware_now() -> datetime:
    return datetime.now(UTC)


def _backoff(attempt: int, base: float, maximum: float) -> float:
    return min(maximum, base * (2 ** min(attempt, 8)))


def _from_unix_millis(value: int) -> datetime:
    seconds, millis = divmod(value, 1_000)
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=millis * 1_000)


def _from_unix_micros(value: int) -> datetime:
    seconds, micros = divmod(value, 1_000_000)
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=micros)


def parse_binance_agg_trade(
    payload: object,
    *,
    socket_received: datetime,
    parse_completed: datetime,
    socket_received_monotonic_ns: int = 0,
    parse_completed_monotonic_ns: int = 0,
) -> BenchmarkTick:
    if not isinstance(payload, dict):
        raise BenchmarkPayloadError("Binance payload must be an object")
    if payload.get("e") != "aggTrade" or payload.get("s") != "BNBUSDT":
        raise BenchmarkPayloadError("Binance stream identity mismatch")
    try:
        price = Decimal(str(payload["p"]))
        source_timestamp = _from_unix_millis(int(payload["T"]))
        event_id = str(int(payload["a"]))
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError, OSError):
        raise BenchmarkPayloadError("Binance aggregate trade is malformed") from None
    return BenchmarkTick(
        asset=Asset.BNB,
        provider=LowLatencyProvider.BINANCE_SPOT,
        symbol="BNB/USDT",
        instrument_id="BNBUSDT",
        price=price,
        source_timestamp=source_timestamp,
        socket_received_timestamp=socket_received,
        parse_completed_timestamp=parse_completed,
        timestamp_semantics=SourceTimestampSemantics.TRADE_TIME,
        source_event_id=event_id,
        provenance=BINANCE_BNB_WEBSOCKET_URL,
        socket_received_monotonic_ns=socket_received_monotonic_ns,
        parse_completed_monotonic_ns=parse_completed_monotonic_ns,
    )


def parse_hyperliquid_bbo(
    payload: object,
    *,
    socket_received: datetime,
    parse_completed: datetime,
    socket_received_monotonic_ns: int = 0,
    parse_completed_monotonic_ns: int = 0,
) -> tuple[BenchmarkTick, ...]:
    if not isinstance(payload, dict):
        raise BenchmarkPayloadError("Hyperliquid payload must be an object")
    channel = payload.get("channel")
    if channel in {"subscriptionResponse", "pong"}:
        return ()
    if channel != "bbo" or not isinstance(payload.get("data"), dict):
        raise BenchmarkPayloadError("Hyperliquid stream identity mismatch")
    item = payload["data"]
    if item.get("coin") != "HYPE" or not isinstance(item.get("bbo"), list):
        raise BenchmarkPayloadError("Hyperliquid HYPE BBO identity mismatch")
    try:
        source_millis = int(item["time"])
        bid_level, ask_level = item["bbo"]
        if not isinstance(bid_level, dict) or not isinstance(ask_level, dict):
            raise TypeError
        bid = Decimal(str(bid_level["px"]))
        ask = Decimal(str(ask_level["px"]))
        midpoint = (bid + ask) / 2
        source_timestamp = _from_unix_millis(source_millis)
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError, OSError):
        raise BenchmarkPayloadError("Hyperliquid BBO is malformed") from None
    return (
        BenchmarkTick(
            asset=Asset.HYPE,
            provider=LowLatencyProvider.HYPERLIQUID_PERP,
            symbol="HYPE/USDC perpetual BBO",
            instrument_id="HYPE",
            price=midpoint,
            source_timestamp=source_timestamp,
            socket_received_timestamp=socket_received,
            parse_completed_timestamp=parse_completed,
            timestamp_semantics=SourceTimestampSemantics.BBO_TIME,
            source_event_id=f"{source_millis}:{bid}:{ask}",
            provenance=HYPERLIQUID_WEBSOCKET_URL,
            bid=bid,
            ask=ask,
            socket_received_monotonic_ns=socket_received_monotonic_ns,
            parse_completed_monotonic_ns=parse_completed_monotonic_ns,
        ),
    )


class BinanceBnbBenchmarkSource:
    """Public, unauthenticated Binance Spot aggregate-trade stream for BNB."""

    def __init__(
        self,
        *,
        connector: Connector = connect,
        clock: Clock = _aware_now,
        monotonic_ns: MonotonicClock = time.perf_counter_ns,
        sleeper: Sleeper = asyncio.sleep,
        base_backoff_seconds: float = 0.25,
        max_backoff_seconds: float = 5.0,
    ) -> None:
        self._connector = connector
        self._clock = clock
        self._monotonic_ns = monotonic_ns
        self._sleeper = sleeper
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._closed = False
        self.diagnostics = SourceDiagnostics()

    async def ticks(self) -> AsyncIterator[BenchmarkTick]:
        failures = 0
        while not self._closed:
            self.diagnostics.connection_attempts += 1
            try:
                async with self._connector(
                    BINANCE_BNB_WEBSOCKET_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=10,
                    close_timeout=5,
                    max_queue=1024,
                ) as websocket:
                    async for message in websocket:
                        socket_received_monotonic_ns = self._monotonic_ns()
                        socket_received = self._clock()
                        try:
                            payload = json.loads(message)
                        except (TypeError, ValueError):
                            self.diagnostics.malformed_messages += 1
                            continue
                        if payload.get("e") == "serverShutdown":
                            break
                        parse_completed_monotonic_ns = self._monotonic_ns()
                        parse_completed = self._clock()
                        tick = parse_binance_agg_trade(
                            payload,
                            socket_received=socket_received,
                            parse_completed=parse_completed,
                            socket_received_monotonic_ns=socket_received_monotonic_ns,
                            parse_completed_monotonic_ns=parse_completed_monotonic_ns,
                        )
                        failures = 0
                        yield tick
                if not self._closed:
                    self.diagnostics.reconnects += 1
                    await self._sleeper(_backoff(failures, self._base_backoff, self._max_backoff))
                    failures += 1
            except (ConnectionClosed, InvalidHandshake, OSError, TimeoutError):
                if self._closed:
                    return
                self.diagnostics.transport_errors += 1
                self.diagnostics.reconnects += 1
                await self._sleeper(_backoff(failures, self._base_backoff, self._max_backoff))
                failures += 1

    async def close(self) -> None:
        self._closed = True


class HyperliquidHypeBenchmarkSource:
    """Public HYPE perpetual BBO from Hyperliquid's documented mainnet API."""

    def __init__(
        self,
        *,
        connector: Connector = connect,
        clock: Clock = _aware_now,
        monotonic_ns: MonotonicClock = time.perf_counter_ns,
        sleeper: Sleeper = asyncio.sleep,
        heartbeat_seconds: float = 30.0,
        base_backoff_seconds: float = 0.25,
        max_backoff_seconds: float = 5.0,
    ) -> None:
        self._connector = connector
        self._clock = clock
        self._monotonic_ns = monotonic_ns
        self._sleeper = sleeper
        self._heartbeat = heartbeat_seconds
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._closed = False
        self.diagnostics = SourceDiagnostics()

    async def ticks(self) -> AsyncIterator[BenchmarkTick]:
        failures = 0
        subscription = {
            "method": "subscribe",
            "subscription": {"type": "bbo", "coin": "HYPE"},
        }
        while not self._closed:
            self.diagnostics.connection_attempts += 1
            try:
                async with self._connector(
                    HYPERLIQUID_WEBSOCKET_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=10,
                    close_timeout=5,
                    max_queue=1024,
                ) as websocket:
                    await websocket.send(json.dumps(subscription))
                    while not self._closed:
                        try:
                            message = await asyncio.wait_for(
                                websocket.recv(), timeout=self._heartbeat
                            )
                        except TimeoutError:
                            await websocket.send(json.dumps({"method": "ping"}))
                            continue
                        socket_received_monotonic_ns = self._monotonic_ns()
                        socket_received = self._clock()
                        try:
                            payload = json.loads(message)
                        except (TypeError, ValueError):
                            self.diagnostics.malformed_messages += 1
                            continue
                        parse_completed_monotonic_ns = self._monotonic_ns()
                        parse_completed = self._clock()
                        ticks = parse_hyperliquid_bbo(
                            payload,
                            socket_received=socket_received,
                            parse_completed=parse_completed,
                            socket_received_monotonic_ns=socket_received_monotonic_ns,
                            parse_completed_monotonic_ns=parse_completed_monotonic_ns,
                        )
                        if ticks:
                            failures = 0
                        for tick in ticks:
                            yield tick
                if not self._closed:
                    self.diagnostics.reconnects += 1
                    await self._sleeper(_backoff(failures, self._base_backoff, self._max_backoff))
                    failures += 1
            except (ConnectionClosed, InvalidHandshake, OSError, TimeoutError):
                if self._closed:
                    return
                self.diagnostics.transport_errors += 1
                self.diagnostics.reconnects += 1
                await self._sleeper(_backoff(failures, self._base_backoff, self._max_backoff))
                failures += 1

    async def close(self) -> None:
        self._closed = True


PYTH_PRO_FEEDS: Mapping[Asset, tuple[str, int, int]] = MappingProxyType(
    {
        # Resolved by exact symbol through Pyth's authenticated /v1/symbols metadata API.
        Asset.GOLD: ("Metal.XAU/USD", 346, -3),
        Asset.SILVER: ("Metal.XAG/USD", 345, -5),
        Asset.HYPE: ("Crypto.HYPE/USD", 110, -8),
        Asset.BNB: ("Crypto.BNB/USD", 15, -8),
    }
)
_PYTH_PRO_BY_ID = {
    feed_id: (asset, symbol, exponent)
    for asset, (symbol, feed_id, exponent) in PYTH_PRO_FEEDS.items()
}


def parse_pyth_pro_update(
    payload: object,
    *,
    socket_received: datetime,
    parse_completed: datetime,
    provenance: str = PYTH_PRO_WEBSOCKET_URLS[0],
    socket_received_monotonic_ns: int = 0,
    parse_completed_monotonic_ns: int = 0,
) -> tuple[BenchmarkTick, ...]:
    if not isinstance(payload, dict):
        raise BenchmarkPayloadError("Pyth Pro payload must be an object")
    message_type = payload.get("type")
    if message_type in {
        "subscriptionAck",
        "subscribedWithInvalidFeedIdsIgnored",
        "subscriptionError",
    }:
        if message_type == "subscriptionError":
            raise BenchmarkPayloadError("Pyth Pro rejected the benchmark subscription")
        if message_type == "subscribedWithInvalidFeedIdsIgnored":
            expected = {item[1] for item in PYTH_PRO_FEEDS.values()}
            subscribed = payload.get("subscribedFeedIds")
            ignored = payload.get("ignoredInvalidFeedIds")
            if not isinstance(subscribed, list) or {int(item) for item in subscribed} != expected:
                raise BenchmarkPayloadError("Pyth Pro subscription identity mismatch")
            if not isinstance(ignored, dict) or any(ignored.values()):
                raise BenchmarkPayloadError("Pyth Pro ignored a configured feed")
        return ()
    parsed = payload.get("parsed")
    if payload.get("type") != "streamUpdated" or not isinstance(parsed, dict):
        raise BenchmarkPayloadError("Pyth Pro stream envelope is malformed")
    updates = parsed.get("priceFeeds")
    if not isinstance(updates, list):
        raise BenchmarkPayloadError("Pyth Pro price feed collection is malformed")
    ticks: list[BenchmarkTick] = []
    for item in updates:
        if not isinstance(item, dict):
            raise BenchmarkPayloadError("Pyth Pro feed item is malformed")
        try:
            feed_id = int(item["priceFeedId"])
            asset, symbol, configured_exponent = _PYTH_PRO_BY_ID[feed_id]
            exponent = int(item.get("exponent", configured_exponent))
            if exponent != configured_exponent:
                raise BenchmarkPayloadError("Pyth Pro exponent conflicts with official metadata")
            price = Decimal(str(item["price"])) * (Decimal(10) ** exponent)
            confidence_value = item.get("confidence")
            confidence = (
                Decimal(str(confidence_value)) * (Decimal(10) ** exponent)
                if confidence_value is not None
                else None
            )
            update_micros = int(item["feedUpdateTimestamp"])
            source_timestamp = _from_unix_micros(update_micros)
        except BenchmarkPayloadError:
            raise
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError, OSError):
            raise BenchmarkPayloadError("Pyth Pro feed item is malformed") from None
        ticks.append(
            BenchmarkTick(
                asset=asset,
                provider=LowLatencyProvider.PYTH_PRO,
                symbol=symbol,
                instrument_id=str(feed_id),
                price=price,
                source_timestamp=source_timestamp,
                socket_received_timestamp=socket_received,
                parse_completed_timestamp=parse_completed,
                timestamp_semantics=SourceTimestampSemantics.PYTH_FEED_UPDATE_TIME,
                source_event_id=f"{feed_id}:{update_micros}:{price}:{confidence}",
                provenance=provenance,
                confidence=confidence,
                socket_received_monotonic_ns=socket_received_monotonic_ns,
                parse_completed_monotonic_ns=parse_completed_monotonic_ns,
            )
        )
    return tuple(ticks)


class PythProBenchmarkSource:
    """Authenticated Pyth Pro benchmark; never exposes or persists its key."""

    def __init__(
        self,
        key_path: Path,
        *,
        connector: Connector = connect,
        clock: Clock = _aware_now,
        monotonic_ns: MonotonicClock = time.perf_counter_ns,
        sleeper: Sleeper = asyncio.sleep,
        base_backoff_seconds: float = 0.25,
        max_backoff_seconds: float = 5.0,
    ) -> None:
        self._key = read_pyth_api_key(key_path)
        self._connector = connector
        self._clock = clock
        self._monotonic_ns = monotonic_ns
        self._sleeper = sleeper
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._closed = False
        self.diagnostics = SourceDiagnostics()

    async def ticks(self) -> AsyncIterator[BenchmarkTick]:
        failures = 0
        subscription = {
            "type": "subscribe",
            "subscriptionId": 1,
            "priceFeedIds": [item[1] for item in PYTH_PRO_FEEDS.values()],
            "properties": ["price", "confidence", "exponent", "feedUpdateTimestamp"],
            "formats": [],
            "channel": "fixed_rate@200ms",
            "ignoreInvalidFeeds": True,
        }
        while not self._closed:
            endpoint = PYTH_PRO_WEBSOCKET_URLS[
                self.diagnostics.connection_attempts % len(PYTH_PRO_WEBSOCKET_URLS)
            ]
            self.diagnostics.connection_attempts += 1
            try:
                async with self._connector(
                    endpoint,
                    additional_headers={"Authorization": f"Bearer {self._key}"},
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=10,
                    close_timeout=5,
                    max_queue=1024,
                ) as websocket:
                    await websocket.send(json.dumps(subscription))
                    async for message in websocket:
                        socket_received_monotonic_ns = self._monotonic_ns()
                        socket_received = self._clock()
                        try:
                            payload = json.loads(message)
                        except (TypeError, ValueError):
                            self.diagnostics.malformed_messages += 1
                            continue
                        parse_completed_monotonic_ns = self._monotonic_ns()
                        parse_completed = self._clock()
                        ticks = parse_pyth_pro_update(
                            payload,
                            socket_received=socket_received,
                            parse_completed=parse_completed,
                            provenance=endpoint,
                            socket_received_monotonic_ns=socket_received_monotonic_ns,
                            parse_completed_monotonic_ns=parse_completed_monotonic_ns,
                        )
                        if ticks:
                            failures = 0
                        for tick in ticks:
                            yield tick
                if not self._closed:
                    self.diagnostics.reconnects += 1
                    await self._sleeper(_backoff(failures, self._base_backoff, self._max_backoff))
                    failures += 1
            except (ConnectionClosed, InvalidHandshake, OSError, TimeoutError):
                if self._closed:
                    return
                self.diagnostics.transport_errors += 1
                self.diagnostics.reconnects += 1
                await self._sleeper(_backoff(failures, self._base_backoff, self._max_backoff))
                failures += 1

    async def close(self) -> None:
        self._closed = True
        self._key = ""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(authenticated=True, feeds={len(PYTH_PRO_FEEDS)})"


class PythCoreBenchmarkSource:
    """Adapter around the unchanged five-feed Pyth Core client for comparison."""

    def __init__(
        self,
        client: PythHermesClient,
        *,
        clock: Clock = _aware_now,
        monotonic_ns: MonotonicClock = time.perf_counter_ns,
    ) -> None:
        self._client = client
        self._clock = clock
        self._monotonic_ns = monotonic_ns
        self._closed = False
        self.diagnostics = SourceDiagnostics()

    @staticmethod
    def _next(iterator: Any) -> tuple[bool, Any]:
        try:
            return True, next(iterator)
        except StopIteration:
            return False, None

    async def ticks(self) -> AsyncIterator[BenchmarkTick]:
        while not self._closed:
            self.diagnostics.connection_attempts += 1
            iterator = self._client.stream_batches()
            while not self._closed:
                try:
                    present, batch = await asyncio.to_thread(self._next, iterator)
                except Exception as error:
                    if self._closed:
                        return
                    self.diagnostics.transport_errors += 1
                    self.diagnostics.reconnects += 1
                    raise BenchmarkNetworkError("Pyth Core benchmark stream failed") from error
                if not present:
                    if self._closed:
                        return
                    self.diagnostics.reconnects += 1
                    break
                parse_completed = self._clock()
                parse_completed_monotonic_ns = (
                    batch.parse_completed_monotonic_ns or self._monotonic_ns()
                )
                socket_received_monotonic_ns = (
                    batch.socket_received_monotonic_ns or parse_completed_monotonic_ns
                )
                self.diagnostics.malformed_messages += len(batch.issues)
                for observation in batch.observations:
                    yield BenchmarkTick(
                        asset=observation.asset,
                        provider=LowLatencyProvider.PYTH_CORE,
                        symbol=observation.symbol,
                        instrument_id=observation.feed_id,
                        price=observation.price,
                        source_timestamp=observation.source_timestamp,
                        socket_received_timestamp=observation.received_timestamp,
                        parse_completed_timestamp=parse_completed,
                        timestamp_semantics=SourceTimestampSemantics.PYTH_PUBLISH_TIME,
                        source_event_id=(
                            f"{observation.feed_id}:{observation.source_timestamp.isoformat()}:"
                            f"{observation.price}:{observation.confidence}"
                        ),
                        provenance=observation.provenance,
                        confidence=observation.confidence,
                        socket_received_monotonic_ns=socket_received_monotonic_ns,
                        parse_completed_monotonic_ns=parse_completed_monotonic_ns,
                    )

    async def close(self) -> None:
        self._closed = True
        await asyncio.to_thread(self._client.close)
