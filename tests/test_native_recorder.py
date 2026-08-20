from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import requests

import live15_quant.cli as cli
from live15_quant.config import Settings
from live15_quant.kalshi_lifecycle import (
    KalshiDiscovery,
    KalshiLifecycle,
)
from live15_quant.models import Asset, MarketTick
from live15_quant.native_recorder import KalshiNativeRecorder
from live15_quant.providers.kalshi import KalshiPublicApiError, KalshiTargetUnavailableError
from live15_quant.storage import RecorderStore
from tests.test_kalshi_lifecycle import NOW, provider, quote, raw_market
from tests.test_storage import prediction_quote


class FakeDiscovery:
    def __init__(self, discoveries: tuple[KalshiDiscovery, ...]) -> None:
        self.discoveries = {item.asset: item for item in discoveries}
        self.followups = {
            market.ticker: market for item in discoveries for market in item.valid_markets
        }
        self.calls = 0

    def discover(self, asset, now=None):
        del now
        self.calls += 1
        return self.discoveries.get(asset, KalshiDiscovery(asset, NOW, None, None, None, (), ()))

    def get_market(self, asset, ticker, *, historical=False):
        del asset, historical
        return self.followups[ticker]


class FakeQuotes:
    def __init__(self) -> None:
        self.calls = 0

    def quote_native(self, market):
        self.calls += 1
        assert market.lifecycle is KalshiLifecycle.OPEN
        return quote(market.ticker, market.event_ticker, NOW)


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


def discovery_for(asset: Asset) -> KalshiDiscovery:
    market = provider().parse_market(asset, raw_market(asset), NOW)
    return KalshiDiscovery(asset, NOW, None, market, None, (), ())


class AssetFailureQuotes:
    def __init__(self, failure: type[Exception]) -> None:
        self.failure = failure
        self.calls: dict[Asset, int] = {}

    def quote_native(self, market):
        self.calls[market.asset] = self.calls.get(market.asset, 0) + 1
        if market.asset is Asset.SILVER:
            raise self.failure("bounded Silver source condition")
        return replace(
            quote(market.ticker, market.event_ticker, NOW),
            asset=market.asset,
            series=market.series,
        )


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
                    native_discovery_poll_interval_seconds=0.01,
                    recorder_health_interval_seconds=1,
                    recorder_health_path=tmp_path / "health.json",
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
                    native_discovery_poll_interval_seconds=0.01,
                    recorder_health_interval_seconds=1,
                    recorder_health_path=tmp_path / "health.json",
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
        recorder._accept_discovery(first_discovery)
        recorder._accept_discovery(second_discovery)
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


class IsolatedDiscovery(FakeDiscovery):
    def __init__(self, failures: set[Asset] | None = None) -> None:
        super().__init__((discovery(),))
        self.failures = failures or set()
        self.called: set[Asset] = set()
        self.all_called = threading.Event()

    def discover(self, asset, now=None):
        self.called.add(asset)
        if len(self.called) == len(Asset):
            self.all_called.set()
        if asset in self.failures:
            raise requests.ConnectionError("bounded source outage")
        return super().discover(asset, now)


def test_multi_asset_network_failure_isolated_and_health_is_machine_readable(tmp_path) -> None:
    fake = IsolatedDiscovery({Asset.ETH})
    health_path = tmp_path / "health.json"

    async def scenario() -> None:
        with RecorderStore(tmp_path / "native.sqlite3") as store:
            recorder = KalshiNativeRecorder(
                Settings(
                    products=("BTC-USD",),
                    native_discovery_poll_interval_seconds=60,
                    recorder_health_path=health_path,
                ),
                store,
                discovery=fake,
                quotes=FakeQuotes(),
                coinbase_factory=OneTickStream,
                now=lambda: NOW,
            )
            task = asyncio.create_task(recorder.run())
            assert await asyncio.to_thread(fake.all_called.wait, 1)
            await asyncio.sleep(0)
            recorder.request_stop()
            await asyncio.wait_for(task, 1)
            health = recorder.health()
            assert Asset.BTC in health.current_markets
            assert health.retry_counts["kalshi_discovery:ETH"] == 1
            assert health.source_failures["kalshi_discovery:ETH"] == "ConnectionError"
            assert health.integrity == "ok"

    asyncio.run(scenario())
    payload = json.loads(health_path.read_text(encoding="utf-8"))
    assert payload["status"] == "degraded"
    assert payload["current_markets"]["BTC"].startswith("KXBTC15M-")


def test_target_unavailable_quote_isolated_to_one_asset(tmp_path) -> None:
    quotes = AssetFailureQuotes(KalshiTargetUnavailableError)

    async def scenario() -> None:
        with RecorderStore(tmp_path / "native.sqlite3") as store:
            recorder = KalshiNativeRecorder(
                Settings(
                    products=("BTC-USD",),
                    official_quote_poll_interval_seconds=0.005,
                    native_discovery_poll_interval_seconds=60,
                    recorder_health_interval_seconds=0.01,
                    recorder_health_path=tmp_path / "health.json",
                ),
                store,
                discovery=FakeDiscovery((discovery_for(Asset.BTC), discovery_for(Asset.SILVER))),
                quotes=quotes,
                coinbase_factory=OneTickStream,
                now=lambda: NOW,
            )
            task = asyncio.create_task(recorder.run())
            try:
                for _ in range(100):
                    health = recorder.health()
                    if quotes.calls.get(Asset.BTC, 0) and health.source_failures.get(
                        "kalshi_quote:Silver"
                    ):
                        break
                    await asyncio.sleep(0.005)
                assert not task.done()
                assert quotes.calls.get(Asset.BTC, 0) >= 1
                assert quotes.calls.get(Asset.SILVER, 0) >= 1
                health = recorder.health()
                assert health.source_failures["kalshi_quote:Silver"] == (
                    "KalshiTargetUnavailableError"
                )
                assert health.fatal_task is None
            finally:
                recorder.request_stop()
                await asyncio.wait_for(task, 1)

    asyncio.run(scenario())


