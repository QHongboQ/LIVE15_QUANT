from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from live15_quant.kalshi_gateway.canonical_ws import canonical_from_sdk
from live15_quant.kalshi_gateway.recorder_consumer import (
    RecorderDomainWriteError,
    RecorderMarketDataConsumer,
    RecorderStoreDomainWriter,
)
from live15_quant.kalshi_gateway.recorder_provider import (
    RecorderProviderState,
    SdkRecorderMarketDataProvider,
)
from live15_quant.kalshi_ws import KalshiUnsynchronizedBookError
from live15_quant.models import Asset
from live15_quant.storage import RecorderStore

NOW = datetime(2026, 8, 25, tzinfo=UTC)
TICKER = "KXBTC15M-TEST"


def _snapshot(
    sequence: int,
    *,
    ticker: str = TICKER,
    subscription_id: int = 1,
    market_id: str = "market-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        type="orderbook_snapshot",
        sid=subscription_id,
        seq=sequence,
        msg=SimpleNamespace(
            market_ticker=ticker,
            market_id=market_id,
            yes={Decimal("0.40"): Decimal("1")},
            no={Decimal("0.50"): Decimal("1")},
        ),
    )


def _delta(
    sequence: int,
    *,
    ticker: str = TICKER,
    subscription_id: int = 1,
    market_id: str = "market-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        type="orderbook_delta",
        sid=subscription_id,
        seq=sequence,
        msg=SimpleNamespace(
            market_ticker=ticker,
            market_id=market_id,
            side="yes",
            price=Decimal("0.41"),
            delta=Decimal("1"),
        ),
    )


def _canonical(
    message: object,
    *,
    asset_by_ticker: dict[str, Asset] | None = None,
    connection_id: str = "provider-test",
):
    return canonical_from_sdk(
        message,
        asset_by_ticker=asset_by_ticker or {TICKER: Asset.BTC},
        connection_id=connection_id,
        received_at=NOW,
    )


@pytest.mark.asyncio
async def test_sdk_provider_has_one_reliability_coordinator_and_emits_authoritative_snapshot() -> (
    None
):
    provider = SdkRecorderMarketDataProvider.isolated(
        asset_by_ticker={TICKER: Asset.BTC},
        connection_id="provider-test",
        stale_seconds=10,
    )
    await provider.start()
    result = provider.accept(_canonical(_snapshot(1)))
    assert result.authoritative is True
    assert result.book is not None
    assert result.state is RecorderProviderState.SYNCHRONIZED
    assert provider.state is RecorderProviderState.SYNCHRONIZED
    queued = await anext(provider.events())
    assert queued == result
    await provider.stop()


@pytest.mark.asyncio
async def test_gap_quarantines_until_fresh_snapshot_and_never_exposes_stale_book() -> None:
    provider = SdkRecorderMarketDataProvider.isolated(
        asset_by_ticker={TICKER: Asset.BTC},
        connection_id="provider-test",
        stale_seconds=10,
    )
    await provider.start()
    provider.accept(_canonical(_snapshot(1)))
    gap = provider.accept(_canonical(_delta(3)))
    assert gap.authoritative is False
    assert gap.book is None
    assert provider.state is RecorderProviderState.QUARANTINED
    with pytest.raises(KalshiUnsynchronizedBookError):
        provider._adapter.book(Asset.BTC)
    recovered = provider.accept(_canonical(_snapshot(4)))
    assert recovered.authoritative is True
    assert recovered.book is not None
    assert provider.state is RecorderProviderState.SYNCHRONIZED
    await provider.stop()


@pytest.mark.asyncio
async def test_reconnect_replaces_session_and_requires_full_fresh_snapshot_set() -> None:
    eth_ticker = "KXETH15M-TEST"
    universe = {TICKER: Asset.BTC, eth_ticker: Asset.ETH}
    provider = SdkRecorderMarketDataProvider.isolated(
        asset_by_ticker=universe,
        connection_id="provider-session-1",
        stale_seconds=10,
    )
    await provider.start()
    provider.accept(
        _canonical(_snapshot(1), asset_by_ticker=universe, connection_id="provider-session-1")
    )
    provider.accept(
        _canonical(
            _snapshot(2, ticker=eth_ticker, market_id="market-eth"),
            asset_by_ticker=universe,
            connection_id="provider-session-1",
        )
    )
    assert provider.synchronized_count == 2

    await provider.begin_reconnect_session(
        connection_id="provider-session-2",
        observed_at=NOW,
        old_state="streaming",
    )
    assert provider.connection_id == "provider-session-2"
    assert provider.synchronized_count == 0
    assert provider.gap_count == 0
    assert provider._adapter.books == {}

    # A new-session delta never revives an old book and opens one bounded
    # global recovery incident rather than a gap per rejected delta.
    delta = provider.accept(
        _canonical(
            _delta(1, subscription_id=2),
            asset_by_ticker=universe,
            connection_id="provider-session-2",
        )
    )
    assert delta.authoritative is False
    assert provider.gap_count == 2

    first_snapshot = provider.accept(
        _canonical(
            _snapshot(2, subscription_id=2),
            asset_by_ticker=universe,
            connection_id="provider-session-2",
        )
    )
    assert first_snapshot.authoritative is False
    assert provider.synchronized_count == 0

    recovered = provider.accept(
        _canonical(
            _snapshot(3, ticker=eth_ticker, subscription_id=2, market_id="market-eth"),
            asset_by_ticker=universe,
            connection_id="provider-session-2",
        )
    )
    assert recovered.authoritative is True
    assert provider.synchronized_count == 2
    assert provider.gap_count == 0

    # The Provider queue is the host's callback-to-consumer boundary.  It
    # remains alive across the SDK session replacement and preserves order.
    stream = provider.events()
    delivered = [await anext(stream) for _ in range(5)]
    assert [item.canonical.connection_id for item in delivered] == [
        "provider-session-1",
        "provider-session-1",
        "provider-session-2",
        "provider-session-2",
        "provider-session-2",
    ]
    await provider.stop()


