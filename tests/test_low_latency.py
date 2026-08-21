from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from live15_quant.latency_benchmark import LatencyBenchmarkStore, LowLatencyBenchmarkRunner
from live15_quant.models import Asset
from live15_quant.providers.low_latency import (
    PYTH_PRO_WEBSOCKET_URLS,
    BenchmarkPayloadError,
    BenchmarkTick,
    BinanceBnbBenchmarkSource,
    HyperliquidHypeBenchmarkSource,
    LowLatencyProvider,
    PythProBenchmarkSource,
    SourceDiagnostics,
    SourceTimestampSemantics,
    parse_binance_agg_trade,
    parse_hyperliquid_bbo,
    parse_pyth_pro_update,
)

NOW = datetime(2026, 8, 21, 0, 0, tzinfo=UTC)


def test_binance_trade_preserves_decimal_and_trade_timestamp() -> None:
    tick = parse_binance_agg_trade(
        {"e": "aggTrade", "s": "BNBUSDT", "a": 42, "p": "912.12345600", "T": 1_777_000_000_123},
        socket_received=NOW,
        parse_completed=NOW + timedelta(microseconds=20),
    )
    assert tick.asset is Asset.BNB
    assert tick.price == Decimal("912.12345600")
    assert tick.source_timestamp.microsecond == 123000
    assert tick.timestamp_semantics is SourceTimestampSemantics.TRADE_TIME


def test_binance_ticker_mismatch_fails_loudly() -> None:
    with pytest.raises(BenchmarkPayloadError, match="identity mismatch"):
        parse_binance_agg_trade(
            {"e": "aggTrade", "s": "BTCUSDT", "a": 42, "p": "1", "T": 1},
            socket_received=NOW,
            parse_completed=NOW,
        )


def test_monotonic_stage_order_survives_wall_clock_adjustment() -> None:
    tick = parse_binance_agg_trade(
        {"e": "aggTrade", "s": "BNBUSDT", "a": 43, "p": "912", "T": 1_777_000_000_123},
        socket_received=NOW,
        parse_completed=NOW - timedelta(milliseconds=1),
        socket_received_monotonic_ns=1_000,
        parse_completed_monotonic_ns=2_000,
    )
    assert tick.parse_completed_timestamp < tick.socket_received_timestamp
    assert tick.parse_completed_monotonic_ns > tick.socket_received_monotonic_ns


def test_hyperliquid_bbo_requires_exact_hype_identity() -> None:
    ticks = parse_hyperliquid_bbo(
        {
            "channel": "bbo",
            "data": {
                "coin": "HYPE",
                "time": 1_777_000_000_456,
                "bbo": [{"px": "44.0000"}, {"px": "44.0002"}],
            },
        },
        socket_received=NOW,
        parse_completed=NOW + timedelta(microseconds=10),
    )
    assert len(ticks) == 1
    assert ticks[0].provider is LowLatencyProvider.HYPERLIQUID_PERP
    assert ticks[0].price == Decimal("44.00010")
    assert ticks[0].bid == Decimal("44.0000")
    assert ticks[0].ask == Decimal("44.0002")
    assert ticks[0].source_event_id == "1777000000456:44.0000:44.0002"

    with pytest.raises(BenchmarkPayloadError, match="HYPE BBO identity"):
        parse_hyperliquid_bbo(
            {"channel": "bbo", "data": {"coin": "SOL", "bbo": []}},
            socket_received=NOW,
            parse_completed=NOW,
        )


def test_pyth_pro_exact_id_precision_and_timestamp() -> None:
    ticks = parse_pyth_pro_update(
        {
            "type": "streamUpdated",
            "parsed": {
                "priceFeeds": [
                    {
                        "priceFeedId": 346,
                        "price": "4312345",
                        "confidence": "125",
                        "exponent": -3,
                        "feedUpdateTimestamp": 1_777_000_000_789_123,
                    }
                ]
            },
        },
        socket_received=NOW,
        parse_completed=NOW + timedelta(microseconds=25),
    )
    assert ticks[0].asset is Asset.GOLD
    assert ticks[0].price == Decimal("4312.345")
    assert ticks[0].confidence == Decimal("0.125")
    assert ticks[0].source_timestamp.microsecond == 789123


