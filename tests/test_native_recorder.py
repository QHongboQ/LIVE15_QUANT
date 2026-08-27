from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import requests

import live15_quant.cli as cli
from live15_quant.config import Settings
from live15_quant.gaps import DataGap, GapReason, GapSource, effective_data_gaps
from live15_quant.kalshi_lifecycle import (
    KalshiDiscovery,
    KalshiLifecycle,
)
from live15_quant.kalshi_ws import KalshiOrderBookSnapshot, KalshiWsPayloadIssue
from live15_quant.models import (
    Asset,
    FreshnessState,
    MarketTick,
    RecorderEventType,
    UnderlyingObservation,
    UnderlyingProvider,
)
from live15_quant.native_recorder import (
    KalshiNativeRecorder,
    PythWorkerUnhealthyError,
    _aggregate_current_health,
)
from live15_quant.providers.kalshi import KalshiPublicApiError, KalshiTargetUnavailableError
from live15_quant.providers.pyth import (
    PythFeedIssue,
    PythNetworkError,
    PythRateLimitError,
    PythUpdateBatch,
)
from live15_quant.storage import MarketIdentityConflictError, RecorderStore
from live15_quant.ws_retention import StorageGrowthMetrics, StorageTierMetrics, WsRetentionError
from tests.test_kalshi_lifecycle import NOW, provider, quote, raw_market
from tests.test_storage import prediction_quote


class FakeDiscovery:
    def __init__(self, discoveries: tuple[KalshiDiscovery, ...]) -> None:
        self.discoveries = {item.asset: item for item in discoveries}
        self.followups = {
            market.ticker: market for item in discoveries for market in item.valid_markets
        }
        self.calls = 0
        self.discover_called = threading.Event()

    def discover(self, asset, now=None):
        del now
        self.calls += 1
        self.discover_called.set()
        return self.discoveries.get(asset, KalshiDiscovery(asset, NOW, None, None, None, (), ()))

    def get_market(self, asset, ticker, *, historical=False):
        del asset, historical
        return self.followups[ticker]


class FakeQuotes:
    def __init__(self) -> None:
        self.calls = 0
        self.quote_called = threading.Event()

    def quote_native(self, market):
        self.calls += 1
        self.quote_called.set()
        assert market.lifecycle is KalshiLifecycle.OPEN
        return quote(market.ticker, market.event_ticker, NOW)


class OneTickStream:
    def __init__(self, emitted: threading.Event | None = None) -> None:
        self.emitted = emitted

    async def ticks(self):
        yield MarketTick(
            symbol="BTC-USD",
            price=Decimal("69541.123456789"),
            bid=Decimal("69541.12"),
            ask=Decimal("69541.13"),
            received_at=NOW,
            exchange_time=NOW - timedelta(milliseconds=1),
        )
        if self.emitted is not None:
            self.emitted.set()
        await asyncio.sleep(60)


class BufferedCoinbaseStream:
    """A receive backlog that does not naturally suspend between ticks."""

    def __init__(self) -> None:
        self.drained = asyncio.Event()

    async def ticks(self):
        for offset in range(100):
            observed = NOW + timedelta(microseconds=offset)
            yield MarketTick(
                symbol="BTC-USD",
                price=Decimal("69541.123456789"),
                bid=Decimal("69541.12"),
                ask=Decimal("69541.13"),
                received_at=observed,
                exchange_time=observed - timedelta(milliseconds=1),
            )
        self.drained.set()
        await asyncio.Event().wait()


class FailingRobinhood:
    def __init__(self) -> None:
        self.calls = 0
        self.discover_called = threading.Event()

    def discover(self):
        self.calls += 1
        self.discover_called.set()
        raise RuntimeError("optional reference unavailable")


class AssetIsolatedUnderlying:
    def __init__(self) -> None:
        self.closed = False
        self.stream_calls = 0
        self.first_stream_closed = threading.Event()
        self.second_stream_attempted = threading.Event()

    @staticmethod
    def batch() -> PythUpdateBatch:
        observations = tuple(
            UnderlyingObservation(
                asset=asset,
                provider=UnderlyingProvider.PYTH_HERMES,
                symbol=f"test:{asset.value}",
                feed_id=asset.name.lower(),
                price=Decimal("100"),
                source_timestamp=NOW,
                received_timestamp=NOW,
                confidence=None,
                provenance="official-test",
                freshness=FreshnessState.FRESH,
            )
            for asset in (Asset.GOLD, Asset.WTI_OIL, Asset.HYPE, Asset.BNB)
        )
        return PythUpdateBatch(
            observations,
            (PythFeedIssue("malformed_price", Asset.SILVER, "silver"),),
        )

    def stream_batches(self):
        self.stream_calls += 1
        if self.stream_calls >= 2:
            self.second_stream_attempted.set()
        yield self.batch()
        # Hermes intentionally closes long-lived SSE connections (documented at 24h).
        self.first_stream_closed.set()
        return

    def latest_batch(self):
        return self.batch()

    def close(self):
        self.closed = True


class RateLimitedUnderlying:
    def __init__(self) -> None:
        self.latest_calls = 0
        self.rate_limited = threading.Event()

    def stream_batches(self):
        if False:
            yield None
        self.rate_limited.set()
        raise PythRateLimitError(0.05)

    def latest_batch(self):
        self.latest_calls += 1
        return PythUpdateBatch(())

    def close(self):
        return None


class BuggyUnderlying:
    def stream_batches(self):
        if False:
            yield None
        raise RuntimeError("programming defect")

    def latest_batch(self):
        raise AssertionError("correctness failures must not reach REST fallback")

    def close(self):
        return None


class PersistentlyFailingUnderlying:
    def stream_batches(self):
        if False:
            yield None
        raise PythNetworkError("simulated Pyth stream failure")

    def latest_batch(self):
        raise PythNetworkError("simulated Pyth REST failure")

    def close(self):
        return None


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


