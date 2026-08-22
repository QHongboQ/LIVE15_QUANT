from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from live15_quant.feature_registry import FEATURE_BY_NAME, MissingReason
from live15_quant.features import FeatureEngine, FeatureInputs, SamplingPolicy
from live15_quant.kalshi_lifecycle import KalshiLifecycle
from live15_quant.models import (
    Asset,
    DataRole,
    ExecutabilityClassification,
    FreshnessState,
    OrderBookLevel,
    SourceTimestampKind,
    UnderlyingProvider,
)
from live15_quant.records import (
    CoinbaseTickRecord,
    KalshiFeatureMarketRecord,
    KalshiNativeQuoteRecord,
    UnderlyingObservationRecord,
)

START = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
DECISION = START + timedelta(minutes=10)
TICKER = "KXBTC15M-26AUG201200-00"
EVENT = "KXBTC15M-26AUG201200"


def policy() -> SamplingPolicy:
    return SamplingPolicy(
        (timedelta(minutes=10), timedelta(minutes=5), timedelta(seconds=30)),
        quote_max_age=timedelta(seconds=15),
        underlying_max_age=timedelta(seconds=15),
    )


def market(**updates) -> KalshiFeatureMarketRecord:
    values = {
        "row_id": 1,
        "schema_version": 4,
        "asset": Asset.BTC,
        "series": "KXBTC15M",
        "ticker": TICKER,
        "event_ticker": EVENT,
        "window_start": START,
        "window_end": START + timedelta(minutes=15),
        "target": Decimal("100.00000001"),
        "lifecycle": KalshiLifecycle.OPEN,
        "official_status": "active",
        "fetched_timestamp": START + timedelta(seconds=1),
        "source_url": "https://api.example/market",
        "rules_primary": "primary",
        "rules_secondary": "secondary",
        "settlement_timer_seconds": 15,
    }
    values.update(updates)
    return KalshiFeatureMarketRecord(**values)


def tick(seconds_before: int, price: str, *, row_id: int) -> CoinbaseTickRecord:
    timestamp = DECISION - timedelta(seconds=seconds_before)
    return CoinbaseTickRecord(
        row_id=row_id,
        schema_version=4,
        exchange_timestamp=timestamp,
        received_timestamp=timestamp,
        product="BTC-USD",
        price=Decimal(price),
        bid=Decimal(price) - Decimal("0.01"),
        ask=Decimal(price) + Decimal("0.01"),
        spread=Decimal("0.02"),
        bid_size=Decimal("1.1"),
        ask_size=Decimal("1.2"),
        last_size=None,
        volume_24h=None,
        role=DataRole.PREDICTIVE_MARKET_DATA,
    )


def quote(
    seconds_before: int = 2,
    *,
    row_id: int = 1,
    yes_bid: Decimal | None = Decimal("0.400000000000000001"),
    freshness: FreshnessState = FreshnessState.FRESH,
) -> KalshiNativeQuoteRecord:
    timestamp = DECISION - timedelta(seconds=seconds_before)
    return KalshiNativeQuoteRecord(
        row_id=row_id,
        schema_version=4,
        asset=Asset.BTC,
        series="KXBTC15M",
        ticker=TICKER,
        event_ticker=EVENT,
        source_timestamp=timestamp,
        source_timestamp_kind=SourceTimestampKind.HTTP_RESPONSE_DATE,
        received_timestamp=timestamp,
        yes_bid=yes_bid,
        yes_ask=Decimal("0.420000000000000001"),
        no_bid=Decimal("0.580000000000000001"),
        no_ask=Decimal("0.600000000000000001"),
        last_trade=Decimal("0.410000000000000001"),
        volume=Decimal("123.000000000000000001"),
        yes_bid_depth=(
            OrderBookLevel(Decimal("0.40"), Decimal("2.000000000000000001")),
            OrderBookLevel(Decimal("0.39"), Decimal("3.000000000000000001")),
        ),
        no_bid_depth=(OrderBookLevel(Decimal("0.58"), Decimal("4.000000000000000001")),),
        source="official Kalshi REST",
        freshness=freshness,
        executability=ExecutabilityClassification.OFFICIAL_VENUE_ORDER_BOOK,
        evidence_urls=("https://docs.kalshi.com/",),
        role=DataRole.CONTRACT_MARKET_QUOTE,
    )


def ticks() -> tuple[CoinbaseTickRecord, ...]:
    return tuple(
        tick(seconds, str(Decimal("100") + Decimal(300 - seconds) / Decimal("1000")), row_id=i)
        for i, seconds in enumerate(range(300, -1, -15), 1)
    )


