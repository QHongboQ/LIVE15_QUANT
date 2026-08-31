from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from live15_quant.kalshi_gateway.shadow import (
    CanonicalWsEventType,
    KalshiSdkReliabilityAdapter,
    ShadowParityComparator,
    ShadowSyncState,
    ShadowTelemetryStore,
    canonical_from_sdk,
)
from live15_quant.managed_kalshi_sdk_shadow import (
    KalshiSdkShadowRunner,
    _sanitized_error_code,
)
from live15_quant.models import Asset

NOW = datetime.now(UTC)
BTC = "KXBTC15M-TEST"
ETH = "KXETH15M-TEST"


def snapshot(ticker: str, sequence: int, *, sid: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        type="orderbook_snapshot",
        sid=sid,
        seq=sequence,
        msg=SimpleNamespace(
            market_ticker=ticker,
            market_id=f"market-{ticker}",
            yes={Decimal("0.40"): Decimal("3")},
            no={Decimal("0.50"): Decimal("4")},
        ),
    )


def delta(ticker: str, sequence: int, *, sid: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        type="orderbook_delta",
        sid=sid,
        seq=sequence,
        msg=SimpleNamespace(
            market_ticker=ticker,
            market_id=f"market-{ticker}",
            side="yes",
            price=Decimal("0.41"),
            delta=Decimal("2"),
            ts_ms=int(NOW.timestamp() * 1000),
        ),
    )


def adapter(tmp_path: Path) -> tuple[KalshiSdkReliabilityAdapter, ShadowTelemetryStore]:
    store = ShadowTelemetryStore(tmp_path / "shadow.sqlite3")
    projection = tmp_path / "old.json"
    projection.write_text("{}", encoding="utf-8")
    return (
        KalshiSdkReliabilityAdapter(
            {BTC: Asset.BTC, ETH: Asset.ETH},
            store,
            ShadowParityComparator(projection),
            connection_id="shadow-test",
            stale_seconds=10,
        ),
        store,
    )


def test_sdk_messages_map_to_canonical_live15_contract() -> None:
    event, book = canonical_from_sdk(
        snapshot(BTC, 1),
        asset_by_ticker={BTC: Asset.BTC},
        connection_id="connection",
        received_at=NOW,
    )
    assert event.event_type is CanonicalWsEventType.SNAPSHOT
    assert event.asset is Asset.BTC
    assert event.sequence == 1
    assert event.receive_timestamp == NOW
    assert book is not None
    assert book.ticker == BTC
    assert book.yes_bids[0].price == Decimal("0.40")


def test_error_diagnostic_contains_only_type_and_http_status() -> None:
    cause = RuntimeError("provider body must not be persisted")
    cause.response = SimpleNamespace(status_code=401)  # type: ignore[attr-defined]
    outer = RuntimeError("credential-looking message must not be persisted")
    outer.__cause__ = cause
    assert _sanitized_error_code(outer) == "RuntimeError/RuntimeError/HTTP_401"


def test_status_heartbeat_preserves_pid_and_managed_identity(tmp_path: Path) -> None:
    store = ShadowTelemetryStore(tmp_path / "shadow.sqlite3")
    runner = KalshiSdkShadowRunner(
        settings=SimpleNamespace(),
        store=store,
        old_projection_path=tmp_path / "old.json",
        status={
            "pid": 1234,
            "started_at": NOW.isoformat(),
            "expected_mode": "SDK_WS_SHADOW_NO_RECORDER_WRITES",
        },
        status_path=tmp_path / "status.json",
        control_path=tmp_path / "control.json",
    )
    try:
        payload = runner._status_payload("WAITING_TICKERS")
        assert payload["pid"] == 1234
        assert payload["started_at"] == NOW.isoformat()
        assert payload["expected_mode"] == "SDK_WS_SHADOW_NO_RECORDER_WRITES"
    finally:
        store.close()


