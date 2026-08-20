from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from live15_quant.config import Settings
from live15_quant.dataset import DatasetBuildConfig, DatasetBuilder, FeatureStore
from live15_quant.features import SamplingPolicy
from live15_quant.kalshi_lifecycle import KalshiLifecycle, KalshiNativeMarketProvider
from live15_quant.native_recorder import KalshiNativeRecorder
from live15_quant.providers.kalshi import KALSHI_15MIN_SERIES, KalshiOfficialQuoteProvider
from live15_quant.storage import RecorderStore

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


async def test_continuous_native_recorder_restart_health_and_integrity(tmp_path) -> None:
    database = tmp_path / "continuous-native.sqlite3"
    health_path = tmp_path / "health.json"
    settings = Settings(
        recorder_data_path=database,
        recorder_health_path=health_path,
        native_discovery_poll_interval_seconds=2,
        official_quote_poll_interval_seconds=2,
        settlement_followup_interval_seconds=2,
        recorder_checkpoint_interval_seconds=2,
        recorder_health_interval_seconds=1,
        recorder_operation_timeout_seconds=15,
    )
    with RecorderStore(database) as store:
        recorder = KalshiNativeRecorder(settings, store)
        task = asyncio.create_task(recorder.run())
        try:
            async with asyncio.timeout(45):
                while True:
                    if task.done():
                        await task
                    health = recorder.health()
                    if (
                        len(health.last_discovery) == len(KALSHI_15MIN_SERIES)
                        and len(health.last_quotes) == len(KALSHI_15MIN_SERIES)
                        and len(health.last_coinbase) == len(settings.products)
                    ):
                        break
                    await asyncio.sleep(0.25)
        finally:
            recorder.request_stop()
            await task
        quote_cursors = dict(recorder.health().last_quotes)
        tick_cursors = dict(recorder.health().last_coinbase)
        recorded_assets = {
            row[0]
            for row in store._connection.execute(
                "SELECT DISTINCT asset FROM kalshi_prediction_quotes"
            )
        }
        assert recorded_assets == {asset.value for asset in KALSHI_15MIN_SERIES}
        assert store.integrity_check() == "ok"

    with RecorderStore(database) as recovered:
        restarted = KalshiNativeRecorder(settings, recovered)
        assert restarted.health().last_quotes == quote_cursors
        assert restarted.health().last_coinbase == tick_cursors
        assert restarted.health().integrity == "ok"
        with FeatureStore(tmp_path / "features.sqlite3") as features:
            summary = DatasetBuilder(recovered, features).build(
                DatasetBuildConfig(
                    SamplingPolicy(
                        tuple(
                            timedelta(seconds=value)
                            for value in settings.dataset_decision_offsets_seconds
                        ),
                        quote_max_age=timedelta(seconds=settings.dataset_quote_max_age_seconds),
                        underlying_max_age=timedelta(
                            seconds=settings.dataset_underlying_max_age_seconds
                        ),
                    )
                )
            )
            assert summary.complete
    assert health_path.exists()