async def wait_for_thread_event(event: threading.Event, description: str) -> None:
    """Wait for an explicit cross-thread state transition, never elapsed-time luck."""

    assert await asyncio.to_thread(event.wait, 1), f"timed out waiting for {description}"


def test_robinhood_disabled_core_records_lifecycle_and_coinbase(tmp_path) -> None:
    reference = FailingRobinhood()
    fake_discovery = FakeDiscovery((discovery(),))
    quotes = FakeQuotes()
    tick_emitted = threading.Event()

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
                coinbase_factory=lambda: OneTickStream(tick_emitted),
                robinhood_reference=reference,
                now=lambda: NOW,
            )
            task = asyncio.create_task(recorder.run())
            await wait_for_thread_event(quotes.quote_called, "Kalshi quote")
            await wait_for_thread_event(tick_emitted, "Coinbase tick")
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


@pytest.mark.asyncio
async def test_buffered_coinbase_yields_to_other_recorder_workers(tmp_path) -> None:
    """A backlogged exchange socket must not monopolize the shared event loop."""

    with RecorderStore(tmp_path / "coinbase-fairness.sqlite3") as store:
        source = BufferedCoinbaseStream()
        recorder = KalshiNativeRecorder(
            Settings(products=("BTC-USD",), recorder_health_path=tmp_path / "health.json"),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=lambda: source,
            now=lambda: NOW,
        )
        task = asyncio.create_task(recorder._record_coinbase())
        await asyncio.sleep(0)
        assert store.count("coinbase_ticks") == 1
        await asyncio.wait_for(source.drained.wait(), 1)
        assert store.count("coinbase_ticks") == 100
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_robinhood_reference_failure_never_blocks_kalshi_core(tmp_path) -> None:
    reference = FailingRobinhood()
    fake_discovery = FakeDiscovery((discovery(),))
    quotes = FakeQuotes()
    tick_emitted = threading.Event()
    reference_failure_recorded = threading.Event()

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
                discovery=fake_discovery,
                quotes=quotes,
                coinbase_factory=lambda: OneTickStream(tick_emitted),
                robinhood_reference=reference,
                now=lambda: NOW,
            )
            record_source_failure = recorder._source_failed

            def observe_source_failure(key: str, error: BaseException) -> None:
                record_source_failure(key, error)
                if key == "robinhood_reference":
                    reference_failure_recorded.set()

            recorder._source_failed = observe_source_failure  # type: ignore[method-assign]
            task = asyncio.create_task(recorder.run())
            await wait_for_thread_event(reference.discover_called, "Robinhood reference call")
            await wait_for_thread_event(
                reference_failure_recorded, "Robinhood reference failure recording"
            )
            await wait_for_thread_event(quotes.quote_called, "Kalshi quote after reference failure")
            await wait_for_thread_event(tick_emitted, "Coinbase tick after reference failure")
            recorder.request_stop()
            await asyncio.wait_for(task, 1)
            assert store.count("kalshi_market_lifecycle") >= 1
            assert recorder.health().robinhood_reference_healthy is False

    asyncio.run(scenario())
    assert reference.calls >= 1


def test_one_pyth_asset_outage_does_not_stop_other_underlying_assets(tmp_path) -> None:
    source = AssetIsolatedUnderlying()

    async def scenario() -> None:
        with RecorderStore(tmp_path / "native.sqlite3") as store:
            recorder = KalshiNativeRecorder(
                Settings(
                    products=("BTC-USD",),
                    enable_pyth_underlying=True,
                    pyth_rest_fallback_interval_seconds=0.01,
                    recorder_health_interval_seconds=1,
                    recorder_health_path=tmp_path / "health.json",
                ),
                store,
                discovery=FakeDiscovery(()),
                quotes=FakeQuotes(),
                coinbase_factory=OneTickStream,
                underlying_factory=lambda: source,
                now=lambda: NOW,
            )
            task = asyncio.create_task(recorder.run())
            await wait_for_thread_event(source.first_stream_closed, "first Pyth stream closure")
            await wait_for_thread_event(
                source.second_stream_attempted, "second Pyth stream attempt"
            )
            recorder.request_stop()
            await asyncio.wait_for(task, 1)
            assert store.count("underlying_observations") == 4
            assert recorder.health().source_failures["pyth:Silver"] == "PythPayloadError"
            assert recorder.health().fatal_task is None
            assert source.stream_calls >= 2

    asyncio.run(scenario())
    assert source.closed is True


def test_pyth_429_honors_retry_after_without_immediate_rest_fallback(tmp_path) -> None:
    source = RateLimitedUnderlying()

    async def scenario() -> None:
        with RecorderStore(tmp_path / "native.sqlite3") as store:
            recorder = KalshiNativeRecorder(
                Settings(
                    products=("BTC-USD",),
                    enable_pyth_underlying=True,
                    recorder_health_path=tmp_path / "health.json",
                ),
                store,
                discovery=FakeDiscovery(()),
                quotes=FakeQuotes(),
                coinbase_factory=OneTickStream,
                underlying_factory=lambda: source,
                now=lambda: NOW,
            )
            task = asyncio.create_task(recorder.run())
            await wait_for_thread_event(source.rate_limited, "Pyth rate-limit response")
            recorder.request_stop()
            await asyncio.wait_for(task, 1)

    asyncio.run(scenario())
    assert source.latest_calls == 0


def test_pyth_programming_error_fails_recorder_loudly(tmp_path) -> None:
    async def scenario() -> None:
        with RecorderStore(tmp_path / "native.sqlite3") as store:
            recorder = KalshiNativeRecorder(
                Settings(
                    products=("BTC-USD",),
                    enable_pyth_underlying=True,
                    recorder_health_path=tmp_path / "health.json",
                ),
                store,
                discovery=FakeDiscovery(()),
                quotes=FakeQuotes(),
                coinbase_factory=OneTickStream,
                underlying_factory=BuggyUnderlying,
                now=lambda: NOW,
            )
            with pytest.raises(RuntimeError, match="programming defect"):
                await asyncio.wait_for(recorder.run(), 1)
            assert recorder.health().fatal_task == "pyth-predictive"
            assert recorder.health().fatal_error_type == "RuntimeError"

    asyncio.run(scenario())