def test_pyth_pro_preserves_actual_endpoint_provenance() -> None:
    endpoint = "wss://pyth-lazer-2.dourolabs.app/v1/stream"
    ticks = parse_pyth_pro_update(
        {
            "type": "streamUpdated",
            "parsed": {
                "priceFeeds": [
                    {
                        "priceFeedId": 15,
                        "price": "90125000000",
                        "exponent": -8,
                        "feedUpdateTimestamp": 1_777_000_000_000_001,
                    }
                ]
            },
        },
        socket_received=NOW,
        parse_completed=NOW,
        provenance=endpoint,
    )
    assert ticks[0].provenance == endpoint
    assert ticks[0].source_timestamp.microsecond == 1


def test_pyth_pro_subscription_ack_requires_all_exact_feeds() -> None:
    expected = [346, 345, 110, 15]
    assert (
        parse_pyth_pro_update(
            {
                "type": "subscribedWithInvalidFeedIdsIgnored",
                "subscribedFeedIds": expected,
                "ignoredInvalidFeedIds": {
                    "unknownIds": [],
                    "unknownSymbols": [],
                    "unsupportedChannels": [],
                    "unstable": [],
                    "notEntitled": [],
                },
            },
            socket_received=NOW,
            parse_completed=NOW,
        )
        == ()
    )
    with pytest.raises(BenchmarkPayloadError, match="ignored a configured feed"):
        parse_pyth_pro_update(
            {
                "type": "subscribedWithInvalidFeedIdsIgnored",
                "subscribedFeedIds": expected,
                "ignoredInvalidFeedIds": {"notEntitled": [346]},
            },
            socket_received=NOW,
            parse_completed=NOW,
        )


def test_pyth_pro_unknown_id_and_exponent_conflict_fail_loudly() -> None:
    base = {
        "type": "streamUpdated",
        "parsed": {
            "priceFeeds": [
                {
                    "priceFeedId": 999999,
                    "price": "1",
                    "exponent": -3,
                    "feedUpdateTimestamp": 1,
                }
            ]
        },
    }
    with pytest.raises(BenchmarkPayloadError, match="malformed"):
        parse_pyth_pro_update(base, socket_received=NOW, parse_completed=NOW)

    base["parsed"]["priceFeeds"][0]["priceFeedId"] = 346  # type: ignore[index]
    base["parsed"]["priceFeeds"][0]["exponent"] = -8  # type: ignore[index]
    with pytest.raises(BenchmarkPayloadError, match="exponent conflicts"):
        parse_pyth_pro_update(base, socket_received=NOW, parse_completed=NOW)


class FakeWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.sent: list[dict[str, Any]] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


class FakeReceiveWebSocket(FakeWebSocket):
    async def recv(self) -> str:
        return await self.__anext__()


class FakeConnection:
    def __init__(
        self, websocket: FakeWebSocket | None = None, error: Exception | None = None
    ) -> None:
        self.websocket = websocket
        self.error = error

    async def __aenter__(self) -> FakeWebSocket:
        if self.error is not None:
            raise self.error
        assert self.websocket is not None
        return self.websocket

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_binance_reconnect_waits_for_real_second_attempt() -> None:
    attempts = 0
    sleeps: list[float] = []
    second_attempt = asyncio.Event()
    websocket = FakeWebSocket(
        [json.dumps({"e": "aggTrade", "s": "BNBUSDT", "a": 1, "p": "900", "T": 1_777_000_000_000})]
    )

    def connector(*_args: object, **_kwargs: object) -> FakeConnection:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return FakeConnection(error=OSError("offline"))
        second_attempt.set()
        return FakeConnection(websocket=websocket)

    async def sleeper(value: float) -> None:
        sleeps.append(value)

    source = BinanceBnbBenchmarkSource(connector=connector, sleeper=sleeper)
    stream = source.ticks()
    tick = await anext(stream)
    await second_attempt.wait()
    await source.close()
    await stream.aclose()
    assert tick.asset is Asset.BNB
    assert attempts == 2
    assert source.diagnostics.reconnects == 1
    assert sleeps == [0.25]


@pytest.mark.asyncio
async def test_binance_server_shutdown_reconnects_without_parsing_control_message() -> None:
    attempts = 0
    sleeps: list[float] = []
    first = FakeWebSocket([json.dumps({"e": "serverShutdown"})])
    second = FakeWebSocket(
        [
            json.dumps(
                {
                    "e": "aggTrade",
                    "s": "BNBUSDT",
                    "a": 2,
                    "p": "901.25",
                    "T": 1_777_000_000_001,
                }
            )
        ]
    )

    def connector(*_args: object, **_kwargs: object) -> FakeConnection:
        nonlocal attempts
        attempts += 1
        return FakeConnection(websocket=first if attempts == 1 else second)

    async def sleeper(value: float) -> None:
        sleeps.append(value)

    source = BinanceBnbBenchmarkSource(connector=connector, sleeper=sleeper)
    stream = source.ticks()
    tick = await anext(stream)
    await source.close()
    await stream.aclose()

    assert tick.price == Decimal("901.25")
    assert attempts == 2
    assert source.diagnostics.reconnects == 1
    assert sleeps == [0.25]


