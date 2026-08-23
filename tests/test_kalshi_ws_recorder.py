from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from live15_quant.config import Settings
from live15_quant.gaps import GapSource, configured_streams, detect_gaps
from live15_quant.kalshi_ws import (
    KalshiBookSide,
    KalshiBookSyncStatus,
    KalshiCommandAcknowledged,
    KalshiOrderBookDelta,
    KalshiOrderBookSnapshot,
    KalshiSubscribed,
    KalshiSubscriptionCommand,
    KalshiUnsynchronizedBookError,
    KalshiWsRuntimeState,
    SynchronizedKalshiOrderBook,
)
from live15_quant.models import Asset, OrderBookLevel
from live15_quant.native_recorder import KalshiNativeRecorder
from live15_quant.storage import RecorderStorageError, RecorderStore
from tests.test_kalshi_lifecycle import NOW
from tests.test_native_recorder import FakeDiscovery, FakeQuotes, OneTickStream, discovery_for


def test_forward_shadow_checkpoint_is_predecision_bounded_and_idempotent(tmp_path) -> None:
    discovery = discovery_for(Asset.BTC)
    with RecorderStore(tmp_path / "raw.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(recorder_health_path=tmp_path / "health.json"),
            store,
            discovery=FakeDiscovery((discovery,)),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            now=lambda: NOW,
        )
        recorder._accept_discovery(discovery)
        market = recorder._health.current[Asset.BTC]
        decision = market.window_end - timedelta(seconds=60)
        book = SynchronizedKalshiOrderBook(
            connection_id="connection",
            subscription_id=1,
            sequence=10,
            ticker=market.ticker,
            market_id="market",
            yes_bids=(OrderBookLevel(Decimal("0.40"), Decimal("2")),),
            no_bids=(OrderBookLevel(Decimal("0.50"), Decimal("3")),),
            source_timestamp=None,
            received_timestamp=decision - timedelta(seconds=1),
        )
        recorder._capture_forward_shadow_checkpoint(Asset.BTC, book)
        recorder._capture_forward_shadow_checkpoint(Asset.BTC, book)
        assert store.count("kalshi_ws_book_checkpoints") == 1
        future_book = replace(book, sequence=11, received_timestamp=decision + timedelta(seconds=1))
        recorder._capture_forward_shadow_checkpoint(Asset.BTC, future_book)
        assert store.count("kalshi_ws_book_checkpoints") == 1