def test_pyth_prolonged_failure_escalates_after_bounded_recovery(tmp_path) -> None:
    async def scenario() -> None:
        with RecorderStore(tmp_path / "native.sqlite3") as store:
            recorder = KalshiNativeRecorder(
                Settings(
                    products=("BTC-USD",),
                    enable_pyth_underlying=True,
                    pyth_rest_fallback_interval_seconds=0.001,
                    pyth_recovery_critical_timeout_seconds=0.001,
                    pyth_recovery_max_attempts=2,
                    recorder_health_path=tmp_path / "health.json",
                ),
                store,
                discovery=FakeDiscovery(()),
                quotes=FakeQuotes(),
                coinbase_factory=OneTickStream,
                underlying_factory=PersistentlyFailingUnderlying,
                now=lambda: NOW,
            )
            with pytest.raises(PythWorkerUnhealthyError, match="exhausted bounded recovery"):
                await asyncio.wait_for(recorder._record_pyth(), 1)
            worker = recorder.health().worker_health["pyth"]
            assert worker["current_state"] == "UNHEALTHY"
            assert worker["last_successful_observation_timestamp"] is None
            assert worker["consecutive_failures"] >= 2

    asyncio.run(scenario())


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
            assert health.integrity == "not_checked"

    asyncio.run(scenario())
    payload = json.loads(health_path.read_text(encoding="utf-8"))
    assert payload["status"] == "degraded"
    assert payload["current_markets"]["BTC"].startswith("KXBTC15M-")


def test_historical_retry_counter_does_not_degrade_current_health() -> None:
    status, issues = _aggregate_current_health(
        integrity="ok",
        source_failures={},
        stale_sources=(),
        stale_workers=(),
    )

    assert status == "healthy"
    assert issues == []


def test_current_reconnect_or_stale_worker_degrades_health() -> None:
    status, issues = _aggregate_current_health(
        integrity="ok",
        source_failures={},
        stale_sources=("kalshi_ws:BTC",),
        stale_workers=("kalshi_ws",),
    )

    assert status == "degraded"
    assert issues == ["stale_source:kalshi_ws:BTC", "stale_worker:kalshi_ws"]


def test_managed_restart_can_reuse_verified_health_without_full_startup_scans(
    tmp_path, monkeypatch
) -> None:
    with RecorderStore(tmp_path / "native.sqlite3") as store:
        monkeypatch.setattr(
            store,
            "row_counts",
            lambda: (_ for _ in ()).throw(AssertionError("must not scan row counts")),
        )
        monkeypatch.setattr(
            store,
            "quick_check",
            lambda: (_ for _ in ()).throw(AssertionError("must not scan database")),
        )
        recorder = KalshiNativeRecorder(
            Settings(products=("BTC-USD",), recorder_health_path=tmp_path / "health.json"),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            initial_row_counts={
                "kalshi_market_lifecycle": 1_000_000_000_001,
                "kalshi_prediction_quotes": 1_000_000_000_002,
                "coinbase_ticks": 1_000_000_000_123,
                "underlying_observations": 1_000_000_000_004,
                "secondary_underlying_observations": 1_000_000_000_005,
                "kalshi_ws_orderbook_events": 1_000_000_000_006,
                "kalshi_ws_book_checkpoints": 1_000_000_000_007,
                "kalshi_settlements": 1_000_000_000_008,
                "kalshi_settlement_conflicts": 0,
                "data_gaps": 1_000_000_000_009,
            },
            initial_row_counts_complete=True,
            last_verified_integrity="ok",
            now=lambda: NOW,
        )

    assert recorder.health().row_counts["coinbase_ticks"] == 1_000_000_000_123
    assert recorder.health().row_counts_complete is True
    assert recorder.health().integrity == "ok"


def test_missing_health_baseline_uses_bounded_estimates_not_full_scans(
    tmp_path, monkeypatch
) -> None:
    with RecorderStore(tmp_path / "native.sqlite3") as store:
        monkeypatch.setattr(
            store,
            "row_counts",
            lambda: (_ for _ in ()).throw(AssertionError("must not scan row counts")),
        )
        monkeypatch.setattr(
            store,
            "quick_check",
            lambda: (_ for _ in ()).throw(AssertionError("must not scan database")),
        )
        recorder = KalshiNativeRecorder(
            Settings(products=("BTC-USD",), recorder_health_path=tmp_path / "health.json"),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
        )

    assert recorder.health().row_counts_complete is False
    assert recorder.health().integrity == "not_checked"


def test_normal_startup_sql_contains_no_full_size_dependent_scans(tmp_path) -> None:
    with RecorderStore(tmp_path / "native.sqlite3") as store:
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)
        KalshiNativeRecorder(
            Settings(products=("BTC-USD",), recorder_health_path=tmp_path / "health.json"),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
        )

    selects = [
        " ".join(statement.upper().split())
        for statement in statements
        if statement.lstrip().upper().startswith(("SELECT", "PRAGMA"))
    ]
    assert len(selects) <= 100
    assert not any("COUNT(" in statement for statement in selects)
    assert not any("PRAGMA QUICK_CHECK" in statement for statement in selects)
    assert not any("PRAGMA INTEGRITY_CHECK" in statement for statement in selects)
    assert not any("MAX(RECEIVED_TIMESTAMP)" in statement for statement in selects)
    assert not any(
        "FROM KALSHI_MARKET_LIFECYCLE WHERE ASSET=" in statement and "WINDOW_END>=" not in statement
        for statement in selects
    )


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