@pytest.mark.asyncio
async def test_repeated_clean_binance_closes_use_exponential_backoff() -> None:
    attempts = 0
    sleeps: list[float] = []

    def connector(*_args: object, **_kwargs: object) -> FakeConnection:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return FakeConnection(websocket=FakeWebSocket([json.dumps({"e": "serverShutdown"})]))
        return FakeConnection(
            websocket=FakeWebSocket(
                [
                    json.dumps(
                        {
                            "e": "aggTrade",
                            "s": "BNBUSDT",
                            "a": 3,
                            "p": "902",
                            "T": 1_777_000_000_002,
                        }
                    )
                ]
            )
        )

    async def sleeper(value: float) -> None:
        sleeps.append(value)

    source = BinanceBnbBenchmarkSource(connector=connector, sleeper=sleeper)
    stream = source.ticks()
    assert (await anext(stream)).price == Decimal("902")
    await source.close()
    await stream.aclose()
    assert attempts == 3
    assert sleeps == [0.25, 0.5]


@pytest.mark.asyncio
async def test_hyperliquid_transport_failure_reconnects_before_hype_bbo() -> None:
    attempts = 0
    sleeps: list[float] = []
    websocket = FakeReceiveWebSocket(
        [
            json.dumps({"channel": "subscriptionResponse"}),
            json.dumps(
                {
                    "channel": "bbo",
                    "data": {
                        "coin": "HYPE",
                        "time": 1_777_000_000_003,
                        "bbo": [{"px": "44.1"}, {"px": "44.2"}],
                    },
                }
            ),
        ]
    )

    def connector(*_args: object, **_kwargs: object) -> FakeConnection:
        nonlocal attempts
        attempts += 1
        return (
            FakeConnection(error=OSError("offline"))
            if attempts == 1
            else FakeConnection(websocket=websocket)
        )

    async def sleeper(value: float) -> None:
        sleeps.append(value)

    source = HyperliquidHypeBenchmarkSource(connector=connector, sleeper=sleeper)
    stream = source.ticks()
    tick = await anext(stream)
    await source.close()
    await stream.aclose()
    assert tick.asset is Asset.HYPE
    assert attempts == 2
    assert sleeps == [0.25]


@pytest.mark.asyncio
async def test_pyth_pro_reconnect_rotates_endpoint_and_preserves_provenance(tmp_path: Path) -> None:
    attempts: list[str] = []
    sleeps: list[float] = []
    key_path = tmp_path / "pyth.key"
    key_path.write_text("test-only-secret", encoding="utf-8")
    websocket = FakeWebSocket(
        [
            json.dumps(
                {
                    "type": "streamUpdated",
                    "parsed": {
                        "priceFeeds": [
                            {
                                "priceFeedId": 15,
                                "price": "90300000000",
                                "exponent": -8,
                                "feedUpdateTimestamp": 1_777_000_000_000_004,
                            }
                        ]
                    },
                }
            )
        ]
    )

    def connector(url: str, **_kwargs: object) -> FakeConnection:
        attempts.append(url)
        return (
            FakeConnection(error=OSError("offline"))
            if len(attempts) == 1
            else FakeConnection(websocket=websocket)
        )

    async def sleeper(value: float) -> None:
        sleeps.append(value)

    source = PythProBenchmarkSource(key_path, connector=connector, sleeper=sleeper)
    stream = source.ticks()
    tick = await anext(stream)
    await source.close()
    await stream.aclose()
    assert attempts == list(PYTH_PRO_WEBSOCKET_URLS[:2])
    assert tick.provenance == PYTH_PRO_WEBSOCKET_URLS[1]
    assert sleeps == [0.25]


class OneTickSource:
    def __init__(self, emitted: asyncio.Event) -> None:
        self.emitted = emitted
        self.closed = False
        self.diagnostics = SourceDiagnostics(connection_attempts=1)

    async def ticks(self):
        socket_monotonic_ns = time.perf_counter_ns()
        yield BenchmarkTick(
            asset=Asset.BNB,
            provider=LowLatencyProvider.BINANCE_SPOT,
            symbol="BNB/USDT",
            instrument_id="BNBUSDT",
            price=Decimal("900.1234"),
            source_timestamp=datetime.now(UTC) - timedelta(milliseconds=5),
            socket_received_timestamp=datetime.now(UTC),
            parse_completed_timestamp=datetime.now(UTC),
            timestamp_semantics=SourceTimestampSemantics.TRADE_TIME,
            source_event_id="1",
            provenance="wss://official.example/ws",
            socket_received_monotonic_ns=socket_monotonic_ns,
            parse_completed_monotonic_ns=time.perf_counter_ns(),
        )
        self.emitted.set()
        while not self.closed:
            await asyncio.sleep(60)

    async def close(self) -> None:
        self.closed = True