def test_gap_quarantines_all_assets_until_fresh_snapshot_set(tmp_path: Path) -> None:
    bridge, store = adapter(tmp_path)
    try:
        bridge.accept(snapshot(BTC, 1), received_at=NOW)
        bridge.accept(snapshot(ETH, 2), received_at=NOW)
        assert bridge.health(NOW)["synchronized_count"] == 2

        bridge.accept(delta(BTC, 4), received_at=NOW + timedelta(milliseconds=10))
        health = bridge.health(NOW + timedelta(milliseconds=10))
        assert health["synchronized_count"] == 0
        assert all(
            item["state"] == ShadowSyncState.UNSYNCHRONIZED.value
            for item in health["assets"].values()
        )

        bridge.accept(snapshot(BTC, 10), received_at=NOW + timedelta(milliseconds=20))
        assert bridge.health(NOW + timedelta(milliseconds=20))["synchronized_count"] == 0
        bridge.accept(snapshot(ETH, 11), received_at=NOW + timedelta(milliseconds=30))
        health = bridge.health(NOW + timedelta(milliseconds=30))
        assert health["synchronized_count"] == 2
        assert health["metrics"]["gap_count"] == 1
    finally:
        store.close()


def test_sdk_resubscribe_sid_change_uses_fresh_snapshot_and_records_gap(
    tmp_path: Path,
) -> None:
    bridge, store = adapter(tmp_path)
    try:
        bridge.accept(snapshot(BTC, 1), received_at=NOW)
        bridge.accept(snapshot(ETH, 2), received_at=NOW)
        bridge.accept(snapshot(BTC, 1, sid=2), received_at=NOW + timedelta(seconds=1))
        bridge.accept(snapshot(ETH, 2, sid=2), received_at=NOW + timedelta(seconds=1))
        health = bridge.health(NOW + timedelta(seconds=1))
        assert health["synchronized_count"] == 2
        assert health["metrics"]["gap_count"] == 1
    finally:
        store.close()


def test_one_stale_asset_does_not_corrupt_other_asset_state(tmp_path: Path) -> None:
    bridge, store = adapter(tmp_path)
    try:
        bridge.accept(snapshot(BTC, 1), received_at=NOW)
        bridge.accept(snapshot(ETH, 2), received_at=NOW + timedelta(seconds=8))
        health = bridge.health(NOW + timedelta(seconds=12))
        assert health["assets"]["BTC"]["state"] == ShadowSyncState.STALE.value
        assert health["assets"]["ETH"]["state"] == ShadowSyncState.SYNCHRONIZED.value
        assert health["synchronized_count"] == 1
    finally:
        store.close()


