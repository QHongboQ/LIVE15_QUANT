from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from live15_quant.config import Settings
from live15_quant.models import (
    FifteenMinuteContract,
    FreshnessState,
    LifecycleState,
    RecorderDiagnosticKind,
)
from live15_quant.recorder import HistoricalRecorder
from live15_quant.storage import RecorderStore
from tests.test_storage import contract, prediction_quote, tick


class FakeDiscovery:
    def __init__(self, contracts: Sequence[FifteenMinuteContract]) -> None:
        self.contracts = contracts

    def discover(self) -> Sequence[FifteenMinuteContract]:
        return self.contracts


class FakeTickStream:
    async def ticks(self) -> AsyncIterator:
        yield tick()
        await asyncio.Event().wait()


class FakeOfficialQuotes:
    def __init__(self, quotes=()) -> None:
        self.items = quotes

    def quotes(self, contracts):
        return self.items


def settings(path: Path) -> Settings:
    return Settings(
        products=("BTC-USD",),
        recorder_data_path=path,
        robinhood_poll_interval_seconds=0.01,
        reconnect_delay_seconds=0.01,
        recorder_health_interval_seconds=0.01,
        recorder_coinbase_stale_seconds=10,
        official_quote_poll_interval_seconds=0.01,
    )


async def test_graceful_shutdown_flushes_records(tmp_path) -> None:
    path = tmp_path / "history.sqlite3"
    active_contract = contract()
    with RecorderStore(path) as store:
        recorder = HistoricalRecorder(
            settings(path),
            store,
            robinhood=FakeDiscovery((active_contract,)),
            coinbase_factory=FakeTickStream,
            official_quotes=FakeOfficialQuotes((prediction_quote(),)),
            now=lambda: active_contract.fetched_at,
        )
        task = asyncio.create_task(recorder.run())
        for _ in range(100):
            if recorder.health().written_record_count >= 3:
                break
            await asyncio.sleep(0.01)
        recorder.request_stop()
        await asyncio.wait_for(task, timeout=1)

        assert store.count("robinhood_snapshots") >= 1
        assert store.count("coinbase_ticks") == 1
        assert store.count("prediction_market_quotes") == 1


def test_health_detects_stale_and_missing_sources(tmp_path) -> None:
    now = datetime(2026, 8, 20, 12, 10, tzinfo=UTC)
    stale_contract = replace(
        contract(datetime(2026, 8, 20, 12, 1, tzinfo=UTC)),
        freshness_state=FreshnessState.STALE,
    )
    with RecorderStore(tmp_path / "history.sqlite3") as store:
        recorder = HistoricalRecorder(
            Settings(products=("BTC-USD", "ETH-USD"), recorder_coinbase_stale_seconds=30),
            store,
            robinhood=FakeDiscovery(()),
            coinbase_factory=FakeTickStream,
            now=lambda: now,
        )
        recorder._accept_contracts((stale_contract,))
        recorder._health.coinbase_last_updates["BTC-USD"] = now - timedelta(seconds=31)
        health = recorder.health()

    assert health.stale_source_count == 4


def test_closed_pre_end_event_is_recorded_then_removed_from_tracking(tmp_path) -> None:
    ended = replace(
        contract(datetime(2026, 8, 20, 12, 14, tzinfo=UTC)),
        lifecycle_state=LifecycleState.CLOSED,
    )
    with RecorderStore(tmp_path / "history.sqlite3") as store:
        recorder = HistoricalRecorder(
            Settings(),
            store,
            robinhood=FakeDiscovery(()),
            coinbase_factory=FakeTickStream,
        )
        recorder._accept_contracts((ended,))

        assert recorder.health().tracked_event_count == 0
        assert store.count("robinhood_snapshots") == 1


def test_post_end_old_event_is_diagnostic_not_training_data(tmp_path) -> None:
    old = contract(datetime(2026, 8, 20, 12, 16, tzinfo=UTC))
    with RecorderStore(tmp_path / "history.sqlite3") as store:
        recorder = HistoricalRecorder(
            Settings(), store, robinhood=FakeDiscovery(()), coinbase_factory=FakeTickStream
        )
        recorder._accept_contracts((old,))

        assert store.count("robinhood_snapshots") == 0
        diagnostics = list(store.replay_robinhood_diagnostics(old.event_id))
        assert {item.kind for item in diagnostics} == {
            RecorderDiagnosticKind.ROLLOVER_GAP_STARTED,
            RecorderDiagnosticKind.POST_END_EVENT_RETURNED,
        }
        health = recorder.health()
        assert health.rollover_gaps[0].started_at == old.end_time
        assert health.stale_source_count >= 1
        gap_fields = recorder._health_fields()["rollover_gaps"]
        assert isinstance(gap_fields, tuple)
        assert gap_fields[0]["asset"] == "BTC"
        assert gap_fields[0]["previous_event_id"] == old.event_id