class FailingSource:
    def __init__(self) -> None:
        self.diagnostics = SourceDiagnostics(connection_attempts=1, transport_errors=1)

    async def ticks(self):
        if False:
            yield None
        raise OSError("isolated source outage")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_benchmark_pipeline_is_bounded_and_uses_temporary_store() -> None:
    emitted = asyncio.Event()
    source = OneTickSource(emitted)

    async def wait_for_sample(_seconds: float) -> None:
        await asyncio.wait_for(emitted.wait(), 1)
        await asyncio.sleep(0)

    report = await LowLatencyBenchmarkRunner([source], duration_waiter=wait_for_sample).run(30)
    summary = report["assets"]["BNB:binance_spot"]
    assert summary["observations"] == 1
    assert summary["receive_persist_ms"]["max"] is not None
    assert report["temporary_store_deleted"] is True
    assert report["production_recorder_touched"] is False
    assert report["queue_drops"] == 0
    assert report["measurement_complete"] is True


@pytest.mark.asyncio
async def test_one_source_failure_does_not_block_other_benchmark_sources() -> None:
    emitted = asyncio.Event()
    healthy = OneTickSource(emitted)

    async def wait_for_healthy_sample(_seconds: float) -> None:
        await asyncio.wait_for(emitted.wait(), 1)
        await asyncio.sleep(0)

    report = await LowLatencyBenchmarkRunner(
        [FailingSource(), healthy], duration_waiter=wait_for_healthy_sample
    ).run(30)
    assert report["assets"]["BNB:binance_spot"]["observations"] == 1
    assert report["source_errors"] == {"FailingSource": "OSError"}
    assert report["measurement_complete"] is False


class BurstSource(OneTickSource):
    async def ticks(self):
        socket_monotonic_ns = time.perf_counter_ns()
        for index in range(5):
            yield BenchmarkTick(
                asset=Asset.BNB,
                provider=LowLatencyProvider.BINANCE_SPOT,
                symbol="BNB/USDT",
                instrument_id="BNBUSDT",
                price=Decimal(900 + index),
                source_timestamp=NOW,
                socket_received_timestamp=NOW,
                parse_completed_timestamp=NOW,
                timestamp_semantics=SourceTimestampSemantics.TRADE_TIME,
                source_event_id=str(index),
                provenance="wss://official.example/ws",
                socket_received_monotonic_ns=socket_monotonic_ns + index,
                parse_completed_monotonic_ns=socket_monotonic_ns + index,
            )
        self.emitted.set()
        while not self.closed:
            await asyncio.sleep(60)


@pytest.mark.asyncio
async def test_bounded_queue_reports_every_overload_drop() -> None:
    emitted = asyncio.Event()
    source = BurstSource(emitted)

    async def wait_for_burst(_seconds: float) -> None:
        await emitted.wait()

    report = await LowLatencyBenchmarkRunner(
        [source], queue_size=2, duration_waiter=wait_for_burst
    ).run(30)
    assert report["assets"]["BNB:binance_spot"]["observations"] == 2
    assert report["queue_drops"] == 3
    assert report["measurement_complete"] is False


@pytest.mark.asyncio
async def test_persistence_failure_fails_promptly_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted = asyncio.Event()
    source = OneTickSource(emitted)

    def fail_append(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr(LatencyBenchmarkStore, "append", fail_append)

    async def wait_for_sample(_seconds: float) -> None:
        await emitted.wait()
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="simulated persistence failure"):
        await LowLatencyBenchmarkRunner([source], duration_waiter=wait_for_sample).run(30)


def test_pyth_pro_repr_never_exposes_credential(tmp_path: Path) -> None:
    key_path = tmp_path / "outside.key"
    key_path.write_text("secret-value-that-must-not-appear", encoding="utf-8")
    client = PythProBenchmarkSource(key_path)
    try:
        representation = repr(client)
        assert "secret-value" not in representation
        assert str(key_path) not in representation
        assert "authenticated=True" in representation
    finally:
        asyncio.run(client.close())