class FakeProductionWs:
    def __init__(self) -> None:
        self.commands: list[dict[str, object]] = []
        self.closed = False
        self.resync_complete = asyncio.Event()
        self.gap_observed = asyncio.Event()
        self.allow_resync = asyncio.Event()
        self.diagnostics = SimpleNamespace(
            reconnects=0,
            receive_queue_high_watermark=3,
            last_message_received_at=NOW,
        )
        self.reconnect_requests = 0

    def set_reconnect_tickers(self, tickers) -> None:
        self.tickers = tuple(tickers)

    async def send_command(self, command: KalshiSubscriptionCommand) -> None:
        self.commands.append(dict(command.as_object()))

    async def close(self) -> None:
        self.closed = True

    async def request_reconnect(self) -> None:
        self.reconnect_requests += 1

    async def messages(self, tickers):
        exact = tuple(tickers)
        assert len(exact) == len(Asset)
        yield KalshiSubscribed(1, 2, "orderbook_delta")
        for sequence, ticker in enumerate(exact, 1):
            yield self._snapshot(ticker, sequence, 0)
        yield self._delta(exact[0], 12)
        assert "get_snapshot" in self._actions()
        self.gap_observed.set()
        await self.allow_resync.wait()
        for offset, ticker in enumerate(exact):
            yield self._snapshot(ticker, 20 + offset, 2)
        self.resync_complete.set()
        await asyncio.Event().wait()

    def _actions(self) -> list[str]:
        actions: list[str] = []
        for command in self.commands:
            params = command.get("params")
            if isinstance(params, dict) and isinstance(params.get("action"), str):
                actions.append(params["action"])
        return actions

    @staticmethod
    def _delta(ticker: str, sequence: int) -> KalshiOrderBookDelta:
        received = NOW + timedelta(seconds=1)
        return KalshiOrderBookDelta(
            connection_id="connection-1",
            subscription_id=2,
            sequence=sequence,
            ticker=ticker,
            market_id=f"market:{ticker}",
            side=KalshiBookSide.YES,
            price=Decimal("0.50"),
            quantity_delta=Decimal("1"),
            source_timestamp=received,
            socket_received_timestamp=received,
            parse_timestamp=received + timedelta(microseconds=10),
            socket_received_monotonic_ns=1_000_000,
            enqueue_timestamp=received + timedelta(microseconds=5),
            enqueue_monotonic_ns=1_005_000,
        )

    @staticmethod
    def _snapshot(ticker: str, sequence: int, seconds: int) -> KalshiOrderBookSnapshot:
        received = NOW + timedelta(seconds=seconds)
        return KalshiOrderBookSnapshot(
            connection_id="connection-1",
            subscription_id=2,
            sequence=sequence,
            ticker=ticker,
            market_id=f"market:{ticker}",
            yes_bids=(OrderBookLevel(Decimal("0.50"), Decimal("10")),),
            no_bids=(OrderBookLevel(Decimal("0.49"), Decimal("11")),),
            source_timestamp=received,
            socket_received_timestamp=received,
            parse_timestamp=received + timedelta(microseconds=10),
            socket_received_monotonic_ns=1_000_000 + sequence * 1_000,
            enqueue_timestamp=received + timedelta(microseconds=5),
            enqueue_monotonic_ns=1_005_000 + sequence * 1_000,
        )


class RolloverProductionWs(FakeProductionWs):
    def __init__(self) -> None:
        super().__init__()
        self.initial_synchronized = asyncio.Event()
        self.rollover = asyncio.Event()
        self.successor_requested = asyncio.Event()
        self.allow_successor = asyncio.Event()
        self.finished = asyncio.Event()
        self.successor: str | None = None

    async def send_command(self, command: KalshiSubscriptionCommand) -> None:
        await super().send_command(command)
        if (
            command.as_object().get("cmd") == "update_subscription"
            and self._actions()[-1] == "add_markets"
        ):
            self.successor_requested.set()
            await self.allow_successor.wait()

    async def messages(self, tickers):
        exact = tuple(tickers)
        yield KalshiSubscribed(1, 2, "orderbook_delta")
        for sequence, ticker in enumerate(exact, 1):
            yield self._snapshot(ticker, sequence, 0)
        self.initial_synchronized.set()
        await self.rollover.wait()
        assert self.successor is not None
        yield self._delta(exact[0], 11)
        assert self._actions()[-1] == "add_markets"
        yield self._snapshot(self.successor, 12, 2)
        assert self._actions()[-2:] == ["add_markets", "delete_markets"]
        delete_request = self.commands[-1]
        yield KalshiCommandAcknowledged(
            connection_id="connection-1",
            request_id=int(delete_request["id"]),
            subscription_id=2,
            sequence=13,
            market_tickers=(exact[0],),
            socket_received_timestamp=NOW + timedelta(seconds=2),
            parse_timestamp=NOW + timedelta(seconds=2, microseconds=10),
        )
        self.finished.set()
        await asyncio.Event().wait()


class RecoveringProductionWs(FakeProductionWs):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0
        self.first_outage = asyncio.Event()
        self.recovered = asyncio.Event()

    async def messages(self, tickers):
        self.attempts += 1
        if self.attempts == 1:
            self.first_outage.set()
            return
        exact = tuple(tickers)
        yield KalshiSubscribed(1, 2, "orderbook_delta")
        for sequence, ticker in enumerate(exact, 1):
            yield self._snapshot(ticker, sequence, 1)
        self.recovered.set()
        await asyncio.Event().wait()