def test_malformed_quote_remains_global_correctness_failure(tmp_path) -> None:
    quotes = AssetFailureQuotes(KalshiPublicApiError)
    health_path = tmp_path / "health.json"

    async def scenario() -> None:
        with RecorderStore(tmp_path / "native.sqlite3") as store:
            recorder = KalshiNativeRecorder(
                Settings(
                    products=("BTC-USD",),
                    official_quote_poll_interval_seconds=0.005,
                    native_discovery_poll_interval_seconds=60,
                    recorder_health_path=health_path,
                ),
                store,
                discovery=FakeDiscovery((discovery_for(Asset.SILVER),)),
                quotes=quotes,
                coinbase_factory=OneTickStream,
                now=lambda: NOW,
            )
            with pytest.raises(KalshiPublicApiError, match="bounded Silver"):
                await asyncio.wait_for(recorder.run(), 1)
            health = recorder.health()
            assert health.fatal_task == "kalshi-quotes-Silver"
            assert health.fatal_error_type == "KalshiPublicApiError"

    asyncio.run(scenario())
    payload = json.loads(health_path.read_text(encoding="utf-8"))
    assert payload["fatal_task"] == "kalshi-quotes-Silver"
    assert payload["fatal_error_type"] == "KalshiPublicApiError"


def test_settlement_followup_survives_rollover_and_restart(tmp_path) -> None:
    path = tmp_path / "native.sqlite3"
    open_market = provider().parse_market(Asset.BTC, raw_market(), NOW)
    closed = provider().parse_market(
        Asset.BTC, raw_market(status="closed"), NOW + timedelta(minutes=16)
    )
    finalized = provider().parse_market(
        Asset.BTC, raw_market(status="finalized", result="no"), NOW + timedelta(minutes=17)
    )
    fake = FakeDiscovery(())
    fake.followups[closed.ticker] = finalized

    with RecorderStore(path) as store:
        store.append_kalshi_market(open_market)
        store.append_kalshi_market(closed)
        assert store.unsettled_kalshi_count(now=NOW + timedelta(minutes=16)) == 1

    async def scenario() -> None:
        with RecorderStore(path) as store:
            recorder = KalshiNativeRecorder(
                Settings(
                    products=("BTC-USD",),
                    settlement_followup_interval_seconds=60,
                    recorder_health_path=tmp_path / "health.json",
                ),
                store,
                discovery=fake,
                quotes=FakeQuotes(),
                coinbase_factory=OneTickStream,
                now=lambda: NOW + timedelta(minutes=17),
            )
            task = asyncio.create_task(recorder._record_settlements_asset(Asset.BTC))
            for _ in range(100):
                if store.count("kalshi_settlements") == 1:
                    break
                await asyncio.sleep(0.001)
            recorder.request_stop()
            await asyncio.wait_for(task, 1)
            assert store.unsettled_kalshi_count(now=NOW + timedelta(minutes=17)) == 0
            assert recorder.health().last_finalized_settlement[Asset.BTC].endswith(":no")

    asyncio.run(scenario())

    with RecorderStore(path) as recovered:
        assert recovered.count("kalshi_settlements") == 1
        assert recovered.unsettled_kalshi_markets(now=NOW + timedelta(minutes=18)) == ()


@pytest.mark.parametrize("missing_side", ("terminal_lifecycle", "settlement_truth"))
def test_crash_window_remains_recoverable_until_terminal_and_truth_exist(
    tmp_path, missing_side
) -> None:
    finalized = provider().parse_market(
        Asset.BTC, raw_market(status="finalized", result="yes"), NOW + timedelta(minutes=16)
    )
    assert finalized.settlement is not None
    with RecorderStore(tmp_path / f"{missing_side}.sqlite3") as store:
        if missing_side == "terminal_lifecycle":
            store.append_kalshi_market(
                provider().parse_market(
                    Asset.BTC, raw_market(status="closed"), NOW + timedelta(minutes=16)
                )
            )
            store.append_kalshi_settlement(finalized.settlement)
        else:
            store.append_kalshi_market(replace(finalized, settlement=None))

        pending = store.unsettled_kalshi_markets(now=NOW + timedelta(minutes=17))

    assert [item.ticker for item in pending] == [finalized.ticker]


def test_periodic_dataset_failure_does_not_terminate_scheduler(monkeypatch) -> None:
    called = threading.Event()
    calls = 0

    def failing_build(_settings):
        nonlocal calls
        calls += 1
        if calls >= 2:
            called.set()
        raise RuntimeError("derived feature store unavailable")

    monkeypatch.setattr(cli, "_build_dataset", failing_build)

    async def scenario() -> None:
        task = asyncio.create_task(
            cli._periodic_dataset_build(Settings(dataset_build_interval_seconds=0.01))
        )
        assert await asyncio.to_thread(called.wait, 1)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


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
