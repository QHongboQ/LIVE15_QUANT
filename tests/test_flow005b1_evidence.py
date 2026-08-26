from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from live15_quant.sequence_evidence import (
    DepthReadiness,
    SequenceConfig,
    TradeObservation,
    bounded_depth_window,
    build_trade_sequences,
    classify_depth_readiness,
    classify_path_readiness,
    target_within_tolerance,
    tlob_eligibility,
)

START = datetime(2026, 6, 25, 0, 0, tzinfo=UTC)


def trade(seconds: int, price: str = "0.50", trade_id: str | None = None) -> TradeObservation:
    timestamp = START + timedelta(seconds=seconds)
    return TradeObservation(
        ticker="TICKER",
        event_id="EVENT",
        asset="BTC",
        event_start=START,
        event_end=START + timedelta(minutes=15),
        timestamp=timestamp,
        trade_id=trade_id or f"trade-{seconds}-{price}",
        price=Decimal(price),
        quantity=Decimal("1"),
        taker_side="yes",
    )


def test_causal_buckets_never_use_future_trade() -> None:
    rows, summary = build_trade_sequences(
        [trade(1, "0.50"), trade(6, "0.60"), trade(11, "0.70")],
        SequenceConfig(
            grid_seconds=(5,), lookback_seconds=5, target_horizons=(5,), target_tolerance_seconds=2
        ),
    )
    assert rows
    first = rows[0]
    assert first["decision_timestamp"] == "2026-06-25T00:00:05+00:00"
    assert first["features"][0]["last_price"] == "0.50"
    assert first["targets"]["5"]["target_price"] == "0.70"
    assert summary["provenance"] == "H1_KALSHI_OFFICIAL_HISTORY"


def test_missing_buckets_are_excluded_not_filled() -> None:
    rows, summary = build_trade_sequences(
        [trade(1), trade(11)],
        SequenceConfig(
            grid_seconds=(5,), lookback_seconds=10, target_horizons=(5,), target_tolerance_seconds=2
        ),
    )
    assert rows == []
    assert summary["exclusions"]["no_trade_in_source_bucket"] > 0


def test_target_tolerance_requires_future_trade_inside_declared_window() -> None:
    assert target_within_tolerance(START + timedelta(seconds=30), START + timedelta(seconds=40), 10)
    assert not target_within_tolerance(
        START + timedelta(seconds=30), START + timedelta(seconds=41), 10
    )


def test_target_missing_reason_is_preserved_for_each_horizon() -> None:
    rows, _ = build_trade_sequences(
        [trade(seconds, "0.50") for seconds in range(0, 61, 5)],
        SequenceConfig(
            grid_seconds=(5,),
            lookback_seconds=10,
            target_horizons=(30, 60, 120),
            target_tolerance_seconds=2,
        ),
    )
    assert rows
    target = rows[-1]["targets"]
    assert target["120"]["available"] is False
    assert target["120"]["missing_reason"] == "future_target_unavailable"


def test_path_readiness_does_not_use_row_count_as_independent_evidence() -> None:
    report = {
        "sequence_count": 100_000,
        "independent_days": 1,
        "independent_events": 500,
        "fold_count": 0,
    }
    assert classify_path_readiness(report) == "SEQUENCE_PARTIAL_MORE_DATA_OR_REPRESENTATION_NEEDED"


def test_depth_readiness_and_tlob_contract_fail_closed() -> None:
    report = {"snapshot_count": 100, "independent_days": 3, "assets": ["BTC", "ETH"], "events": 5}
    assert classify_depth_readiness(report) == DepthReadiness.READY
    assert (
        classify_depth_readiness(
            {"snapshot_count": 0, "independent_days": 0, "assets": [], "events": 0}
        )
        == DepthReadiness.BLOCKED
    )
    assert (
        tlob_eligibility({"snapshot_count": 100, "has_continuous_sequence": False})
        == "TLOB_ADAPTER_OR_DATA_GAP"
    )


def test_depthfeed_window_is_hard_bounded() -> None:
    start, end = bounded_depth_window(START + timedelta(days=7))
    assert end - start == timedelta(days=7)