@pytest.mark.parametrize("result", ("yes", "no"))
def test_pending_ignores_stale_closed_then_accepts_finalized(tmp_path, result) -> None:
    path = tmp_path / "regression.sqlite3"
    pending = provider().parse_market(
        Asset.BTC,
        raw_market(status="determined", result=result),
        NOW + timedelta(minutes=16),
    )
    stale = provider().parse_market(
        Asset.BTC, raw_market(status="closed"), NOW + timedelta(minutes=16, seconds=1)
    )
    finalized = provider().parse_market(
        Asset.BTC,
        raw_market(status="finalized", result=result),
        NOW + timedelta(minutes=17),
    )
    with RecorderStore(path) as store:
        recorder = KalshiNativeRecorder(
            Settings(products=("BTC-USD",), recorder_health_path=tmp_path / "health.json"),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW + timedelta(minutes=17),
        )
        recorder._accept_market(pending)
        recorder._accept_market(stale)
        assert store.latest_kalshi_state(pending.ticker).lifecycle is (
            KalshiLifecycle.SETTLEMENT_PENDING
        )
        events = store.replay_recorder_events()
        assert [event.event_type.value for event in events] == ["lifecycle_regression"]
        recorder._accept_market(finalized)
        expected = KalshiLifecycle.SETTLED_YES if result == "yes" else KalshiLifecycle.SETTLED_NO
        assert store.latest_kalshi_state(pending.ticker).lifecycle is expected
        assert store.count("kalshi_settlements") == 1

    with RecorderStore(path) as recovered:
        assert recovered.latest_kalshi_state(pending.ticker).lifecycle is expected
        assert len(recovered.replay_recorder_events()) == 1


@pytest.mark.parametrize("stale_status", ("closed", "determined"))
def test_settled_truth_ignores_stale_nonterminal_observation(tmp_path, stale_status) -> None:
    finalized = provider().parse_market(
        Asset.BTC,
        raw_market(status="finalized", result="yes"),
        NOW + timedelta(minutes=17),
    )
    stale = provider().parse_market(
        Asset.BTC,
        raw_market(status=stale_status, result="yes" if stale_status == "determined" else None),
        NOW + timedelta(minutes=17, seconds=1),
    )
    with RecorderStore(tmp_path / "settled.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(products=("BTC-USD",), recorder_health_path=tmp_path / "health.json"),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW + timedelta(minutes=18),
        )
        recorder._accept_market(finalized)
        recorder._accept_market(stale)
        assert store.latest_kalshi_state(finalized.ticker).lifecycle is KalshiLifecycle.SETTLED_YES
        assert next(iter(store.replay_kalshi_settlements())).result.value == "yes"
        assert len(store.replay_recorder_events()) == 1


def test_settled_truth_rejects_conflicting_stale_determination(tmp_path) -> None:
    finalized = provider().parse_market(
        Asset.BTC,
        raw_market(status="finalized", result="yes"),
        NOW + timedelta(minutes=17),
    )
    conflicting = provider().parse_market(
        Asset.BTC,
        raw_market(status="determined", result="no"),
        NOW + timedelta(minutes=18),
    )
    with RecorderStore(tmp_path / "result-conflict.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(products=("BTC-USD",), recorder_health_path=tmp_path / "health.json"),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW + timedelta(minutes=18),
        )
        recorder._accept_market(finalized)
        with pytest.raises(KalshiPublicApiError, match="conflicting official result"):
            recorder._accept_market(conflicting)
        assert store.latest_kalshi_state(finalized.ticker).lifecycle is (
            KalshiLifecycle.SETTLED_YES
        )
        event = store.replay_recorder_events()[0]
        assert event.event_type is RecorderEventType.SETTLEMENT_CONFLICT


def test_stale_regression_identity_conflict_remains_fatal(tmp_path) -> None:
    pending = provider().parse_market(
        Asset.BTC,
        raw_market(status="determined", result="yes"),
        NOW + timedelta(minutes=16),
    )
    stale = provider().parse_market(
        Asset.BTC, raw_market(status="closed"), NOW + timedelta(minutes=16, seconds=1)
    )
    with RecorderStore(tmp_path / "identity.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(products=("BTC-USD",), recorder_health_path=tmp_path / "health.json"),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW + timedelta(minutes=17),
        )
        recorder._accept_market(pending)
        with pytest.raises(KalshiPublicApiError, match="identity"):
            recorder._accept_market(replace(stale, target=stale.target + Decimal("1")))


def test_typed_market_rejects_series_or_event_ticker_mismatch() -> None:
    stale = provider().parse_market(
        Asset.BTC, raw_market(status="closed"), NOW + timedelta(minutes=17)
    )
    with pytest.raises(ValueError, match="exact series"):
        replace(stale, series="KXOTHER15M")
    with pytest.raises(ValueError, match="event ticker"):
        replace(stale, event_ticker="KXBTC15M-CONFLICT")


def test_stale_regression_window_conflict_remains_fatal(tmp_path) -> None:
    pending = provider().parse_market(
        Asset.BTC,
        raw_market(status="determined", result="yes"),
        NOW + timedelta(minutes=16),
    )
    stale = provider().parse_market(
        Asset.BTC, raw_market(status="closed"), NOW + timedelta(minutes=17)
    )
    conflicting = replace(
        stale,
        window_start=stale.window_start + timedelta(minutes=15),
        window_end=stale.window_end + timedelta(minutes=15),
    )
    with RecorderStore(tmp_path / "identity-window.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(products=("BTC-USD",), recorder_health_path=tmp_path / "health.json"),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW + timedelta(minutes=17),
        )
        recorder._accept_market(pending)
        with pytest.raises(KalshiPublicApiError, match="identity"):
            recorder._accept_market(conflicting)


