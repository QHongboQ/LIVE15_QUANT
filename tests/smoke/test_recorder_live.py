from __future__ import annotations

import asyncio
import os
import sqlite3

import pytest

from live15_quant.config import Settings
from live15_quant.models import RecorderDiagnosticKind
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
        outcome: str | None = None
        try:
            async with asyncio.timeout(40):
                while outcome is None:
                    if task.done():
                        await task
                    if (
                        store.count("robinhood_snapshots") > 0
                        and store.count("coinbase_ticks") > 0
                        and store.count("prediction_market_quotes") > 0
                    ):
                        outcome = "complete"
                    elif (
                        recorder.health().rollover_gaps
                        and store.count("coinbase_ticks") > 0
                        and store.count("robinhood_diagnostics") > 0
                    ):
                        outcome = "expected_rollover_gap"
                    if outcome is not None:
                        break
                    await asyncio.sleep(0.25)
        finally:
            recorder.request_stop()
            await task

        assert outcome is not None
        gaps = recorder.health().rollover_gaps
        if outcome == "expected_rollover_gap":
            assert gaps
            for gap in gaps:
                diagnostics = list(store.replay_robinhood_diagnostics(gap.previous_event_id))
                kinds = {item.kind for item in diagnostics}
                assert RecorderDiagnosticKind.POST_END_EVENT_RETURNED in kinds
                assert RecorderDiagnosticKind.ROLLOVER_GAP_STARTED in kinds
                assert all(
                    item.fetched_timestamp < item.end_time
                    for item in store.replay_robinhood(gap.previous_event_id)
                )
        else:
            assert recorder.health().written_record_count >= 3

    assert database.exists()
    assert "data" not in database.parts
    with sqlite3.connect(database) as connection:
        post_end_snapshots = connection.execute(
            "SELECT COUNT(*) FROM robinhood_snapshots WHERE fetched_timestamp >= end_time"
        ).fetchone()
        post_end_quotes = connection.execute(
            """
            SELECT COUNT(*)
            FROM prediction_market_quotes AS quote
            JOIN robinhood_diagnostics AS diagnostic
              ON diagnostic.event_id = quote.robinhood_event_id
            WHERE diagnostic.kind = ?
              AND quote.received_timestamp >= diagnostic.event_end_time
            """,
            (RecorderDiagnosticKind.POST_END_EVENT_RETURNED.value,),
        ).fetchone()
    assert post_end_snapshots == (0,)
    assert post_end_quotes == (0,)

    if outcome == "expected_rollover_gap":
        pytest.skip("expected upstream-unavailable: validated safe Robinhood rollover gap")