def test_parity_uses_only_old_projection_and_independent_shadow_store(tmp_path: Path) -> None:
    recorder = tmp_path / "recorder.sqlite3"
    recorder.write_bytes(b"immutable-recorder-sentinel")
    projection = tmp_path / "old.json"
    projection.write_text(
        json.dumps(
            {
                "state": "SYNCHRONIZED",
                "published_at": NOW.isoformat(),
                "current_tickers": [BTC],
                "books": {
                    BTC: {
                        "yes_bids": [["0.40", "3"]],
                        "no_bids": [["0.50", "4"]],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = ShadowTelemetryStore(tmp_path / "shadow.sqlite3")
    bridge = KalshiSdkReliabilityAdapter(
        {BTC: Asset.BTC},
        store,
        ShadowParityComparator(projection),
        connection_id="shadow-test",
    )
    try:
        bridge.accept(snapshot(BTC, 1), received_at=NOW)
        bridge.accept(delta(BTC, 2), received_at=NOW + timedelta(seconds=2))
        metrics = store.summary()
        assert metrics["aligned_comparisons"] == 1
        assert metrics["ticker_match_rate"] == 1
        assert metrics["best_bid_match_rate"] == 1
        assert metrics["best_ask_match_rate"] == 1
        assert metrics["top_depth_match_rate"] == 1
        assert metrics["per_asset"]["BTC"]["ticker_match_rate"] == 1
        assert metrics["per_asset"]["BTC"]["mismatch_count"] == 0
        assert recorder.read_bytes() == b"immutable-recorder-sentinel"
    finally:
        store.close()


def test_reconnect_quarantines_books_and_persists_canonical_event(tmp_path: Path) -> None:
    bridge, store = adapter(tmp_path)
    try:
        bridge.accept(snapshot(BTC, 1), received_at=NOW)
        bridge.accept(snapshot(ETH, 2), received_at=NOW)
        bridge.connection_state_changed("connected", "reconnecting", NOW + timedelta(seconds=1))
        health = bridge.health(NOW + timedelta(seconds=1))
        assert health["synchronized_count"] == 0
        assert health["metrics"]["reconnect_count"] == 1
        assert store.summary()["snapshot_count"] == 2
        reconnect_events = store.connection.execute(
            "SELECT COUNT(*) count FROM shadow_events WHERE event_type='RECONNECT'"
        ).fetchone()
        assert reconnect_events["count"] == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_managed_session_connects_and_subscribes_all_ten_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    universe = {f"KX{asset.name.replace('_', '')}15M-TEST": asset for asset in Asset}
    key_id = tmp_path / "id.txt"
    key_id.write_text("masked-id", encoding="utf-8")
    private_key = tmp_path / "private.key"
    private_key.write_text("not-read-by-fake", encoding="utf-8")
    settings = SimpleNamespace(
        enable_kalshi_production_websocket=True,
        kalshi_production_api_key_id_path=key_id,
        kalshi_production_private_key_path=private_key,
        kalshi_websocket_read_timeout_seconds=5.0,
        kalshi_websocket_stale_seconds=10.0,
    )
    subscriptions: list[tuple[str, tuple[str, ...]]] = []

    class PendingStream:
        def __aiter__(self) -> PendingStream:
            return self

        async def __anext__(self) -> object:
            await asyncio.Event().wait()
            raise StopAsyncIteration

    class SnapshotStream:
        def __init__(self) -> None:
            self.messages = [
                snapshot(ticker, sequence)
                for sequence, ticker in enumerate(sorted(universe), start=1)
            ]

        def __aiter__(self) -> SnapshotStream:
            return self

        async def __anext__(self) -> object:
            if not self.messages:
                raise StopAsyncIteration
            return self.messages.pop(0)

    class Session:
        async def subscribe_orderbook_delta(self, *, tickers, maxsize):
            subscriptions.append(("orderbook", tuple(tickers)))
            assert maxsize == 10_000
            return SnapshotStream()

        async def subscribe_ticker(self, *, tickers, maxsize):
            subscriptions.append(("ticker", tuple(tickers)))
            assert maxsize == 2_000
            return PendingStream()

        async def subscribe_market_lifecycle(self, *, tickers, maxsize):
            subscriptions.append(("lifecycle", tuple(tickers)))
            assert maxsize == 1_000
            return PendingStream()

    class Context:
        async def __aenter__(self) -> Session:
            return Session()

        async def __aexit__(self, *_args) -> None:
            return None

    class WebSocket:
        def connect(self) -> Context:
            return Context()

    class Gateway:
        def __init__(self, *_args, **_kwargs) -> None:
            self.stream = SnapshotStream()

        def immutable_orderbook_stream(self, *, maxsize):
            assert maxsize == 20_000
            return self.stream

        def build(
            self,
            *,
            on_state_change: object,
            on_error: object,
            capture_pre_dispatch: bool,
        ) -> WebSocket:
            assert callable(on_state_change)
            assert callable(on_error)
            assert capture_pre_dispatch is True
            return WebSocket()

    monkeypatch.setattr("live15_quant.managed_kalshi_sdk_shadow.KalshiWebSocketGateway", Gateway)
    store = ShadowTelemetryStore(tmp_path / "shadow.sqlite3")
    runner = KalshiSdkShadowRunner(
        settings=settings,
        store=store,
        old_projection_path=tmp_path / "old.json",
        status={},
        status_path=tmp_path / "status.json",
        control_path=tmp_path / "control.json",
    )
    try:
        await runner._run_session(universe)
        assert [kind for kind, _ in subscriptions] == ["ticker", "lifecycle", "orderbook"]
        assert all(len(tickers) == 10 for _, tickers in subscriptions)
        assert runner.adapter is not None
        assert runner.adapter.health(datetime.now(UTC))["synchronized_count"] == 10
    finally:
        store.close()


@pytest.mark.asyncio
async def test_rollover_watcher_detects_new_ticker_universe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = tuple(f"KX{index}-OLD" for index in range(len(Asset)))
    replacement = {f"KX{index}-NEW": asset for index, asset in enumerate(Asset)}
    monkeypatch.setattr(
        "live15_quant.managed_kalshi_sdk_shadow._current_universe",
        lambda _settings: replacement,
    )
    store = ShadowTelemetryStore(tmp_path / "shadow.sqlite3")
    runner = KalshiSdkShadowRunner(
        settings=SimpleNamespace(),
        store=store,
        old_projection_path=tmp_path / "old.json",
        status={},
        status_path=tmp_path / "status.json",
        control_path=tmp_path / "control.json",
    )
    changed = asyncio.Event()
    try:
        await asyncio.wait_for(runner._watch_rollover(original, changed), timeout=2)
        assert changed.is_set()
        assert runner.rollover_count == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unknown_session_ticker_triggers_clean_rollover_without_accepting_it(
    tmp_path: Path,
) -> None:
    class RolloverStream:
        def __aiter__(self) -> RolloverStream:
            return self

        async def __anext__(self) -> object:
            if getattr(self, "sent", False):
                raise StopAsyncIteration
            self.sent = True
            return snapshot("KXBTC15M-NEW", 2)

    bridge, store = adapter(tmp_path)
    runner = KalshiSdkShadowRunner(
        settings=SimpleNamespace(),
        store=store,
        old_projection_path=tmp_path / "old.json",
        status={},
        status_path=tmp_path / "status.json",
        control_path=tmp_path / "control.json",
    )
    runner.adapter = bridge
    rollover = asyncio.Event()
    try:
        await runner._pump(RolloverStream(), unknown_ticker_policy="rollover", rollover=rollover)
        assert rollover.is_set()
        assert runner.rollover_count == 1
        assert runner.last_rollover_reason == "SDK_TICKER_ROLLOVER"
        assert bridge.health(datetime.now(UTC))["synchronized_count"] == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unrelated_lifecycle_ticker_is_ignored_without_rollover(
    tmp_path: Path,
) -> None:
    class LifecycleStream:
        def __aiter__(self) -> LifecycleStream:
            return self

        async def __anext__(self) -> object:
            if getattr(self, "sent", False):
                raise StopAsyncIteration
            self.sent = True
            return SimpleNamespace(
                type="market_lifecycle_v2",
                msg=SimpleNamespace(market_ticker="KXUNRELATED-MARKET"),
            )

    bridge, store = adapter(tmp_path)
    runner = KalshiSdkShadowRunner(
        settings=SimpleNamespace(),
        store=store,
        old_projection_path=tmp_path / "old.json",
        status={},
        status_path=tmp_path / "status.json",
        control_path=tmp_path / "control.json",
    )
    runner.adapter = bridge
    try:
        await runner._pump(LifecycleStream(), unknown_ticker_policy="ignore")
        assert runner.rollover_count == 0
        assert runner.last_rollover_reason is None
        assert bridge.health(datetime.now(UTC))["synchronized_count"] == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_event_fee_update_does_not_restart_market_lifecycle_pump(
    tmp_path: Path,
) -> None:
    class FeeStream:
        def __aiter__(self) -> FeeStream:
            return self

        async def __anext__(self) -> object:
            if getattr(self, "sent", False):
                raise StopAsyncIteration
            self.sent = True
            return SimpleNamespace(type="event_fee_update", msg=SimpleNamespace())

    store = ShadowTelemetryStore(tmp_path / "shadow.sqlite3")
    runner = KalshiSdkShadowRunner(
        settings=SimpleNamespace(),
        store=store,
        old_projection_path=tmp_path / "old.json",
        status={},
        status_path=tmp_path / "status.json",
        control_path=tmp_path / "control.json",
    )
    try:
        await runner._pump(FeeStream())
    finally:
        store.close()


@pytest.mark.asyncio
async def test_heartbeat_treats_sdk_streaming_state_as_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, store = adapter(tmp_path)
    bridge.last_state = "streaming"
    runner = KalshiSdkShadowRunner(
        settings=SimpleNamespace(),
        store=store,
        old_projection_path=tmp_path / "old.json",
        status={},
        status_path=tmp_path / "status.json",
        control_path=tmp_path / "control.json",
    )
    runner.adapter = bridge
    written: list[dict[str, object]] = []

    def capture(_path: Path, payload: dict[str, object]) -> None:
        written.append(payload)
        runner.stop_event.set()

    monkeypatch.setattr("live15_quant.managed_kalshi_sdk_shadow.atomic_json", capture)
    try:
        await runner._heartbeat()
        assert written[0]["status"] == "RUNNING"
    finally:
        store.close()