class InvariantRecoveringProductionWs(FakeProductionWs):
    """Inject one impossible depth update, then provide official fresh snapshots."""

    async def messages(self, tickers):
        exact = tuple(tickers)
        yield KalshiSubscribed(1, 2, "orderbook_delta")
        for sequence, ticker in enumerate(exact, 1):
            yield self._snapshot(ticker, sequence, 0)
        yield replace(
            self._delta(exact[0], len(exact) + 1),
            quantity_delta=Decimal("-11"),
        )
        assert "get_snapshot" in self._actions()
        self.gap_observed.set()
        await self.allow_resync.wait()
        for offset, ticker in enumerate(exact):
            yield self._snapshot(ticker, 20 + offset, 2)
        self.resync_complete.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_recorder_uses_only_synchronized_ws_and_closes_sequence_gap(tmp_path) -> None:
    source = FakeProductionWs()
    discoveries = tuple(discovery_for(asset) for asset in Asset)
    with RecorderStore(tmp_path / "raw.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                enable_kalshi_production_websocket=True,
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(discoveries),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            kalshi_ws_factory=lambda: source,
            now=lambda: NOW,
        )
        for item in discoveries:
            recorder._accept_discovery(item)
        task = asyncio.create_task(recorder._record_kalshi_ws())
        await asyncio.wait_for(source.gap_observed.wait(), 1)
        assert recorder.health().kalshi_ws_connection_state is KalshiWsRuntimeState.UNSYNCHRONIZED
        current_ticker = next(iter(recorder._health.current.values())).ticker
        with pytest.raises(KalshiUnsynchronizedBookError):
            recorder.synchronized_kalshi_ws_book(current_ticker)
        assert len(
            tuple(gap for gap in store.active_data_gaps() if gap.source is GapSource.KALSHI_WS)
        ) == len(Asset)
        source.allow_resync.set()
        await asyncio.wait_for(source.resync_complete.wait(), 1)
        recorder._flush_kalshi_ws_pending()
        health = recorder.health()
        assert health.kalshi_ws_connection_state is KalshiWsRuntimeState.SYNCHRONIZED
        assert len(health.kalshi_ws_synchronized_markets) == len(Asset)
        assert health.kalshi_ws_seq_gaps == 1
        assert health.kalshi_ws_resync_count == 1
        assert health.kalshi_ws_queue_high_watermark == 3
        assert store.count("kalshi_ws_orderbook_events") == 21
        assert store.count("kalshi_prediction_quotes") == 0
        assert not tuple(
            gap for gap in store.active_data_gaps() if gap.source is GapSource.KALSHI_WS
        )
        recovered = tuple(
            gap
            for gap in store.replay_data_gaps(source=GapSource.KALSHI_WS)
            if gap.source is GapSource.KALSHI_WS and gap.recovered
        )
        assert len(recovered) == len(Asset)
        for market in recorder._health.current.values():
            assert recorder.synchronized_kalshi_ws_book(market.ticker).ticker == market.ticker
        source.diagnostics.transport_state = KalshiWsRuntimeState.RECONNECTING
        with pytest.raises(KalshiUnsynchronizedBookError):
            recorder.synchronized_kalshi_ws_book(current_ticker)
        source.diagnostics.transport_state = KalshiWsRuntimeState.WAITING_SNAPSHOT
        source.diagnostics.receive_queue_dropped = 1
        with pytest.raises(KalshiUnsynchronizedBookError):
            recorder.synchronized_kalshi_ws_book(current_ticker)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_transport_stall_invalidates_all_books_and_requests_reconnect(tmp_path) -> None:
    source = FakeProductionWs()
    discoveries = tuple(discovery_for(asset) for asset in Asset)
    observed = NOW + timedelta(seconds=11)
    with RecorderStore(tmp_path / "transport-stall.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                enable_kalshi_production_websocket=True,
                kalshi_websocket_stale_seconds=10,
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(discoveries),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            kalshi_ws_factory=lambda: source,
            now=lambda: observed,
        )
        for item in discoveries:
            recorder._accept_discovery(item)
        task = asyncio.create_task(recorder._record_kalshi_ws())
        await asyncio.wait_for(source.gap_observed.wait(), 1)
        source.allow_resync.set()
        await asyncio.wait_for(source.resync_complete.wait(), 1)
        source.diagnostics.last_message_received_at = NOW

        assert await recorder._enforce_kalshi_ws_liveness(observed)
        assert source.reconnect_requests == 1
        assert recorder.health().kalshi_ws_connection_state is KalshiWsRuntimeState.RECONNECTING
        # A recovered transport timestamp alone cannot revive a cleared book; an
        # official fresh snapshot is still required before any consumption.
        source.diagnostics.last_message_received_at = observed
        for market in recorder._health.current.values():
            with pytest.raises(KalshiUnsynchronizedBookError):
                recorder.synchronized_kalshi_ws_book(market.ticker)
        assert len(
            tuple(gap for gap in store.active_data_gaps() if gap.source is GapSource.KALSHI_WS)
        ) == len(Asset)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_partial_initial_snapshot_set_has_bounded_reconnect(tmp_path) -> None:
    source = FakeProductionWs()
    discoveries = tuple(discovery_for(asset) for asset in Asset)
    clock = [100.0]
    with RecorderStore(tmp_path / "partial-snapshot-stall.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                enable_kalshi_production_websocket=True,
                kalshi_websocket_stale_seconds=10,
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(discoveries),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            kalshi_ws_factory=lambda: source,
            now=lambda: NOW,
            monotonic=lambda: clock[0],
        )
        for item in discoveries:
            recorder._accept_discovery(item)
        recorder._health.kalshi_ws_state = KalshiWsRuntimeState.WAITING_SNAPSHOT
        recorder._health.kalshi_ws_synchronized[Asset.BTC] = discoveries[0].current.ticker
        recorder._kalshi_ws_waiting_since_monotonic = clock[0]
        source.diagnostics.last_message_received_at = NOW

        clock[0] += 9.0
        assert not await recorder._enforce_kalshi_ws_liveness(NOW)
        assert source.reconnect_requests == 0

        clock[0] += 2.0
        assert await recorder._enforce_kalshi_ws_liveness(NOW)
        assert source.reconnect_requests == 1
        assert recorder.health().kalshi_ws_connection_state is KalshiWsRuntimeState.RECONNECTING
        assert not recorder._health.kalshi_ws_synchronized

        # A close in progress cannot trigger another request every monitor tick.
        clock[0] += 60.0
        assert not await recorder._enforce_kalshi_ws_liveness(NOW)
        assert source.reconnect_requests == 1


