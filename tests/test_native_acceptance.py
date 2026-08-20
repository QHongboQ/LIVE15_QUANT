from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta

import pytest
import requests

import live15_quant.native_acceptance as acceptance
from live15_quant.kalshi_lifecycle import KalshiDiscovery, KalshiLifecycle
from live15_quant.models import Asset
from live15_quant.providers.kalshi import KalshiTargetUnavailableError
from tests.test_kalshi_lifecycle import NOW, provider, quote, raw_market


@dataclass
class DeterministicClock:
    now: float = 100.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> DeterministicClock:
    return DeterministicClock()


def test_acceptance_continues_after_exhausted_network_retry(
    monkeypatch, clock: DeterministicClock
) -> None:
    calls: list[float] = []
    client_options: dict[str, object] = {}

    class FakeClient:
        closed = False

        def __init__(self, settings, **kwargs) -> None:
            del settings
            client_options.update(kwargs)

        def close(self) -> None:
            self.closed = True

    class FailingProvider:
        def __init__(self, client) -> None:
            del client

        def discover_all(self):
            calls.append(clock.monotonic())
            raise requests.ConnectionError("public market data unavailable")

    client = FakeClient
    provider = FailingProvider
    monkeypatch.setattr(acceptance, "KalshiOfficialQuoteProvider", client)
    monkeypatch.setattr(acceptance, "KalshiNativeMarketProvider", provider)

    result = acceptance.run_acceptance(
        max_seconds=5,
        poll_seconds=1,
        post_rollover_seconds=1,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert calls == [100.0, 101.0, 103.0]
    assert clock.sleeps == [1, 2, 2]
    assert clock.now == 105.0
    assert result["status"] == "expected_upstream_unavailable"
    assert result["reason"] == "no_current_market_with_published_target"
    assert result["network_retry_exhaustions"] == 3
    assert client_options["retry_total"] == 0
    assert client_options["deadline_monotonic"] == 105.0
    assert client_options["monotonic"] == clock.monotonic


def test_acceptance_fails_immediately_on_correctness_error(
    monkeypatch, clock: DeterministicClock
) -> None:
    calls = 0

    class FakeClient:
        def __init__(self, settings, **kwargs) -> None:
            del settings, kwargs

        def close(self) -> None:
            return None

    class InvalidProvider:
        def __init__(self, client) -> None:
            del client

        def discover_all(self):
            nonlocal calls
            calls += 1
            raise ValueError("invalid official market payload")

    monkeypatch.setattr(acceptance, "KalshiOfficialQuoteProvider", FakeClient)
    monkeypatch.setattr(acceptance, "KalshiNativeMarketProvider", InvalidProvider)

    with pytest.raises(ValueError, match="invalid official market payload"):
        acceptance.run_acceptance(
            max_seconds=10,
            poll_seconds=1,
            post_rollover_seconds=1,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

    assert calls == 1
    assert clock.sleeps == []
    assert clock.now == 100.0


def test_acceptance_dynamically_observes_adjacent_rollover(
    monkeypatch, clock: DeterministicClock
) -> None:
    start = NOW.replace(minute=0, second=0, microsecond=0)
    first_observed = start + timedelta(minutes=5)
    rollover_observed = start + timedelta(minutes=15, seconds=25)
    first = provider().parse_market(Asset.BTC, raw_market(start=start), first_observed)
    announced = provider().parse_market(
        Asset.BTC,
        raw_market(start=start + timedelta(minutes=15), status="initialized", target="68160.1"),
        first_observed,
    )
    finalized = provider().parse_market(
        Asset.BTC,
        raw_market(start=start, status="finalized", result="yes"),
        rollover_observed,
    )
    successor = provider().parse_market(
        Asset.BTC,
        raw_market(start=start + timedelta(minutes=15), target="68160.1"),
        rollover_observed,
    )
    initial = KalshiDiscovery(Asset.BTC, first_observed, None, first, announced, (), ())
    after = KalshiDiscovery(Asset.BTC, rollover_observed, finalized, successor, None, (), ())
    successor_at = 102.0
    discovery_times: list[float] = []
    successor_quote_calls = 0

    class FakeClient:
        def __init__(self, settings, **kwargs) -> None:
            del settings, kwargs

        def quote_native(self, market):
            nonlocal successor_quote_calls
            if market.ticker == successor.ticker:
                successor_quote_calls += 1
                if successor_quote_calls == 1:
                    raise KalshiTargetUnavailableError("official target not published yet")
            received = first_observed if market.ticker == first.ticker else rollover_observed
            return replace(
                quote(market.ticker, market.event_ticker, received),
                asset=market.asset,
                series=market.series,
            )

        def close(self) -> None:
            return None

    class FakeProvider:
        def __init__(self, client) -> None:
            del client

        def discover_all(self):
            discovery_times.append(clock.monotonic())
            return (initial,)

        def discover(self, asset):
            assert asset is Asset.BTC
            discovery_times.append(clock.monotonic())
            return after if clock.monotonic() >= successor_at else initial

    monkeypatch.setattr(acceptance, "KalshiOfficialQuoteProvider", FakeClient)
    monkeypatch.setattr(acceptance, "KalshiNativeMarketProvider", FakeProvider)

    result = acceptance.run_acceptance(
        max_seconds=10,
        poll_seconds=1,
        post_rollover_seconds=2,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert first.lifecycle is KalshiLifecycle.OPEN
    assert successor.window_start == first.window_end
    assert result["status"] == "pass"
    assert discovery_times == [100.0, 101.0, 102.0, 103.0, 104.0]
    assert clock.sleeps == [1, 1, 1, 1]
    assert clock.now == 104.0 < 110.0
    assert result["discovery_polls"] == 5
    assert result["upstream_unavailable_polls"] == 1
    assert result["baseline_ticker"] == first.ticker
    assert result["announced_next_ticker"] == announced.ticker
    assert result["successor_ticker"] == successor.ticker
    assert result["rollover_observed_at"] == rollover_observed.isoformat()
    assert result["official_settlement_result"] == "yes"
    assert result["lifecycle_replay"] == [
        KalshiLifecycle.OPEN.value,
        KalshiLifecycle.CLOSED.value,
        KalshiLifecycle.SETTLEMENT_PENDING.value,
        KalshiLifecycle.SETTLED_YES.value,
    ]
    assert result["restart_counts_match"] is True


def test_acceptance_without_successor_stops_at_absolute_deadline(
    monkeypatch, clock: DeterministicClock
) -> None:
    start = NOW.replace(minute=0, second=0, microsecond=0)
    observed = start + timedelta(minutes=5)
    first = provider().parse_market(Asset.BTC, raw_market(start=start), observed)
    initial = KalshiDiscovery(Asset.BTC, observed, None, first, None, (), ())
    discovery_times: list[float] = []

    class FakeClient:
        def __init__(self, settings, **kwargs) -> None:
            del settings, kwargs

        def quote_native(self, market):
            return replace(
                quote(market.ticker, market.event_ticker, observed),
                asset=market.asset,
                series=market.series,
            )

        def close(self) -> None:
            return None

    class NoSuccessorProvider:
        def __init__(self, client) -> None:
            del client

        def discover_all(self):
            discovery_times.append(clock.monotonic())
            return (initial,)

        def discover(self, asset):
            assert asset is Asset.BTC
            discovery_times.append(clock.monotonic())
            return initial

    monkeypatch.setattr(acceptance, "KalshiOfficialQuoteProvider", FakeClient)
    monkeypatch.setattr(acceptance, "KalshiNativeMarketProvider", NoSuccessorProvider)

    result = acceptance.run_acceptance(
        max_seconds=5,
        poll_seconds=1,
        post_rollover_seconds=1,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert result["status"] == "expected_upstream_unavailable"
    assert result["reason"] == "no_adjacent_rollover_observed"
    assert discovery_times == [100.0, 101.0, 102.0, 103.0, 104.0]
    assert clock.sleeps == [1, 1, 1, 1, 1]
    assert clock.now == 105.0


def test_acceptance_rejects_timeout_above_thirty_minutes() -> None:
    with pytest.raises(ValueError, match="30 minutes"):
        acceptance.run_acceptance(max_seconds=1800.01)


def test_nearest_current_is_dynamic_and_deterministic() -> None:
    start = NOW.replace(minute=0, second=0, microsecond=0)
    almost_ended = provider().parse_market(Asset.BTC, raw_market(start=start), NOW)
    later = provider().parse_market(
        Asset.ETH,
        raw_market(Asset.ETH, start=start + timedelta(minutes=15), target="3500.1"),
        start + timedelta(minutes=16),
    )
    selected = acceptance._nearest_current(
        (
            KalshiDiscovery(
                Asset.BTC,
                start + timedelta(minutes=14, seconds=55),
                None,
                almost_ended,
                None,
                (),
                (),
            ),
            KalshiDiscovery(Asset.ETH, start + timedelta(minutes=16), None, later, None, (), ()),
        ),
        minimum_observation_seconds=10,
    )

    assert selected is not None and selected.asset is Asset.ETH
