from __future__ import annotations

import asyncio
import os

import pytest

from live15_quant.config import Settings
from live15_quant.providers.coinbase import CoinbaseRestClient, CoinbaseWebSocketClient

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.getenv("LIVE15_RUN_SMOKE") != "1",
        reason="set LIVE15_RUN_SMOKE=1 to access Coinbase public services",
    ),
]


def test_coinbase_rest_live() -> None:
    tick = CoinbaseRestClient(Settings()).get_ticker("BTC-USD")

    assert tick.symbol == "BTC-USD"
    assert tick.bid > 0
    assert tick.ask >= tick.bid


async def test_coinbase_websocket_live() -> None:
    stream = CoinbaseWebSocketClient(Settings(), products=("BTC-USD",)).ticks()
    try:
        tick = await asyncio.wait_for(anext(stream), timeout=20)
    finally:
        await stream.aclose()

    assert tick.symbol == "BTC-USD"
    assert tick.price > 0