@pytest.mark.asyncio
async def test_unchanged_book_remains_usable_when_transport_is_fresh(tmp_path) -> None:
    source = FakeProductionWs()
    discoveries = tuple(discovery_for(asset) for asset in Asset)
    observed = NOW + timedelta(seconds=11)
    with RecorderStore(tmp_path / "quiet-book.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                enable_kalshi_production_websocket=True,
                kalshi_websocket_stale_seconds=10,
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(discoveries),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            kalshi_ws_factory=lambda: source,
            now=lambda: observed,
        )
        for item in discoveries:
            recorder._accept_discovery(item)
        task = asyncio.create_task(recorder._record_kalshi_ws())
        await asyncio.wait_for(source.gap_observed.wait(), 1)
        source.allow_resync.set()
        await asyncio.wait_for(source.resync_complete.wait(), 1)
        source.diagnostics.last_message_received_at = observed

        health = recorder.health()
        assert health.kalshi_ws_connection_state is KalshiWsRuntimeState.SYNCHRONIZED
        assert not [value for value in health.stale_sources if value.startswith("kalshi_ws:")]
        ticker = next(iter(recorder._health.current.values())).ticker
        assert recorder.synchronized_kalshi_ws_book(ticker).ticker == ticker
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_recorder_isolates_impossible_depth_and_recovers_all_books(tmp_path) -> None:
    source = InvariantRecoveringProductionWs()
    discoveries = tuple(discovery_for(asset) for asset in Asset)
    with RecorderStore(tmp_path / "invariant-recovery.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                enable_kalshi_production_websocket=True,
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(discoveries),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            kalshi_ws_factory=lambda: source,
            now=lambda: NOW,
        )
        for item in discoveries:
            recorder._accept_discovery(item)
        task = asyncio.create_task(recorder._record_kalshi_ws())
        await asyncio.wait_for(source.gap_observed.wait(), 1)

        assert not task.done()
        assert recorder.health().kalshi_ws_connection_state is KalshiWsRuntimeState.UNSYNCHRONIZED
        assert len(store.active_data_gaps()) == len(Asset)
        for market in recorder._health.current.values():
            with pytest.raises(KalshiUnsynchronizedBookError):
                recorder.synchronized_kalshi_ws_book(market.ticker)

        source.allow_resync.set()
        await asyncio.wait_for(source.resync_complete.wait(), 1)
        recorder._flush_kalshi_ws_pending()

        assert not task.done()
        assert recorder.health().kalshi_ws_connection_state is KalshiWsRuntimeState.SYNCHRONIZED
        assert not store.active_data_gaps()
        assert len(recorder.health().kalshi_ws_synchronized_markets) == len(Asset)
        assert len(tuple(gap for gap in store.replay_data_gaps() if gap.recovered)) == len(Asset)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_ws_storage_preserves_enqueue_timing_without_float(tmp_path, monkeypatch) -> None:
    from live15_quant import storage as storage_module

    message = FakeProductionWs._snapshot("KXBTC15M-TEST", 1, 0)
    monkeypatch.setattr(storage_module.time, "perf_counter_ns", lambda: 2_000_000)
    with RecorderStore(tmp_path / "timing.sqlite3") as store:
        store.append_kalshi_ws_orderbook_event(
            message,
            sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED,
        )
        record = next(store.replay_kalshi_ws_orderbook_events("connection-1", 2))
    assert record.enqueue_timestamp == NOW + timedelta(microseconds=5)
    assert record.receive_enqueue_latency_ms == Decimal("0.005")
    assert record.receive_persist_latency_ms == Decimal("0.999")
    assert json.dumps(record.provenance) == '"kalshi_ws"'


