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
from live15_quant.models import (
    Asset,
    FreshnessState,
    MarketTick,
    RecorderEventType,
    UnderlyingObservation,
    UnderlyingProvider,
)
from live15_quant.native_recorder import KalshiNativeRecorder
from live15_quant.providers.kalshi import KalshiPublicApiError, KalshiTargetUnavailableError
from live15_quant.providers.pyth import (
    PythFeedIssue,
    PythRateLimitError,
    PythUpdateBatch,
)
from live15_quant.storage import MarketIdentityConflictError, RecorderStore
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


class AssetIsolatedUnderlying:
    def __init__(self) -> None:
        self.closed = False
        self.stream_calls = 0

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
        yield self.batch()
        # Hermes intentionally closes long-lived SSE connections (documented at 24h).
        return

    def latest_batch(self):
        return self.batch()

    def close(self):
        self.closed = True


class RateLimitedUnderlying:
    def __init__(self) -> None:
        self.latest_calls = 0

    def stream_batches(self):
        if False:
            yield None
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
            await asyncio.sleep(0.05)
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
            await asyncio.sleep(0.01)
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