def test_same_window_under_conflicting_ticker_fails_loudly(tmp_path) -> None:
    pending = provider().parse_market(
        Asset.BTC,
        raw_market(status="determined", result="yes"),
        NOW + timedelta(minutes=16),
    )
    conflicting = replace(pending, ticker=f"{pending.ticker}-CONFLICT")
    with RecorderStore(tmp_path / "ticker-conflict.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(products=("BTC-USD",), recorder_health_path=tmp_path / "health.json"),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW + timedelta(minutes=17),
        )
        recorder._accept_market(pending)
        with pytest.raises(MarketIdentityConflictError, match="ticker"):
            recorder._accept_market(conflicting)


def test_stale_settlement_regression_isolated_while_other_asset_finalizes(tmp_path) -> None:
    btc_pending = provider().parse_market(
        Asset.BTC,
        raw_market(status="determined", result="yes"),
        NOW + timedelta(minutes=16),
    )
    btc_stale = provider().parse_market(
        Asset.BTC, raw_market(status="closed"), NOW + timedelta(minutes=17)
    )
    eth_closed = provider().parse_market(
        Asset.ETH,
        raw_market(Asset.ETH, status="closed"),
        NOW + timedelta(minutes=16),
    )
    eth_final = provider().parse_market(
        Asset.ETH,
        raw_market(Asset.ETH, status="finalized", result="no"),
        NOW + timedelta(minutes=17),
    )
    followup = FakeDiscovery(())
    followup.followups = {btc_pending.ticker: btc_stale, eth_closed.ticker: eth_final}

    async def scenario() -> None:
        with RecorderStore(tmp_path / "isolation.sqlite3") as store:
            store.append_kalshi_market(btc_pending)
            store.append_kalshi_market(eth_closed)
            recorder = KalshiNativeRecorder(
                Settings(
                    products=("BTC-USD",),
                    settlement_followup_interval_seconds=60,
                    recorder_health_path=tmp_path / "health.json",
                ),
                store,
                discovery=followup,
                quotes=FakeQuotes(),
                coinbase_factory=OneTickStream,
                now=lambda: NOW + timedelta(minutes=17),
            )
            tasks = [
                asyncio.create_task(recorder._record_settlements_asset(asset))
                for asset in (Asset.BTC, Asset.ETH)
            ]
            for _ in range(100):
                if store.count("kalshi_settlements") == 1:
                    break
                await asyncio.sleep(0.001)
            recorder.request_stop()
            await asyncio.gather(*tasks)
            assert store.latest_kalshi_state(btc_pending.ticker).lifecycle is (
                KalshiLifecycle.SETTLEMENT_PENDING
            )
            assert store.latest_kalshi_state(eth_closed.ticker).lifecycle is (
                KalshiLifecycle.SETTLED_NO
            )
            assert len(store.replay_recorder_events()) == 1

    asyncio.run(scenario())


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


@pytest.mark.asyncio
async def test_health_reports_worker_progress_and_event_loop_lag(tmp_path, monkeypatch) -> None:
    monotonic_values = iter((0.0, 0.025))
    with RecorderStore(tmp_path / "worker-health.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                products=("BTC-USD",),
                recorder_health_interval_seconds=0.01,
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
            monotonic=lambda: next(monotonic_values),
        )
        recorder._worker_advanced("coinbase", NOW)
        waits = iter((False, True))

        async def controlled_wait(_seconds: float) -> bool:
            return next(waits)

        monkeypatch.setattr(recorder, "_wait", controlled_wait)
        await recorder._report_health()
        payload = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
        assert payload["worker_progress"]["coinbase"] == NOW.isoformat()
        assert payload["event_loop_lag_seconds"] == pytest.approx(0.015)
        assert "kalshi_discovery:BTC" in payload["stale_workers"]


def test_realtime_gap_opens_once_and_closes_append_only_after_recovery(tmp_path) -> None:
    path = tmp_path / "live-gap.sqlite3"
    previous = NOW - timedelta(seconds=40)
    settings = Settings(
        products=("BTC-USD",),
        recorder_coinbase_stale_seconds=15,
        recorder_health_path=tmp_path / "health.json",
    )
    with RecorderStore(path) as store:
        store.append_coinbase(
            MarketTick(
                symbol="BTC-USD",
                price=Decimal("1"),
                bid=Decimal("0.9"),
                ask=Decimal("1.1"),
                received_at=previous,
                exchange_time=previous,
            )
        )
        recorder = KalshiNativeRecorder(
            settings,
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
        )
        recorder._open_due_gaps(NOW)
        recorder._open_due_gaps(NOW + timedelta(seconds=1))
        active = store.active_data_gaps()
        assert len(active) == 1
        assert active[0].reason is GapReason.RESTART
        assert store.count("data_gaps") == 1

        recorder._observe_gap(
            GapSource.COINBASE,
            Asset.BTC,
            NOW + timedelta(seconds=1),
            source_health_key="coinbase",
        )
        assert store.active_data_gaps() == ()
        facts = store.replay_data_gaps()
        assert len(facts) == 2
        assert not facts[0].recovered
        assert facts[1].recovered
        assert facts[1].gap_start == facts[0].gap_start
        assert facts[1].reason is facts[0].reason


def test_restart_recovers_persisted_active_gap_without_duplicate_open(tmp_path) -> None:
    path = tmp_path / "restart-gap.sqlite3"
    previous = NOW - timedelta(seconds=40)
    settings = Settings(
        products=("BTC-USD",),
        recorder_coinbase_stale_seconds=15,
        recorder_health_path=tmp_path / "health.json",
    )
    with RecorderStore(path) as store:
        store.append_coinbase(
            MarketTick(
                symbol="BTC-USD",
                price=Decimal("1"),
                bid=Decimal("0.9"),
                ask=Decimal("1.1"),
                received_at=previous,
                exchange_time=previous,
            )
        )
        first = KalshiNativeRecorder(
            settings,
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
        )
        first._open_due_gaps(NOW)

    with RecorderStore(path) as recovered:
        restarted = KalshiNativeRecorder(
            settings,
            recovered,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW + timedelta(seconds=5),
        )
        restarted._open_due_gaps(NOW + timedelta(seconds=5))
        assert recovered.count("data_gaps") == 1
        assert len(recovered.active_data_gaps()) == 1