@pytest.mark.asyncio
async def test_recorder_rollover_adds_successor_before_removing_predecessor(tmp_path) -> None:
    source = RolloverProductionWs()
    discoveries = tuple(discovery_for(asset) for asset in Asset)
    with RecorderStore(tmp_path / "rollover.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                enable_kalshi_production_websocket=True,
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(discoveries),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            kalshi_ws_factory=lambda: source,
            now=lambda: NOW,
        )
        for item in discoveries:
            recorder._accept_discovery(item)
        task = asyncio.create_task(recorder._record_kalshi_ws())
        await asyncio.wait_for(source.initial_synchronized.wait(), 1)
        predecessor = recorder._health.current[Asset.BTC]
        successor_event_ticker = f"{predecessor.event_ticker}-NEXT"
        successor_ticker = f"{successor_event_ticker}-00"
        source.successor = successor_ticker
        recorder._health.current[Asset.BTC] = replace(
            predecessor,
            ticker=successor_ticker,
            event_ticker=successor_event_ticker,
            window_start=predecessor.window_end,
            window_end=predecessor.window_end + timedelta(minutes=15),
        )
        source.rollover.set()
        await asyncio.wait_for(source.successor_requested.wait(), 1)
        assert recorder.health().kalshi_ws_connection_state is KalshiWsRuntimeState.WAITING_SNAPSHOT
        with pytest.raises(KalshiUnsynchronizedBookError):
            recorder.synchronized_kalshi_ws_book(predecessor.ticker)
        source.allow_successor.set()
        await asyncio.wait_for(source.finished.wait(), 1)
        assert recorder.health().kalshi_ws_synchronized_markets[Asset.BTC] == successor_ticker
        assert recorder.synchronized_kalshi_ws_book(successor_ticker).ticker == successor_ticker
        with pytest.raises(KalshiUnsynchronizedBookError):
            recorder.synchronized_kalshi_ws_book(predecessor.ticker)
        assert predecessor.ticker not in source.tickers
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_historical_ws_gap_detection_uses_exact_ticker_asset_join(tmp_path) -> None:
    configured = Settings(kalshi_websocket_stale_seconds=1)
    market = discovery_for(Asset.BTC).current
    assert market is not None
    first = replace(
        FakeProductionWs._snapshot(market.ticker, 1, 0),
        market_id="market:first",
    )
    second = replace(
        FakeProductionWs._snapshot(market.ticker, 1, 5),
        connection_id="connection-2",
        market_id="market:first",
    )
    with RecorderStore(tmp_path / "historical.sqlite3") as store:
        store.append_kalshi_market(market)
        store.append_kalshi_ws_orderbook_event(
            first, sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED
        )
        store.append_kalshi_ws_orderbook_event(
            second, sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED
        )
        assert store.latest_gap_stream_timestamp(
            GapSource.KALSHI_WS, Asset.BTC, market.series
        ) == NOW + timedelta(seconds=5)
        stream = next(
            item
            for item in configured_streams(configured)
            if item.source is GapSource.KALSHI_WS and item.asset is Asset.BTC
        )
        detected = detect_gaps(
            store._connection,
            (stream,),
            start=NOW,
            end=NOW + timedelta(seconds=6),
            detected_at=NOW + timedelta(seconds=6),
            immutable_snapshot=True,
        )
    assert len(detected) == 1
    assert detected[0].gap_start == NOW
    assert detected[0].gap_end == NOW + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_ws_transport_end_retries_without_stopping_other_recorder_work(tmp_path) -> None:
    source = RecoveringProductionWs()
    discoveries = tuple(discovery_for(asset) for asset in Asset)
    with RecorderStore(tmp_path / "reconnect.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                enable_kalshi_production_websocket=True,
                reconnect_delay_seconds=0.001,
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(discoveries),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            kalshi_ws_factory=lambda: source,
            now=lambda: NOW,
        )
        for item in discoveries:
            recorder._accept_discovery(item)
        task = asyncio.create_task(recorder._record_kalshi_ws())
        await asyncio.wait_for(source.first_outage.wait(), 1)
        await asyncio.wait_for(source.recovered.wait(), 1)
        assert not task.done()
        health = recorder.health()
        assert health.kalshi_ws_connection_state is KalshiWsRuntimeState.SYNCHRONIZED
        assert "kalshi_ws" not in health.source_failures
        assert health.fatal_task is None
        assert source.attempts == 2
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_ws_persistence_failure_remains_fail_loud(tmp_path, monkeypatch) -> None:
    source = FakeProductionWs()
    discoveries = tuple(discovery_for(asset) for asset in Asset)
    with RecorderStore(tmp_path / "failure.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                enable_kalshi_production_websocket=True,
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(discoveries),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            kalshi_ws_factory=lambda: source,
            now=lambda: NOW,
        )
        for item in discoveries:
            recorder._accept_discovery(item)

        def fail_persistence(*_args, **_kwargs):
            raise RecorderStorageError("injected durable write failure")

        task = asyncio.create_task(recorder._record_kalshi_ws())
        await asyncio.wait_for(source.gap_observed.wait(), 1)
        monkeypatch.setattr(store, "append_kalshi_ws_orderbook_event_batch", fail_persistence)
        with pytest.raises(RecorderStorageError, match="durable write failure"):
            recorder._flush_kalshi_ws_pending()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