def test_feature_calculation_is_deterministic_decimal_safe_and_live_compatible() -> None:
    source = FeatureInputs(market(), (quote(5, row_id=1), quote(2, row_id=2)), ticks(), DECISION)
    engine = FeatureEngine(policy())

    offline = engine.compute(source)
    live = engine.compute(source)
    values = offline.by_name()

    assert offline == live
    assert len(values) == len(FEATURE_BY_NAME) == 42
    assert values["underlying_price"].value == Decimal("100.3")
    assert values["yes_bid"].value == Decimal("0.400000000000000001")
    assert values["yes_spread"].value == Decimal("0.020000000000000000")
    assert values["yes_cumulative_depth"].value == Decimal("5.000000000000000002")
    assert values["yes_top_depth_change"].value == Decimal(0)


def test_pyth_asset_uses_provider_specific_underlying_without_future_leakage() -> None:
    gold_market = market(
        asset=Asset.GOLD,
        series="KXGOLD15M",
        event_ticker="KXGOLD15M-26AUG201200",
        ticker="KXGOLD15M-26AUG201200-00",
        target=Decimal("3388"),
    )
    observations = tuple(
        UnderlyingObservationRecord(
            row_id=index,
            schema_version=6,
            asset=Asset.GOLD,
            provider=UnderlyingProvider.PYTH_HERMES,
            symbol="Metal.XAU/USD",
            feed_id="a" * 64,
            price=Decimal("3388") + Decimal(index) / Decimal(100),
            source_timestamp=DECISION - timedelta(seconds=seconds),
            received_timestamp=DECISION - timedelta(seconds=seconds),
            confidence=Decimal("0.01"),
            provenance="https://official.example/hermes",
            freshness=FreshnessState.FRESH,
            role=DataRole.PREDICTIVE_MARKET_DATA,
        )
        for index, seconds in enumerate(range(300, -1, -15), 1)
    )
    future = replace(
        observations[-1],
        row_id=999,
        price=Decimal("9999"),
        source_timestamp=DECISION + timedelta(seconds=1),
        received_timestamp=DECISION + timedelta(seconds=1),
    )
    vector = FeatureEngine(policy()).compute(
        FeatureInputs(gold_market, (), (), DECISION, (*observations, future))
    )
    assert vector.by_name()["underlying_price"].value == observations[-1].price


def test_closed_market_underlying_is_not_forward_filled_as_fresh() -> None:
    decision = datetime(2026, 8, 22, 4, 0, tzinfo=UTC)
    gold_market = market(
        asset=Asset.GOLD,
        series="KXGOLD15M",
        event_ticker="KXGOLD15M-26AUG220400",
        ticker="KXGOLD15M-26AUG220400-00",
        target=Decimal("3388"),
        window_start=decision - timedelta(minutes=5),
        window_end=decision + timedelta(minutes=10),
        fetched_timestamp=decision - timedelta(minutes=5),
    )
    prior_close = UnderlyingObservationRecord(
        row_id=1,
        schema_version=6,
        asset=Asset.GOLD,
        provider=UnderlyingProvider.PYTH_HERMES,
        symbol="Metal.XAU/USD",
        feed_id="a" * 64,
        price=Decimal("3388.10"),
        source_timestamp=datetime(2026, 8, 21, 20, 59, tzinfo=UTC),
        received_timestamp=datetime(2026, 8, 21, 20, 59, tzinfo=UTC),
        confidence=Decimal("0.01"),
        provenance="official-test",
        freshness=FreshnessState.FRESH,
        role=DataRole.PREDICTIVE_MARKET_DATA,
    )
    vector = (
        FeatureEngine(policy())
        .compute(FeatureInputs(gold_market, (), (), decision, (prior_close,)))
        .by_name()
    )

    assert vector["underlying_price"].value is None
    assert vector["underlying_price"].missing_reason is MissingReason.MARKET_CLOSED


def test_future_quote_and_future_tick_are_excluded() -> None:
    engine = FeatureEngine(policy())
    past = FeatureInputs(market(), (quote(),), ticks(), DECISION)
    future_quote = replace(
        quote(row_id=99, yes_bid=Decimal("0.99")),
        source_timestamp=DECISION + timedelta(seconds=1),
        received_timestamp=DECISION + timedelta(seconds=1),
    )
    future_tick = replace(
        tick(0, "999", row_id=99),
        exchange_timestamp=DECISION + timedelta(seconds=1),
        received_timestamp=DECISION + timedelta(seconds=1),
    )

    assert engine.compute(past) == engine.compute(
        replace(past, quotes=(*past.quotes, future_quote), ticks=(*past.ticks, future_tick))
    )


def test_post_window_quote_and_orderbook_cannot_enter_features() -> None:
    engine = FeatureEngine(policy())
    baseline = FeatureInputs(market(), (quote(),), ticks(), DECISION)
    post_window = replace(
        quote(row_id=99, yes_bid=Decimal("0.99")),
        source_timestamp=START + timedelta(minutes=15),
        received_timestamp=START + timedelta(minutes=15),
    )

    assert engine.compute(baseline) == engine.compute(
        replace(baseline, quotes=(*baseline.quotes, post_window))
    )