def test_restart_recovers_all_distinct_active_gap_facts_after_real_observation(tmp_path) -> None:
    """Interrupted recovery facts stay append-only and do not block the recorder."""

    path = tmp_path / "multiple-active-gaps.sqlite3"
    settings = Settings(
        products=("BTC-USD",),
        recorder_coinbase_stale_seconds=15,
        recorder_health_path=tmp_path / "health.json",
    )
    first = DataGap(
        source=GapSource.COINBASE,
        asset=Asset.BTC,
        instrument="BTC-USD",
        gap_start=NOW - timedelta(seconds=90),
        gap_end=None,
        detected_at=NOW - timedelta(seconds=75),
        threshold_seconds=Decimal("15"),
        reason=GapReason.RESTART,
        recovered=False,
        recorder_session_id="first-session",
    )
    second = DataGap(
        source=GapSource.COINBASE,
        asset=Asset.BTC,
        instrument="BTC-USD",
        gap_start=NOW - timedelta(seconds=45),
        gap_end=None,
        detected_at=NOW - timedelta(seconds=30),
        threshold_seconds=Decimal("15"),
        reason=GapReason.SOURCE_OUTAGE,
        recovered=False,
        recorder_session_id="second-session",
    )
    with RecorderStore(path) as store:
        store.append_data_gaps((first, second))
        recorder = KalshiNativeRecorder(
            settings,
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
        )
        assert len(store.active_data_gaps()) == 2

        recorder._observe_gap(
            GapSource.COINBASE,
            Asset.BTC,
            NOW,
            source_health_key="coinbase",
        )

        assert store.active_data_gaps() == ()
        facts = effective_data_gaps(store.replay_data_gaps())
        assert [(fact.gap_start, fact.recovered) for fact in facts] == [
            (first.gap_start, True),
            (second.gap_start, True),
        ]


@pytest.mark.asyncio
async def test_periodic_checkpoint_never_runs_full_database_quick_check(
    tmp_path, monkeypatch
) -> None:
    """Live maintenance is bounded; structural scans belong to startup/snapshots."""

    with RecorderStore(tmp_path / "bounded-checkpoint.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                products=("BTC-USD",),
                recorder_checkpoint_interval_seconds=60,
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
        )
        checkpoint_called = asyncio.Event()
        original_checkpoint = store.checkpoint

        def bounded_checkpoint() -> tuple[int, int, int]:
            result = original_checkpoint()
            checkpoint_called.set()
            recorder.request_stop()
            return result

        monkeypatch.setattr(store, "checkpoint", bounded_checkpoint)

        def forbidden_scan() -> str:
            raise AssertionError("periodic maintenance attempted a full database scan")

        monkeypatch.setattr(store, "quick_check", forbidden_scan)
        await asyncio.wait_for(recorder._checkpoint(), 1)
        assert checkpoint_called.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [WsRetentionError("bad archive"), OSError("archive offline")])
async def test_archive_failure_isolated_from_core_recorder(
    tmp_path, monkeypatch, failure: Exception
) -> None:
    with RecorderStore(tmp_path / "archive-isolation.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                products=("BTC-USD",),
                recorder_health_path=tmp_path / "health.json",
                ws_archive_poll_interval_seconds=60,
            ),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
        )
        assert recorder._archive_service is not None

        def fail_archive(*_args: object, **_kwargs: object) -> None:
            raise failure

        monkeypatch.setattr(recorder._archive_service, "run_once", fail_archive)

        async def stop_after_failure(_seconds: float) -> bool:
            return True

        monkeypatch.setattr(recorder, "_wait", stop_after_failure)
        await asyncio.wait_for(recorder._archive_ws_retention(), 1)
        health = recorder.health()
        assert health.source_failures["ws_archive"] == type(failure).__name__
        assert health.fatal_task is None
        assert health.fatal_error_type is None


@pytest.mark.asyncio
async def test_adaptive_retention_fail_safe_requests_managed_pause(tmp_path, monkeypatch) -> None:
    pause_reasons: list[str] = []
    with RecorderStore(tmp_path / "adaptive-fail-safe.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                products=("BTC-USD",),
                recorder_health_path=tmp_path / "health.json",
                ws_archive_poll_interval_seconds=60,
            ),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            controlled_pause=pause_reasons.append,
            now=lambda: NOW,
        )
        assert recorder._archive_service is not None
        manifest = recorder._archive_service.manifest
        monkeypatch.setattr(
            recorder._archive_service,
            "run_once",
            lambda **_kwargs: SimpleNamespace(
                backlog_events=0,
                events_per_second=0.0,
                elapsed_seconds=0.0,
            ),
        )
        monkeypatch.setattr(
            recorder._archive_service,
            "hot_metrics",
            lambda _now: {},
        )
        monkeypatch.setattr(
            manifest,
            "metrics",
            lambda: {"verified": 0, "failed": 1, "uncompressed": 0, "compressed": 0},
        )
        storage = StorageTierMetrics(10_000, 0, 10_000, 0, 0, None, None, None, None)
        monkeypatch.setattr(manifest, "storage_metrics", lambda _path: storage)
        monkeypatch.setattr(
            manifest,
            "record_storage_sample",
            lambda *_args, **_kwargs: StorageGrowthMetrics(None, None, None),
        )
        monkeypatch.setattr(manifest, "latest", lambda: None)
        monkeypatch.setattr(
            "live15_quant.native_recorder.DiskQuota",
            lambda: SimpleNamespace(classify=lambda **_kwargs: SimpleNamespace(value="fail_safe")),
        )

        await asyncio.wait_for(recorder._archive_ws_retention(), 1)

        assert pause_reasons
        assert "controlled pause" in pause_reasons[0]
        assert recorder._stop_event.is_set()


