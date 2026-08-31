from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from kalshi.ws.backpressure import MessageQueue, OverflowStrategy
from kalshi.ws.channels import Subscription, SubscriptionManager
from websockets.exceptions import ConcurrencyError

from live15_quant.kalshi_gateway.canonical_ws import canonical_from_sdk
from live15_quant.kalshi_gateway.production_recorder_host import SdkProductionRecorderHost
from live15_quant.kalshi_gateway.recorder_provider import (
    RecorderProviderState,
    SdkRecorderMarketDataProvider,
)
from live15_quant.models import Asset


@pytest.mark.asyncio
async def test_current_active_universe_starts_sdk_session_when_commodities_are_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed commodity subset must not block the current active WS universe."""

    closed_assets = {Asset.GOLD, Asset.SILVER, Asset.WTI_OIL}
    active_universe = {
        asset: f"KX{asset.value.upper().replace(' ', '-')}-CURRENT"
        for asset in Asset
        if asset not in closed_assets
    }
    subscriptions: list[tuple[str, tuple[str, ...]]] = []
    stop = asyncio.Event()

    class PendingStream:
        def __aiter__(self) -> PendingStream:
            return self

        async def __anext__(self) -> object:
            await asyncio.Event().wait()
            raise StopAsyncIteration

    class Session:
        async def subscribe_ticker(self, *, tickers, maxsize):
            subscriptions.append(("ticker", tuple(tickers)))
            assert maxsize == 2_000
            return PendingStream()

        async def subscribe_market_lifecycle(self, *, tickers, maxsize):
            subscriptions.append(("lifecycle", tuple(tickers)))
            assert maxsize == 1_000
            return PendingStream()

        async def subscribe_orderbook_delta(self, *, tickers, maxsize):
            subscriptions.append(("orderbook", tuple(tickers)))
            assert maxsize == 10_000
            stop.set()
            return PendingStream()

    class Context:
        async def __aenter__(self) -> Session:
            return Session()

        async def __aexit__(self, *_args) -> None:
            return None

    class WebSocket:
        def on(self, _event):
            def register(handler):
                return handler

            return register

        def connect(self) -> Context:
            return Context()

    class Gateway:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def build(self, **_kwargs) -> WebSocket:
            return WebSocket()

    monkeypatch.setattr(
        "live15_quant.kalshi_gateway.production_recorder_host.production_credentials",
        lambda _settings: object(),
    )
    monkeypatch.setattr(
        "live15_quant.kalshi_gateway.production_recorder_host.KalshiWebSocketGateway",
        Gateway,
    )

    host = object.__new__(SdkProductionRecorderHost)
    host._settings = SimpleNamespace(
        kalshi_websocket_stale_seconds=10.0,
        kalshi_websocket_read_timeout_seconds=5.0,
    )
    host._store = SimpleNamespace(bounded_row_count_estimates=lambda: {})
    host._on_committed = lambda _events: None
    host._on_transport_state = lambda _state, _observed_at: None
    host._provider = None
    host._consumer = None
    host._reconnect_count = 0
    host._final_summary = None
    host._reconnect_requested = asyncio.Event()

    await host._run_session(active_universe, stop)

    expected_tickers = tuple(sorted(active_universe.values()))
    assert subscriptions == [
        ("ticker", expected_tickers),
        ("lifecycle", expected_tickers),
        ("orderbook", expected_tickers),
    ]
    assert len(expected_tickers) == 7


@pytest.mark.asyncio
async def test_explicit_sdk_reconnect_replaces_one_session_without_stopping_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    universe = {Asset.BTC: "KXBTC15M-CURRENT"}
    stop = asyncio.Event()
    first_session_ready = asyncio.Event()
    session_count = 0
    reconnect_states: list[str] = []

    class PendingStream:
        def __aiter__(self) -> PendingStream:
            return self

        async def __anext__(self) -> object:
            await asyncio.Event().wait()
            raise StopAsyncIteration

    class Session:
        async def subscribe_ticker(self, *, tickers, maxsize):
            return PendingStream()

        async def subscribe_market_lifecycle(self, *, tickers, maxsize):
            return PendingStream()

        async def subscribe_orderbook_delta(self, *, tickers, maxsize):
            if session_count == 1:
                first_session_ready.set()
            else:
                stop.set()
            return PendingStream()

    class Context:
        async def __aenter__(self) -> Session:
            return Session()

        async def __aexit__(self, *_args) -> None:
            return None

    class WebSocket:
        def on(self, _event):
            def register(handler):
                return handler

            return register

        def connect(self) -> Context:
            nonlocal session_count
            session_count += 1
            return Context()

    class Gateway:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def build(self, **_kwargs) -> WebSocket:
            return WebSocket()

    monkeypatch.setattr(
        "live15_quant.kalshi_gateway.production_recorder_host.production_credentials",
        lambda _settings: object(),
    )
    monkeypatch.setattr(
        "live15_quant.kalshi_gateway.production_recorder_host.KalshiWebSocketGateway",
        Gateway,
    )

    host = SdkProductionRecorderHost(
        settings=SimpleNamespace(
            kalshi_websocket_stale_seconds=10.0,
            kalshi_websocket_read_timeout_seconds=5.0,
        ),
        store=SimpleNamespace(bounded_row_count_estimates=lambda: {}),
        universe=lambda: universe,
        on_committed=lambda _events: None,
        on_transport_state=lambda state, _observed_at: reconnect_states.append(state),
    )
    run_task = asyncio.create_task(host.run(stop))
    await asyncio.wait_for(first_session_ready.wait(), 1)
    assert host.running

    await host.request_reconnect()
    assert host.running
    await asyncio.wait_for(run_task, 1)

    assert session_count == 2
    assert reconnect_states == ["reconnecting"]
    assert not host.running
    await asyncio.sleep(0)
    assert session_count == 2


@pytest.mark.asyncio
async def test_rollover_replaces_session_without_a_second_websocket_reader() -> None:
    """The pinned SDK race is real, while the host rollover cannot trigger it."""

    original = {Asset.BTC: "KXBTC15M-OLD"}
    replacement = {Asset.BTC: "KXBTC15M-NEW"}
    host = object.__new__(SdkProductionRecorderHost)
    host._universe = lambda: replacement
    stop = asyncio.Event()
    changed = asyncio.Event()

    class SingleReaderConnection:
        def __init__(self) -> None:
            self.reader_started = asyncio.Event()
            self.release_reader = asyncio.Event()
            self.reading = False

        async def recv(self) -> None:
            if self.reading:
                raise ConcurrencyError(
                    "cannot call recv while another coroutine is already running recv"
                )
            self.reading = True
            self.reader_started.set()
            try:
                await self.release_reader.wait()
            finally:
                self.reading = False

        async def send(self, _message: object) -> None:
            return None

    connection = SingleReaderConnection()
    manager = SubscriptionManager(connection)
    subscription = Subscription(
        7,
        "orderbook_delta",
        {"market_tickers": ["KXBTC15M-OLD"]},
        MessageQueue(maxsize=1, overflow=OverflowStrategy.ERROR),
    )
    subscription.server_sid = 3
    manager._subscriptions[7] = subscription

    active_reader = asyncio.create_task(connection.recv())
    await connection.reader_started.wait()
    try:
        # This is the actual pinned-SDK update path that production previously
        # reached from the rollover watcher. It calls the same connection's
        # recv() to wait for its command acknowledgement.
        with pytest.raises(ConcurrencyError, match="another coroutine"):
            await manager.update_subscription(
                7,
                "delete_markets",
                market_tickers=["KXBTC15M-OLD"],
            )

        # The fixed host exposes no session/client-id/manager path here: it
        # marks the session boundary and lets normal context exit replace it.
        await asyncio.wait_for(
            host._watch_universe(
                original,
                stop,
                changed,
            ),
            timeout=1.5,
        )
    finally:
        connection.release_reader.set()
        await active_reader

    assert changed.is_set()


@pytest.mark.asyncio
async def test_new_ten_market_session_requires_all_valid_authoritative_snapshots() -> None:
    """A replacement universe cannot look synchronized from a partial snapshot set."""

    universe = {asset: f"KXMARKET{index}-NEW" for index, asset in enumerate(Asset, start=1)}
    asset_by_ticker = {ticker: asset for asset, ticker in universe.items()}
    provider = SdkRecorderMarketDataProvider.isolated(
        asset_by_ticker=asset_by_ticker,
        connection_id="replacement-session",
        stale_seconds=10.0,
    )
    await provider.start()
    try:
        for sequence, (ticker, asset) in enumerate(sorted(asset_by_ticker.items())[:-1], start=1):
            message = SimpleNamespace(
                type="orderbook_snapshot",
                sid=9,
                seq=sequence,
                msg=SimpleNamespace(
                    market_ticker=ticker,
                    market_id=f"market-{asset.value}",
                    yes={},
                    no={},
                ),
            )
            accepted = provider.accept(
                canonical_from_sdk(
                    message,
                    asset_by_ticker=asset_by_ticker,
                    connection_id="replacement-session",
                    received_at=datetime.now(UTC),
                )
            )
            assert accepted.authoritative is True
            assert provider.synchronized_count == sequence
            assert provider.state is RecorderProviderState.WAITING_SNAPSHOT

        ticker, asset = sorted(asset_by_ticker.items())[-1]
        final = provider.accept(
            canonical_from_sdk(
                SimpleNamespace(
                    type="orderbook_snapshot",
                    sid=9,
                    seq=len(Asset),
                    msg=SimpleNamespace(
                        market_ticker=ticker,
                        market_id=f"market-{asset.value}",
                        yes={},
                        no={},
                    ),
                ),
                asset_by_ticker=asset_by_ticker,
                connection_id="replacement-session",
                received_at=datetime.now(UTC),
            )
        )
        assert final.authoritative is True
        assert provider.synchronized_count == len(Asset)
        assert provider.gap_count == 0
    finally:
        await provider.stop()
