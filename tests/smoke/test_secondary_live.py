from __future__ import annotations

import asyncio
import os

import pytest

from live15_quant.models import Asset
from live15_quant.providers.low_latency import (
    BenchmarkSource,
    BenchmarkTick,
    BinanceBnbPublicMarketDataSource,
    HyperliquidHypePublicMarketDataSource,
    LowLatencyProvider,
)

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.getenv("LIVE15_RUN_SMOKE") != "1",
        reason="set LIVE15_RUN_SMOKE=1 to access official public venue streams",
    ),
]


async def _one_tick(source: BenchmarkSource, *, timeout_seconds: float = 15) -> BenchmarkTick:
    try:
        async with asyncio.timeout(timeout_seconds):
            return await anext(source.ticks())
    finally:
        await source.close()


@pytest.mark.asyncio
async def test_binance_bnb_public_secondary_stream_is_read_only_and_exact() -> None:
    tick = await _one_tick(BinanceBnbPublicMarketDataSource())
    assert tick.asset is Asset.BNB
    assert tick.provider is LowLatencyProvider.BINANCE_SPOT
    assert tick.instrument_id == "BNBUSDT"
    assert tick.symbol == "BNB/USDT"
    assert tick.price > 0
    assert tick.bid is None and tick.ask is None


@pytest.mark.asyncio
async def test_hyperliquid_hype_public_secondary_stream_is_read_only_and_exact() -> None:
    tick = await _one_tick(HyperliquidHypePublicMarketDataSource())
    assert tick.asset is Asset.HYPE
    assert tick.provider is LowLatencyProvider.HYPERLIQUID_PERP
    assert tick.instrument_id == "HYPE"
    assert tick.symbol == "HYPE/USDC perpetual BBO"
    assert tick.bid is not None and tick.ask is not None
    assert tick.price == (tick.bid + tick.ask) / 2
