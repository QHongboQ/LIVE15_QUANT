from __future__ import annotations

import asyncio
import os

import pytest

from live15_quant.config import Settings
from live15_quant.recorder import HistoricalRecorder
from live15_quant.storage import RecorderStore

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.getenv("LIVE15_RUN_SMOKE") != "1",
        reason="set LIVE15_RUN_SMOKE=1 to access public external services",
    ),
]


async def test_short_live_recorder_writes_only_to_temporary_path(tmp_path) -> None:
    database = tmp_path / "online-smoke.sqlite3"
    settings = Settings(
        products=("BTC-USD",),
        recorder_data_path=database,
        robinhood_poll_interval_seconds=2,
        recorder_health_interval_seconds=2,
    )

    with RecorderStore(database) as store:
        recorder = HistoricalRecorder(settings, store)
        task = asyncio.create_task(recorder.run())
        try:
            async with asyncio.timeout(40):
                while not (
                    store.count("robinhood_snapshots") > 0 and store.count("coinbase_ticks") > 0
                ):
                    await asyncio.sleep(0.25)
        finally:
            recorder.request_stop()
            await task

        assert recorder.health().written_record_count >= 2

    assert database.exists()
    assert "data" not in database.parts
