from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from live15_quant.kalshi_ws import (
    KalshiBookSide,
    KalshiOrderBookDelta,
    KalshiOrderBookSequenceGuard,
    KalshiOrderBookSnapshot,
    KalshiSequenceGapError,
)
from live15_quant.models import OrderBookLevel

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def snapshot(sequence: int = 10) -> KalshiOrderBookSnapshot:
    return KalshiOrderBookSnapshot(
        subscription_id=2,
        sequence=sequence,
        ticker="KXBTC15M-26AUG201215-15",
        market_id="official-market-id",
        yes_bids=(OrderBookLevel(Decimal("0.5000"), Decimal("12.00")),),
        no_bids=(),
        received_timestamp=NOW,
    )


def delta(sequence: int = 11) -> KalshiOrderBookDelta:
    return KalshiOrderBookDelta(
        subscription_id=2,
        sequence=sequence,
        ticker="KXBTC15M-26AUG201215-15",
        market_id="official-market-id",
        side=KalshiBookSide.YES,
        price=Decimal("0.5000"),
        quantity_delta=Decimal("-2.00"),
        source_timestamp=NOW,
        received_timestamp=NOW,
    )


def test_sequence_guard_requires_snapshot_and_contiguous_deltas() -> None:
    guard = KalshiOrderBookSequenceGuard()
    with pytest.raises(KalshiSequenceGapError, match="before a snapshot"):
        guard.accept(delta())
    guard.accept(snapshot())
    guard.accept(delta())
    with pytest.raises(KalshiSequenceGapError, match="expected 12, got 13"):
        guard.accept(delta(13))


def test_reconnect_reset_requires_a_new_snapshot() -> None:
    guard = KalshiOrderBookSequenceGuard()
    guard.accept(snapshot())
    guard.reset()
    with pytest.raises(KalshiSequenceGapError, match="before a snapshot"):
        guard.accept(delta())


def test_delta_instrument_must_match_subscription_snapshot() -> None:
    guard = KalshiOrderBookSequenceGuard()
    guard.accept(snapshot())
    with pytest.raises(KalshiSequenceGapError, match="instrument"):
        guard.accept(replace(delta(), ticker="KXETH15M-OTHER"))


def test_websocket_models_preserve_decimal_and_reject_naive_time() -> None:
    assert delta().quantity_delta == Decimal("-2.00")
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(delta(), source_timestamp=NOW.replace(tzinfo=None))