def test_received_tick_with_future_exchange_timestamp_is_excluded() -> None:
    future_source = replace(
        tick(1, "999", row_id=99), exchange_timestamp=DECISION + timedelta(seconds=1)
    )
    vector = FeatureEngine(policy()).compute(
        FeatureInputs(market(), (quote(),), (*ticks(), future_source), DECISION)
    )

    assert vector.by_name()["underlying_price"].value == Decimal("100.3")


def test_received_quote_with_future_source_timestamp_is_excluded() -> None:
    invalid = replace(
        quote(1, row_id=99, yes_bid=Decimal("0.99")),
        source_timestamp=DECISION + timedelta(seconds=1),
    )
    vector = FeatureEngine(policy()).compute(
        FeatureInputs(market(), (quote(2), invalid), ticks(), DECISION)
    )

    assert vector.by_name()["yes_bid"].value == Decimal("0.400000000000000001")


def test_recent_receive_with_old_source_timestamp_is_stale() -> None:
    delayed_tick = replace(
        tick(1, "999", row_id=99), exchange_timestamp=DECISION - timedelta(minutes=1)
    )
    delayed_quote = replace(quote(1, row_id=99), source_timestamp=DECISION - timedelta(minutes=1))
    vector = (
        FeatureEngine(policy())
        .compute(FeatureInputs(market(), (delayed_quote,), (delayed_tick,), DECISION))
        .by_name()
    )

    assert vector["underlying_price"].value is None
    assert vector["underlying_price"].missing_reason is MissingReason.STALE
    assert vector["yes_bid"].value is None
    assert vector["yes_bid"].missing_reason is MissingReason.STALE


def test_return_boundary_rejects_tick_with_future_exchange_time() -> None:
    boundary = DECISION - timedelta(seconds=15)
    invalid_baseline = replace(
        tick(15, "90", row_id=98), exchange_timestamp=boundary + timedelta(seconds=1)
    )
    vector = (
        FeatureEngine(policy())
        .compute(FeatureInputs(market(), (quote(),), (*ticks(), invalid_baseline), DECISION))
        .by_name()
    )

    assert vector["return_15s"].value != Decimal("100.3") / Decimal("90") - Decimal(1)


def test_future_metadata_and_terminal_lifecycle_fail_closed() -> None:
    engine = FeatureEngine(policy())
    with pytest.raises(ValueError, match="future market metadata"):
        engine.compute(
            FeatureInputs(
                market(fetched_timestamp=DECISION + timedelta(seconds=1)), (), (), DECISION
            )
        )
    with pytest.raises(ValueError, match="terminal or future lifecycle"):
        engine.compute(
            FeatureInputs(market(lifecycle=KalshiLifecycle.SETTLED_YES), (), (), DECISION)
        )


def test_missing_stale_and_insufficient_lookback_reasons_are_not_zero_filled() -> None:
    engine = FeatureEngine(policy())
    sparse = (tick(1, "100", row_id=1),)
    vector = engine.compute(
        FeatureInputs(
            market(),
            (quote(20, freshness=FreshnessState.STALE),),
            sparse,
            DECISION,
        )
    ).by_name()

    assert vector["yes_bid"].value is None
    assert vector["yes_bid"].missing_reason is MissingReason.STALE
    assert vector["return_15s"].missing_reason is MissingReason.NOT_ENOUGH_LOOKBACK
    assert vector["return_15s"].value is None


def test_missing_market_side_and_unsupported_underlying_are_explicit() -> None:
    btc = FeatureEngine(policy()).compute(
        FeatureInputs(market(), (quote(yes_bid=None),), ticks(), DECISION)
    )
    gold = FeatureEngine(policy()).compute(
        FeatureInputs(
            market(
                asset=Asset.GOLD,
                series="KXGOLD15M",
                ticker="KXGOLD15M-26AUG201200-00",
                event_ticker="KXGOLD15M-26AUG201200",
            ),
            (),
            (),
            DECISION,
        )
    )

    assert btc.by_name()["yes_bid"].missing_reason is MissingReason.MARKET_SIDE_UNAVAILABLE
    assert gold.by_name()["underlying_price"].missing_reason is MissingReason.SOURCE_UNAVAILABLE


def test_sampling_grid_is_configurable_and_utc_is_required() -> None:
    decisions = policy().decision_times(START, START + timedelta(minutes=15))
    assert decisions == (
        START + timedelta(minutes=5),
        START + timedelta(minutes=10),
        START + timedelta(minutes=14, seconds=30),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        policy().decision_times(START.replace(tzinfo=None), START + timedelta(minutes=15))


def test_feature_interface_contains_no_settlement_or_label_input() -> None:
    fields = set(FeatureInputs.__dataclass_fields__)
    assert "settlement" not in fields
    assert "label" not in fields
    assert not any("settlement" in name or "result" in name for name in FEATURE_BY_NAME)
