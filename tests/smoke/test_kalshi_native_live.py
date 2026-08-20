from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from live15_quant.config import Settings
from live15_quant.kalshi_lifecycle import KalshiLifecycle, KalshiNativeMarketProvider
from live15_quant.providers.kalshi import KALSHI_15MIN_SERIES, KalshiOfficialQuoteProvider

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.getenv("LIVE15_RUN_SMOKE") != "1",
        reason="set LIVE15_RUN_SMOKE=1 to access official public Kalshi services",
    ),
]


def test_all_ten_native_markets_and_one_orderbook_without_robinhood() -> None:
    client = KalshiOfficialQuoteProvider(Settings())
    provider = KalshiNativeMarketProvider(client)
    try:
        deadline = time.monotonic() + 20
        while True:
            discoveries = provider.discover_all()
            current = tuple(item.current for item in discoveries if item.current is not None)
            if len(current) == 10 or time.monotonic() >= deadline:
                break
            time.sleep(2)
        if len(current) != 10:
            pytest.skip(
                "expected upstream-unavailable: official target not published for new window"
            )
        assert len({market.series for market in current}) == 10
        assert all(market.lifecycle is KalshiLifecycle.OPEN for market in current)
        quote = client.quote_native(current[0])
        assert quote.ticker == current[0].ticker
        assert quote.event_ticker == current[0].event_ticker
    finally:
        client.close()


def test_official_historical_market_exposes_finalized_label() -> None:
    client = KalshiOfficialQuoteProvider(Settings())
    provider = KalshiNativeMarketProvider(client)
    now = datetime.now(UTC)
    try:
        page = next(
            provider.backfill_pages(
                next(iter(KALSHI_15MIN_SERIES)),
                start=now - timedelta(days=365),
                end=now,
                historical=True,
            )
        )
        labels = tuple(market.settlement for market in page.markets if market.settlement)
        assert labels
        assert all(label.official_source.find("/historical/markets/") >= 0 for label in labels)
    finally:
        client.close()
