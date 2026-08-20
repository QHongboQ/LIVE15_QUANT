from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from live15_quant.config import Settings
from live15_quant.models import Asset, MappingConfidence, Venue
from live15_quant.providers.kalshi import KalshiOfficialQuoteProvider
from live15_quant.providers.robinhood_15min import Robinhood15MinuteProvider

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.getenv("LIVE15_RUN_SMOKE") != "1",
        reason="set LIVE15_RUN_SMOKE=1 to access public external services",
    ),
]


def test_kalshi_official_quote_maps_current_robinhood_btc_live() -> None:
    settings = Settings()
    contracts = Robinhood15MinuteProvider(settings).discover()
    btc = next(contract for contract in contracts if contract.asset is Asset.BTC)
    provider = KalshiOfficialQuoteProvider(settings)

    if btc.fetched_at >= btc.end_time:
        mapping, market, _ = provider.map_contract(btc)
        if mapping.confidence is MappingConfidence.VERIFIED:
            assert market is not None
            assert mapping.venue_ticker is not None
            assert "start_time" in mapping.matched_fields
            assert "end_time" in mapping.matched_fields
            assert "target_price" in mapping.matched_fields
        else:
            assert mapping.confidence is MappingConfidence.PARTIAL
            assert market is None
            assert mapping.venue_ticker is None
        pytest.skip(
            "expected upstream-unavailable: Robinhood SSR still exposes a post-end event "
            f"at {datetime.now(UTC).isoformat()}"
        )

    quote = provider.quote(btc)

    assert quote is not None
    assert quote.venue is Venue.KALSHI
    assert quote.mapping_confidence is MappingConfidence.VERIFIED
    assert quote.venue_series == "KXBTC15M"
    assert quote.yes_bid is not None
    assert quote.yes_ask is not None
    assert quote.no_bid is not None
    assert quote.no_ask is not None
    assert quote.last_trade is not None
    assert quote.yes_bid_depth or quote.no_bid_depth
