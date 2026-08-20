from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from live15_quant.config import Settings
from live15_quant.kalshi_lifecycle import (
    KalshiDiscovery,
    KalshiLifecycle,
)
from live15_quant.models import Asset, MarketTick
from live15_quant.native_recorder import KalshiNativeRecorder
from live15_quant.storage import RecorderStore
from tests.test_kalshi_lifecycle import NOW, provider, quote, raw_market
from tests.test_storage import prediction_quote


class FakeDiscovery:
    def __init__(self, discoveries: tuple[KalshiDiscovery, ...]) -> None:
        self.discoveries = discoveries
        self.calls = 0

    def discover_all(self, now=None):
        del now
        self.calls += 1
        return self.discoveries


class FakeQuotes:
    def __init__(self) -> None:
        self.calls = 0

    def quotes_native(self, markets):
        self.calls += 1
        assert all(market.lifecycle is KalshiLifecycle.OPEN for market in markets)
        return tuple(quote(market.ticker, market.event_ticker, NOW) for market in markets)


class OneTickStream:
    async def ticks(self):
        yield MarketTick(
            symbol="BTC-USD",
            price=Decimal("69541.123456789"),
            bid=Decimal("69541.12"),
            ask=Decimal("69541.13"),
            received_at=NOW,
            exchange_time=NOW - timedelta(milliseconds=1),
        )
        await asyncio.sleep(60)


class FailingRobinhood:
    def __init__(self) -> None:
        self.calls = 0

    def discover(self):
        self.calls += 1
        raise RuntimeError("optional reference unavailable")


def discovery() -> KalshiDiscovery:
    market = provider().parse_market(Asset.BTC, raw_market(), NOW)
    return KalshiDiscovery(Asset.BTC, NOW, None, market, None, (), ())


def test_robinhood_disabled_core_records_lifecycle_and_coinbase(tmp_path) -> None:
    reference = FailingRobinhood()
    fake_discovery = FakeDiscovery((discovery(),))
    quotes = FakeQuotes()

    async def scenario() -> None:
        with RecorderStore(tmp_path / "native.sqlite3") as store:
            recorder = KalshiNativeRecorder(
                Settings(
                    products=("BTC-USD",),
                    enable_robinhood_reference=False,
                    robinhood_poll_interval_seconds=0.01,
                    official_quote_poll_interval_seconds=0.01,
                    recorder_health_interval_seconds=1,
                ),
                store,
                discovery=fake_discovery,
                quotes=quotes,
                coinbase_factory=OneTickStream,
                robinhood_reference=reference,
                now=lambda: NOW,
            )
            task = asyncio.create_task(recorder.run())
            await asyncio.sleep(0.05)
            recorder.request_stop()
            await asyncio.wait_for(task, 1)
            assert store.count("kalshi_market_lifecycle") >= 1
            assert store.count("kalshi_prediction_quotes") == 1
            assert store.count("coinbase_ticks") == 1
            assert recorder.health().current_markets[Asset.BTC].startswith("KXBTC15M-")

    asyncio.run(scenario())
    assert reference.calls == 0
    assert fake_discovery.calls >= 1
    assert quotes.calls >= 1


def test_robinhood_reference_failure_never_blocks_kalshi_core(tmp_path) -> None:
    reference = FailingRobinhood()

    async def scenario() -> None:
        with RecorderStore(tmp_path / "native.sqlite3") as store:
            recorder = KalshiNativeRecorder(
                Settings(
                    products=("BTC-USD",),
                    enable_robinhood_reference=True,
                    robinhood_poll_interval_seconds=0.01,
                    official_quote_poll_interval_seconds=0.01,
                    recorder_health_interval_seconds=1,
                ),
                store,
                discovery=FakeDiscovery((discovery(),)),
                quotes=FakeQuotes(),
                coinbase_factory=OneTickStream,
                robinhood_reference=reference,
                now=lambda: NOW,
            )
            task = asyncio.create_task(recorder.run())
            await asyncio.sleep(0.05)
            recorder.request_stop()
            await asyncio.wait_for(task, 1)
            assert store.count("kalshi_market_lifecycle") >= 1
            assert recorder.health().robinhood_reference_healthy is False

    asyncio.run(scenario())
    assert reference.calls >= 1


def test_rollover_and_restart_recover_deterministic_lifecycle(tmp_path) -> None:
    path = tmp_path / "native.sqlite3"
    open_market = provider().parse_market(Asset.BTC, raw_market(), NOW)
    finalized = provider().parse_market(
        Asset.BTC, raw_market(status="finalized", result="yes"), NOW + timedelta(minutes=16)
    )
    next_market = provider().parse_market(
        Asset.BTC,
        raw_market(start=NOW.replace(minute=15), target="69542.25"),
        NOW + timedelta(minutes=16),
    )
    first_discovery = KalshiDiscovery(Asset.BTC, NOW, None, open_market, None, (), ())
    second_discovery = KalshiDiscovery(
        Asset.BTC,
        NOW + timedelta(minutes=16),
        finalized,
        next_market,
        None,
        (),
        (),
    )
    with RecorderStore(path) as store:
        recorder = KalshiNativeRecorder(
            Settings(),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
        )
        recorder._accept_discoveries((first_discovery,))
        recorder._accept_discoveries((second_discovery,))
        states = [record.lifecycle for record in store.replay_kalshi_markets(open_market.ticker)]
        assert states == [
            KalshiLifecycle.OPEN,
            KalshiLifecycle.CLOSED,
            KalshiLifecycle.SETTLEMENT_PENDING,
            KalshiLifecycle.SETTLED_YES,
        ]

    with RecorderStore(path) as store:
        restarted = KalshiNativeRecorder(
            Settings(),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW + timedelta(minutes=16),
        )
        assert restarted._health.states[open_market.ticker] is KalshiLifecycle.SETTLED_YES
        assert restarted.health().settlement_count == 1
        assert store.count("kalshi_settlements") == 1


def test_v3_to_v4_migration_failure_rolls_back(tmp_path) -> None:
    path = tmp_path / "rollback.sqlite3"
    market = provider().parse_market(Asset.BTC, raw_market(), NOW)
    with RecorderStore(path) as store:
        store.append_kalshi_quote(quote(market.ticker, market.event_ticker, NOW))
        store.append_prediction_quote(prediction_quote())
    import sqlite3

    connection = sqlite3.connect(path)
    for table in (
        "kalshi_prediction_quotes",
        "kalshi_market_lifecycle",
        "kalshi_settlements",
        "kalshi_settlement_conflicts",
        "kalshi_backfill_state",
    ):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("UPDATE recorder_metadata SET value='3'")
    connection.execute("UPDATE robinhood_snapshots SET schema_version=3")
    connection.execute("UPDATE coinbase_ticks SET schema_version=3")
    connection.execute("UPDATE robinhood_diagnostics SET schema_version=3")
    connection.execute("UPDATE prediction_market_quotes SET schema_version=2")
    connection.commit()
    connection.close()

    with pytest.raises(Exception, match="mixed schema"):
        RecorderStore(path)
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT value FROM recorder_metadata").fetchone()[0] == "3"
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kalshi_settlements'"
        ).fetchone()
        is None
    )
    connection.close()