@pytest.mark.asyncio
async def test_archive_retention_defers_to_live_ws_backpressure(tmp_path, monkeypatch) -> None:
    with RecorderStore(tmp_path / "archive-backpressure.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                products=("BTC-USD",),
                recorder_health_path=tmp_path / "health.json",
                ws_archive_poll_interval_seconds=60,
            ),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
        )
        assert recorder._archive_service is not None
        recorder._kalshi_ws = SimpleNamespace(
            diagnostics=SimpleNamespace(
                receive_queue_capacity=8192,
                receive_queue_depth=2048,
            )
        )

        def forbidden_archive(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("archive ran while the live WS queue was under pressure")

        monkeypatch.setattr(recorder._archive_service, "run_once", forbidden_archive)

        async def stop_after_defer(_seconds: float) -> bool:
            return True

        monkeypatch.setattr(recorder, "_wait", stop_after_defer)
        await asyncio.wait_for(recorder._archive_ws_retention(), 1)

        health = recorder.health()
        assert health.worker_progress["ws_archive"] == NOW
        assert health.ws_archive_metrics["deferred_for_ws_backpressure"] is True
        assert health.source_failures == {}


@pytest.mark.asyncio
async def test_archive_chunk_work_yields_event_loop_to_control_and_health(
    tmp_path, monkeypatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    with RecorderStore(tmp_path / "archive-fairness.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                products=("BTC-USD",),
                recorder_health_path=tmp_path / "health.json",
                ws_archive_poll_interval_seconds=60,
            ),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
        )
        assert recorder._archive_service is not None

        def bounded_archive_burst(*_args: object, **_kwargs: object) -> None:
            entered.set()
            assert release.wait(1)
            raise WsRetentionError("deterministic archive completion")

        monkeypatch.setattr(recorder._archive_service, "run_once", bounded_archive_burst)

        async def stop_after_iteration(_seconds: float) -> bool:
            return True

        monkeypatch.setattr(recorder, "_wait", stop_after_iteration)
        archive_task = asyncio.create_task(recorder._archive_ws_retention())
        assert await asyncio.to_thread(entered.wait, 1)

        control_and_health_progressed = asyncio.Event()

        async def control_and_health_probe() -> None:
            recorder._worker_advanced("control_watcher", NOW)
            recorder._worker_advanced("health", NOW)
            control_and_health_progressed.set()

        probe = asyncio.create_task(control_and_health_probe())
        await asyncio.wait_for(control_and_health_progressed.wait(), 0.2)
        release.set()
        await asyncio.gather(archive_task, probe)

        assert recorder.health().worker_progress["control_watcher"] == NOW
        assert recorder.health().worker_progress["health"] == NOW


def test_sdk_durable_commit_advances_existing_persistence_health_key(tmp_path) -> None:
    with RecorderStore(tmp_path / "sdk-progress.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                products=("BTC-USD",),
                recorder_health_path=tmp_path / "health.json",
                enable_kalshi_production_websocket=True,
                kalshi_recorder_provider="sdk",
                enable_ws_archive=False,
            ),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
        )

        recorder._on_sdk_ws_committed(())
        assert "kalshi_ws_persistence" not in recorder._health.worker_progress

        # This callback is the consumer's post-commit notification.  A full
        # book is unnecessary here: only its successful durability boundary
        # matters to Recorder health progress.
        recorder._on_sdk_ws_committed((SimpleNamespace(authoritative=False, book=None),))
        assert recorder._health.worker_progress["kalshi_ws_persistence"] == NOW


def test_archive_retention_waits_for_current_ws_books_to_synchronize(tmp_path) -> None:
    with RecorderStore(tmp_path / "archive-ws-startup.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                products=("BTC-USD",),
                recorder_health_path=tmp_path / "health.json",
                enable_kalshi_production_websocket=True,
            ),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            kalshi_ws_factory=lambda: SimpleNamespace(
                diagnostics=SimpleNamespace(receive_queue_capacity=8192, receive_queue_depth=0)
            ),
            now=lambda: NOW,
        )
        weekend_current_assets = tuple(
            asset for asset in Asset if asset not in {Asset.GOLD, Asset.SILVER, Asset.WTI_OIL}
        )
        current_markets = {
            asset: market
            for asset in weekend_current_assets
            if (market := discovery_for(asset).current) is not None
        }
        assert len(current_markets) == 7
        recorder._health.current.update(current_markets)
        recorder._kalshi_ws = SimpleNamespace(
            diagnostics=SimpleNamespace(receive_queue_capacity=8192, receive_queue_depth=0)
        )

        assert recorder._archive_service is not None
        assert recorder._archive_service.chunk_records == 10_000
        assert recorder._ws_archive_backpressure_active() is True
        assert recorder._retention_core_healthy() is False
        for asset, market in tuple(current_markets.items())[:-1]:
            recorder._health.kalshi_ws_synchronized[asset] = market.ticker
        assert recorder._ws_archive_backpressure_active() is True
        assert recorder._retention_core_healthy() is False
        last_asset, last_market = tuple(current_markets.items())[-1]
        recorder._health.kalshi_ws_synchronized[last_asset] = last_market.ticker
        assert recorder._ws_archive_backpressure_active() is False
        assert recorder._retention_core_healthy() is True


def test_archive_poll_schedule_is_bounded_and_backlog_aware(tmp_path) -> None:
    with RecorderStore(tmp_path / "archive-cadence.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                products=("BTC-USD",),
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
        )

        assert recorder._archive_poll_schedule(backlog_events=10_000) == ("CATCH_UP", 2.0)
        assert recorder._archive_poll_schedule(backlog_events=2_500) == ("ACTIVE", 5.0)
        assert recorder._archive_poll_schedule(backlog_events=1) == ("NEAR_CAUGHT_UP", 10.0)
        assert recorder._archive_poll_schedule(
            backlog_events=0, eligibility_status="WAITING_FOR_SOURCE_DATA"
        ) == ("IDLE", 60.0)
        assert recorder._archive_poll_schedule(
            backlog_events=0,
            eligibility_status="WAITING_FOR_RETENTION_ELIGIBILITY",
            next_eligible_at=NOW + timedelta(seconds=15),
        ) == ("IDLE", 15.0)
        assert recorder._archive_poll_schedule(backlog_events=0, backpressure=True) == (
            "BACKPRESSURE",
            2.0,
        )