def test_rollover_gap_persists_until_delayed_next_event(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "history.sqlite3"
    old = contract(datetime(2026, 8, 20, 12, 14, tzinfo=UTC))
    after_end = replace(old, fetched_at=datetime(2026, 8, 20, 12, 16, tzinfo=UTC))
    next_event = replace(
        old,
        event_id="event-2",
        contract_id="contract-2",
        start_time=old.end_time,
        end_time=old.end_time + timedelta(minutes=15),
        fetched_at=datetime(2026, 8, 20, 12, 17, tzinfo=UTC),
        lifecycle_state=LifecycleState.LIVE,
    )
    with RecorderStore(path) as store:
        recorder = HistoricalRecorder(
            Settings(), store, robinhood=FakeDiscovery(()), coinbase_factory=FakeTickStream
        )
        recorder._accept_contracts((old,))
        recorder._accept_contracts((after_end,))
        assert len(recorder.health().rollover_gaps) == 1
        assert store.count("robinhood_snapshots") == 1

        recorder._accept_contracts((after_end,))
        assert len(recorder.health().rollover_gaps) == 1
        recorder._accept_contracts((next_event,))

        assert recorder.health().rollover_gaps == ()
        assert store.count("robinhood_snapshots") == 2
        kinds = [item.kind for item in store.replay_robinhood_diagnostics(old.event_id)]
        assert kinds.count(RecorderDiagnosticKind.POST_END_EVENT_RETURNED) == 1
        assert RecorderDiagnosticKind.ROLLOVER_GAP_STARTED in kinds
        assert RecorderDiagnosticKind.ROLLOVER_GAP_ENDED in kinds
        post_end_logs = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "robinhood_post_end_event_returned"
        ]
        assert len(post_end_logs) == 1


def test_restart_during_rollover_gap_recovers_then_closes_it(tmp_path) -> None:
    path = tmp_path / "history.sqlite3"
    old = contract(datetime(2026, 8, 20, 12, 16, tzinfo=UTC))
    with RecorderStore(path) as store:
        first = HistoricalRecorder(
            Settings(), store, robinhood=FakeDiscovery(()), coinbase_factory=FakeTickStream
        )
        first._accept_contracts((old,))
        assert len(first.health().rollover_gaps) == 1

    next_event = replace(
        old,
        event_id="event-2",
        contract_id="contract-2",
        start_time=old.end_time,
        end_time=old.end_time + timedelta(minutes=15),
        fetched_at=datetime(2026, 8, 20, 12, 17, tzinfo=UTC),
    )
    with RecorderStore(path) as store:
        restarted = HistoricalRecorder(
            Settings(), store, robinhood=FakeDiscovery(()), coinbase_factory=FakeTickStream
        )
        assert restarted.health().rollover_gaps[0].previous_event_id == old.event_id
        restarted._accept_contracts((next_event,))
        assert restarted.health().rollover_gaps == ()


def test_direct_rollover_closes_gap_when_next_event_replaces_old(tmp_path) -> None:
    old = contract(datetime(2026, 8, 20, 12, 14, tzinfo=UTC))
    next_event = replace(
        old,
        event_id="event-2",
        contract_id="contract-2",
        start_time=old.end_time,
        end_time=old.end_time + timedelta(minutes=15),
        fetched_at=datetime(2026, 8, 20, 12, 16, tzinfo=UTC),
    )
    with RecorderStore(tmp_path / "history.sqlite3") as store:
        recorder = HistoricalRecorder(
            Settings(), store, robinhood=FakeDiscovery(()), coinbase_factory=FakeTickStream
        )
        recorder._accept_contracts((old,))
        recorder._accept_contracts((next_event,))

        assert recorder.health().rollover_gaps == ()
        kinds = [item.kind for item in store.replay_robinhood_diagnostics(old.event_id)]
        assert kinds == [
            RecorderDiagnosticKind.ROLLOVER_GAP_STARTED,
            RecorderDiagnosticKind.ROLLOVER_GAP_ENDED,
        ]


def test_regressed_old_page_does_not_reopen_gap_while_successor_is_active(tmp_path) -> None:
    old = contract(datetime(2026, 8, 20, 12, 16, tzinfo=UTC))
    successor = replace(
        old,
        event_id="event-2",
        contract_id="contract-2",
        start_time=old.end_time,
        end_time=old.end_time + timedelta(minutes=15),
        fetched_at=datetime(2026, 8, 20, 12, 17, tzinfo=UTC),
    )
    regressed_old = replace(old, fetched_at=datetime(2026, 8, 20, 12, 18, tzinfo=UTC))
    with RecorderStore(tmp_path / "history.sqlite3") as store:
        recorder = HistoricalRecorder(
            Settings(), store, robinhood=FakeDiscovery(()), coinbase_factory=FakeTickStream
        )
        recorder._accept_contracts((old,))
        recorder._accept_contracts((successor,))
        assert recorder.health().rollover_gaps == ()

        recorder._accept_contracts((regressed_old,))

        assert recorder.health().rollover_gaps == ()
        assert recorder.health().tracked_event_count == 1


def test_post_end_official_quote_is_not_persisted(tmp_path) -> None:
    active = contract(datetime(2026, 8, 20, 12, 1, tzinfo=UTC))
    post_end = replace(prediction_quote(), received_timestamp=active.end_time)
    with RecorderStore(tmp_path / "history.sqlite3") as store:
        recorder = HistoricalRecorder(
            Settings(), store, robinhood=FakeDiscovery(()), coinbase_factory=FakeTickStream
        )
        recorder._health.tracked_events[active.event_id] = active

        recorder._accept_official_quotes((post_end,))

        assert store.count("prediction_market_quotes") == 0
        assert recorder.health().official_quote_last_updates == {}