@pytest.mark.asyncio
async def test_stop_closes_event_stream_without_background_task() -> None:
    provider = SdkRecorderMarketDataProvider.isolated(
        asset_by_ticker={TICKER: Asset.BTC},
        connection_id="provider-test",
        stale_seconds=10,
    )
    await provider.start()
    stream = provider.events()
    waiter = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await provider.stop()
    with pytest.raises(StopAsyncIteration):
        await waiter
    assert provider.state is RecorderProviderState.STOPPED


class _Writer:
    def __init__(self, *, fail: bool = False) -> None:
        self.events = []
        self.fail = fail

    def persist_market_data_event(self, event) -> None:
        if self.fail:
            raise RuntimeError("injected history failure")
        self.events.append(event)


@pytest.mark.asyncio
async def test_consumer_advances_checkpoint_only_after_atomic_writer_succeeds() -> None:
    provider = SdkRecorderMarketDataProvider.isolated(
        asset_by_ticker={TICKER: Asset.BTC},
        connection_id="provider-test",
        stale_seconds=10,
    )
    await provider.start()
    event = provider.accept(_canonical(_snapshot(1)))
    writer = _Writer()
    consumer = RecorderMarketDataConsumer(writer)
    assert consumer.consume(event) is True
    assert len(writer.events) == 1
    assert consumer.checkpoint is not None
    assert consumer.checkpoint.sequence == 1
    assert consumer.consume(event) is False
    assert len(writer.events) == 1


@pytest.mark.asyncio
async def test_consumer_never_advances_checkpoint_across_writer_failure() -> None:
    provider = SdkRecorderMarketDataProvider.isolated(
        asset_by_ticker={TICKER: Asset.BTC},
        connection_id="provider-test",
        stale_seconds=10,
    )
    await provider.start()
    event = provider.accept(_canonical(_snapshot(1)))
    consumer = RecorderMarketDataConsumer(_Writer(fail=True))
    with pytest.raises(RecorderDomainWriteError):
        consumer.consume(event)
    assert consumer.checkpoint is None


@pytest.mark.asyncio
async def test_failed_sdk_batch_does_not_notify_durable_progress() -> None:
    class BatchWriter(_Writer):
        def persist_market_data_events(self, _events: tuple[object, ...]) -> None:
            if self.fail:
                raise RuntimeError("injected batch failure")

    provider = SdkRecorderMarketDataProvider.isolated(
        asset_by_ticker={TICKER: Asset.BTC},
        connection_id="provider-test",
        stale_seconds=10,
    )
    await provider.start()
    event = provider.accept(_canonical(_snapshot(1)))
    committed: list[tuple[object, ...]] = []
    consumer = RecorderMarketDataConsumer(
        BatchWriter(fail=True),
        on_committed=lambda events: committed.append(events),
    )

    assert consumer.consume(event) is True
    with pytest.raises(RecorderDomainWriteError):
        consumer.flush()
    assert committed == []
    assert consumer.checkpoint is None


@pytest.mark.asyncio
async def test_sdk_provider_writes_an_isolated_recorder_store_via_neutral_writer(tmp_path) -> None:
    provider = SdkRecorderMarketDataProvider.isolated(
        asset_by_ticker={TICKER: Asset.BTC},
        connection_id="provider-test",
        stale_seconds=10,
    )
    await provider.start()
    event = provider.accept(_canonical(_snapshot(1)))
    with RecorderStore(tmp_path / "isolated.sqlite3") as store:
        consumer = RecorderMarketDataConsumer(RecorderStoreDomainWriter(store))
        assert consumer.consume(event) is True
        assert consumer.flush() == 1
        assert store.count("kalshi_ws_orderbook_events") == 1
        assert consumer.consume(event) is False
        assert store.count("kalshi_ws_orderbook_events") == 1


@pytest.mark.asyncio
async def test_idle_flush_persists_partial_sdk_batch_and_stops_cleanly(tmp_path) -> None:
    provider = SdkRecorderMarketDataProvider.isolated(
        asset_by_ticker={TICKER: Asset.BTC},
        connection_id="provider-test",
        stale_seconds=10,
    )
    await provider.start()
    event = provider.accept(_canonical(_snapshot(1)))
    with RecorderStore(tmp_path / "idle.sqlite3") as store:
        consumer = RecorderMarketDataConsumer(
            RecorderStoreDomainWriter(store), flush_interval_seconds=0.01
        )
        stop_event = asyncio.Event()
        timer = asyncio.create_task(consumer.run_idle_flush(stop_event))
        assert consumer.consume(event) is True
        await asyncio.sleep(0.03)
        assert store.count("kalshi_ws_orderbook_events") == 1
        assert consumer.pending_count == 0
        stop_event.set()
        await timer
        assert consumer.close() == 0