@pytest.mark.asyncio
async def test_malformed_ws_market_isolated_and_official_snapshot_recovers_recorder(
    tmp_path,
) -> None:
    market = discovery_for(Asset.BTC).current
    assert market is not None
    issue = KalshiWsPayloadIssue(
        connection_id="connection-recovery",
        message_type="orderbook_delta",
        channel=None,
        subscription_id=2,
        sequence=11,
        ticker=market.ticker,
        parser_stage="data_payload",
        reason="malformed Kalshi WebSocket market_id",
        schema_keys=("top:type", "msg:market_ticker"),
        payload_shape_hash="0123456789abcdef",
        affects_orderbook=True,
        socket_received_timestamp=NOW,
        parse_timestamp=NOW,
    )
    recovered = KalshiOrderBookSnapshot(
        connection_id="connection-recovery",
        subscription_id=2,
        sequence=20,
        ticker=market.ticker,
        market_id="official-market-id",
        yes_bids=(),
        no_bids=(),
        source_timestamp=NOW,
        socket_received_timestamp=NOW + timedelta(milliseconds=1),
        parse_timestamp=NOW + timedelta(milliseconds=1),
    )

    class RecoveringStream:
        def __init__(self) -> None:
            self.commands: list[str] = []
            self.diagnostics = SimpleNamespace(receive_queue_capacity=8192, receive_queue_depth=0)

        def set_reconnect_tickers(self, _tickers) -> None:
            return None

        async def send_command(self, command) -> None:
            self.commands.append(command.payload)

        async def messages(self, _tickers):
            yield issue
            yield recovered

    source = RecoveringStream()
    with RecorderStore(tmp_path / "ws-payload-recovery.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                products=("BTC-USD",),
                recorder_health_path=tmp_path / "health.json",
                enable_ws_archive=False,
            ),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
        )
        recorder._kalshi_ws = source
        recorder._health.current[Asset.BTC] = market
        await recorder._record_kalshi_ws_session()

        health = recorder.health()
        diagnostics = store.replay_recorder_events(limit=10)

    assert json.loads(source.commands[0])["params"]["action"] == "get_snapshot"
    assert health.kalshi_ws_connection_state.value == "synchronized"
    assert health.kalshi_ws_synchronized_markets == {Asset.BTC: market.ticker}
    assert health.fatal_task is None and health.fatal_error_type is None
    assert health.source_failures == {}
    assert health.retry_counts["kalshi_ws"] == 1
    assert any(event.error_type == "KalshiWsPayloadIssue" for event in diagnostics)


def test_normal_market_closure_is_not_reported_as_stale_or_source_failure(tmp_path) -> None:
    saturday = NOW.replace(day=22, hour=4)
    last_observations = {
        Asset.GOLD: NOW.replace(hour=20, minute=59, second=55),
        Asset.SILVER: NOW.replace(hour=20, minute=59, second=55),
        Asset.WTI_OIL: NOW.replace(hour=20, minute=44, second=55),
    }
    with RecorderStore(tmp_path / "market-closed.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                products=("BTC-USD",),
                enable_pyth_underlying=True,
                enable_kalshi_production_websocket=True,
                enable_ws_archive=False,
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            kalshi_ws_factory=lambda: SimpleNamespace(
                diagnostics=SimpleNamespace(receive_queue_capacity=8192, receive_queue_depth=0)
            ),
            now=lambda: saturday,
        )
        for asset in (Asset.GOLD, Asset.SILVER, Asset.WTI_OIL):
            recorder._health.last_additional_underlying[asset] = last_observations[asset]
            for source in (GapSource.PYTH, GapSource.KALSHI_REST, GapSource.KALSHI_WS):
                recorder._gap_last[(source, asset)] = last_observations[asset]
        recorder._open_due_gaps(saturday)
        health = recorder.health()
        gap_count = store.count("data_gaps")

    for asset in (Asset.GOLD, Asset.SILVER, Asset.WTI_OIL):
        assert f"pyth:{asset.value}" in health.market_closed_sources
        assert f"pyth:{asset.value}" not in health.stale_sources
        assert f"pyth:{asset.value}" not in health.source_failures
    assert gap_count == 0
    assert health.as_dict()["status"] == "degraded"  # Other configured live workers are absent.


def test_active_commodity_gap_closes_at_session_end_not_sunday_reopen(tmp_path) -> None:
    friday_before_close = (NOW + timedelta(days=1)).replace(hour=20, minute=59, second=30)
    saturday = NOW.replace(day=22, hour=4)
    with RecorderStore(tmp_path / "market-close-active-gap.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                products=("BTC-USD",),
                enable_ws_archive=False,
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: saturday,
        )
        stream = recorder._gap_streams[(GapSource.KALSHI_REST, Asset.GOLD)]
        recorder._gap_last[(GapSource.KALSHI_REST, Asset.GOLD)] = friday_before_close
        recorder._open_gap(
            stream,
            friday_before_close,
            source_health_key="kalshi_quote:Gold",
            detected_at=friday_before_close + timedelta(seconds=10),
        )

        recorder._open_due_gaps(saturday)

        assert store.active_data_gaps() == ()
        recovered = effective_data_gaps(store.replay_data_gaps())
        assert len(recovered) == 1
        assert recovered[0].recovered
        assert recovered[0].gap_end == (NOW + timedelta(days=1)).replace(
            hour=21, minute=0, second=0
        )


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
