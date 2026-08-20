from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
import requests

import live15_quant.native_acceptance as acceptance
from live15_quant.kalshi_lifecycle import KalshiDiscovery, KalshiLifecycle
from live15_quant.models import Asset
from tests.test_kalshi_lifecycle import NOW, provider, quote, raw_market


def test_acceptance_continues_after_exhausted_network_retry(monkeypatch) -> None:
    now = 100.0
    calls: list[float] = []
    client_options: dict[str, object] = {}

    def monotonic() -> float:
        return now

    def sleeper(seconds: float) -> None:
        nonlocal now
        assert seconds >= 0
        now += seconds

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
            calls.append(monotonic())
            raise requests.ConnectionError("public market data unavailable")

    client = FakeClient
    provider = FailingProvider
    monkeypatch.setattr(acceptance, "KalshiOfficialQuoteProvider", client)
    monkeypatch.setattr(acceptance, "KalshiNativeMarketProvider", provider)

    result = acceptance.run_acceptance(
        max_seconds=0.02,
        poll_seconds=0.001,
        post_rollover_seconds=0.001,
        monotonic=monotonic,
        sleeper=sleeper,
    )

    assert len(calls) >= 2
    assert calls == sorted(calls)
    assert all(call < 100.02 for call in calls)
    assert now == pytest.approx(100.02)
    assert result["status"] == "expected_upstream_unavailable"
    assert result["reason"] == "no_current_market_with_published_target"
    assert result["network_retry_exhaustions"] >= 2
    assert client_options["retry_total"] == 0
    assert client_options["deadline_monotonic"] == pytest.approx(100.02)
    assert client_options["monotonic"] is monotonic


def test_acceptance_fails_immediately_on_correctness_error(monkeypatch) -> None:
    now = 100.0
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

    def monotonic() -> float:
        return now

    def sleeper(seconds: float) -> None:
        raise AssertionError(f"correctness error must not sleep: {seconds}")

    monkeypatch.setattr(acceptance, "KalshiOfficialQuoteProvider", FakeClient)
    monkeypatch.setattr(acceptance, "KalshiNativeMarketProvider", InvalidProvider)

    with pytest.raises(ValueError, match="invalid official market payload"):
        acceptance.run_acceptance(
            max_seconds=10,
            poll_seconds=1,
            post_rollover_seconds=1,
            monotonic=monotonic,
            sleeper=sleeper,
        )

    assert calls == 1


def test_acceptance_dynamically_observes_adjacent_rollover(monkeypatch) -> None:
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

    class FakeClient:
        def __init__(self, settings, **kwargs) -> None:
            del settings, kwargs

        def quote_native(self, market):
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
            return (initial,)

        def discover(self, asset):
            assert asset is Asset.BTC
            return after

    monkeypatch.setattr(acceptance, "KalshiOfficialQuoteProvider", FakeClient)
    monkeypatch.setattr(acceptance, "KalshiNativeMarketProvider", FakeProvider)

    result = acceptance.run_acceptance(
        max_seconds=0.1,
        poll_seconds=0.001,
        post_rollover_seconds=0.001,
    )

    assert result["status"] == "pass"
    assert result["baseline_ticker"] == first.ticker
    assert result["announced_next_ticker"] == announced.ticker
    assert result["successor_ticker"] == successor.ticker
    assert result["official_settlement_result"] == "yes"
    assert result["lifecycle_replay"] == [
        KalshiLifecycle.OPEN.value,
        KalshiLifecycle.CLOSED.value,
        KalshiLifecycle.SETTLEMENT_PENDING.value,
        KalshiLifecycle.SETTLED_YES.value,
    ]
    assert result["restart_counts_match"] is True


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
