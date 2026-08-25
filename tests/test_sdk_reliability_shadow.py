from __future__ import annotations

import asyncio
import json
import sqlite3
import zlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from live15_quant.kalshi_gateway.canonical_ws import (
    CanonicalEventType,
    canonical_from_sdk,
    unknown_lifecycle_event,
)
from live15_quant.kalshi_gateway.reliability import (
    KalshiReliabilityAdapter,
    ReliabilityState,
)
from live15_quant.kalshi_gateway.shadow_recorder import (
    BookPriceSample,
    RestSanityStatus,
    SdkReliabilityShadowRecorder,
    compare_rest_orderbook,
)
from live15_quant.kalshi_gateway.websocket import (
    GatewayReceivedMessage,
    GatewayWireDiagnostic,
    KalshiWebSocketGateway,
    _load_ws_json_with_sparse_snapshot_compat,
)
from live15_quant.kalshi_ws import KalshiUnsynchronizedBookError
from live15_quant.managed_sdk_reliability_shadow import (
    SdkReliabilityShadowRunner,
    classify_event_lifecycle,
)
from live15_quant.models import Asset, OrderBookLevel

NOW = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
BTC = "KXBTC15M-TEST"
ETH = "KXETH15M-TEST"


def snapshot(
    ticker: str,
    sequence: int,
    *,
    sid: int = 1,
    yes: dict[Decimal, Decimal] | None = None,
    no: dict[Decimal, Decimal] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        type="orderbook_snapshot",
        sid=sid,
        seq=sequence,
        msg=SimpleNamespace(
            market_ticker=ticker,
            market_id=f"market-{ticker}",
            yes=yes if yes is not None else {Decimal("0.40"): Decimal("3")},
            no=no if no is not None else {Decimal("0.50"): Decimal("4")},
        ),
    )


def delta(ticker: str, sequence: int, *, side: str = "yes", sid: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        type="orderbook_delta",
        sid=sid,
        seq=sequence,
        msg=SimpleNamespace(
            market_ticker=ticker,
            market_id=f"market-{ticker}",
            side=side,
            price=Decimal("0.41"),
            delta=Decimal("2"),
            ts_ms=int(NOW.timestamp() * 1000),
        ),
    )


def canonical(message: object, universe: dict[str, Asset], *, received_at: datetime = NOW):
    return canonical_from_sdk(
        message,
        asset_by_ticker=universe,
        connection_id="reliability-test",
        received_at=received_at,
    )


def adapter(
    tmp_path: Path,
    universe: dict[str, Asset] | None = None,
) -> tuple[KalshiReliabilityAdapter, SdkReliabilityShadowRecorder]:
    mapping = universe or {BTC: Asset.BTC, ETH: Asset.ETH}
    recorder = SdkReliabilityShadowRecorder(
        tmp_path / "shadow.sqlite3",
        official_recorder_path=tmp_path / "official.sqlite3",
    )
    return (
        KalshiReliabilityAdapter(
            mapping,
            recorder,
            connection_id="reliability-test",
            stale_seconds=10.0,
        ),
        recorder,
    )


def test_canonical_snapshot_and_delta_mapping() -> None:
    universe = {BTC: Asset.BTC}
    first = canonical(snapshot(BTC, 1), universe)
    second = canonical(delta(BTC, 2), universe)
    assert first.event_type is CanonicalEventType.SNAPSHOT
    assert first.yes_bids == (OrderBookLevel(Decimal("0.40"), Decimal("3")),)
    assert first.no_bids == (OrderBookLevel(Decimal("0.50"), Decimal("4")),)
    assert first.executable_yes_ask == Decimal("0.50")
    assert first.executable_no_ask == Decimal("0.60")
    assert first.top_depth["yes"] == first.yes_bids
    assert second.event_type is CanonicalEventType.DELTA
    assert second.delta_side == "yes"
    assert second.delta_price == Decimal("0.41")
    assert second.delta_quantity == Decimal("2")
    assert second.exchange_timestamp == NOW


def test_empty_side_compatibility_is_narrow_and_malformed_remains_strict() -> None:
    raw = json.dumps(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {
                "market_ticker": BTC,
                "market_id": "market",
                "yes_dollars_fp": [["0.40", "1"]],
            },
        }
    )
    normalized = _load_ws_json_with_sparse_snapshot_compat(raw)
    assert normalized["msg"]["no_dollars_fp"] == []
    ambiguous = _load_ws_json_with_sparse_snapshot_compat(
        json.dumps(
            {
                "type": "orderbook_snapshot",
                "sid": 1,
                "seq": 1,
                "msg": {"market_ticker": BTC, "market_id": "market"},
            }
        )
    )
    assert "yes_dollars_fp" not in ambiguous["msg"]
    assert "no_dollars_fp" not in ambiguous["msg"]
    with pytest.raises(ValueError, match="YES snapshot"):
        malformed = snapshot(BTC, 1)
        malformed.msg.yes = None
        canonical(malformed, {BTC: Asset.BTC})


def test_gap_is_persisted_and_all_assets_quarantined_until_snapshot_set(
    tmp_path: Path,
) -> None:
    bridge, recorder = adapter(tmp_path)
    universe = bridge.asset_by_ticker
    try:
        bridge.accept(canonical(snapshot(BTC, 1), universe))
        bridge.accept(canonical(snapshot(ETH, 2), universe))
        assert bridge.health(NOW)["synchronized_count"] == 2

        result = bridge.accept(
            canonical(delta(BTC, 4), universe, received_at=NOW + timedelta(milliseconds=1))
        )
        assert result.authoritative is False
        health = bridge.health(NOW + timedelta(milliseconds=1))
        assert health["synchronized_count"] == 0
        assert {item["state"] for item in health["assets"].values()} == {
            ReliabilityState.QUARANTINED.value
        }
        assert recorder.summary()["gap_count"] == 1

        bridge.accept(
            canonical(snapshot(BTC, 10), universe, received_at=NOW + timedelta(seconds=1))
        )
        assert bridge.health(NOW + timedelta(seconds=1))["synchronized_count"] == 0
        bridge.accept(
            canonical(snapshot(ETH, 11), universe, received_at=NOW + timedelta(seconds=1))
        )
        health = bridge.health(NOW + timedelta(seconds=1))
        assert health["synchronized_count"] == 2
        assert recorder.summary()["unrecovered_gap_count"] == 0
    finally:
        recorder.close()


def test_per_asset_freshness_is_fail_closed_without_harming_other_asset(
    tmp_path: Path,
) -> None:
    bridge, recorder = adapter(tmp_path)
    universe = bridge.asset_by_ticker
    try:
        bridge.accept(canonical(snapshot(BTC, 1), universe))
        bridge.accept(canonical(snapshot(ETH, 2), universe, received_at=NOW + timedelta(seconds=8)))
        health = bridge.health(NOW + timedelta(seconds=12))
        assert health["assets"]["BTC"]["state"] == ReliabilityState.STALE.value
        assert health["assets"]["ETH"]["state"] == ReliabilityState.SYNCHRONIZED.value
        with pytest.raises(KalshiUnsynchronizedBookError):
            bridge.book(Asset.BTC)
        assert bridge.book(Asset.ETH).ticker == ETH
        assert recorder.summary()["reconnect_count"] == 0
    finally:
        recorder.close()


def test_delta_updates_only_its_asset_without_refreshing_other_book_timestamp(
    tmp_path: Path,
) -> None:
    bridge, recorder = adapter(tmp_path)
    try:
        bridge.accept(canonical(snapshot(BTC, 1), bridge.asset_by_ticker))
        bridge.accept(
            canonical(
                snapshot(ETH, 2),
                bridge.asset_by_ticker,
                received_at=NOW + timedelta(milliseconds=1),
            )
        )
        eth_before = bridge.book(Asset.ETH).received_timestamp
        btc_history_before = len(bridge.book_history[Asset.BTC])
        eth_history_before = len(bridge.book_history[Asset.ETH])

        bridge.accept(
            canonical(
                delta(BTC, 3),
                bridge.asset_by_ticker,
                received_at=NOW + timedelta(seconds=1),
            )
        )

        assert bridge.book(Asset.BTC).received_timestamp == NOW + timedelta(seconds=1)
        assert bridge.book(Asset.ETH).received_timestamp == eth_before
        assert len(bridge.book_history[Asset.BTC]) == btc_history_before + 1
        assert len(bridge.book_history[Asset.ETH]) == eth_history_before
    finally:
        recorder.close()


def test_market_lifecycle_uses_nested_event_identity_from_new_sdk_shape() -> None:
    universe = {BTC: Asset.BTC}
    message = SimpleNamespace(
        type="market_lifecycle_v2",
        sid=2,
        seq=1,
        msg=SimpleNamespace(
            market_ticker=BTC,
            event_type="created",
            result=None,
            event_ticker=None,
            additional_metadata={"event_ticker": "KXBTC15M-EVENT"},
            exchange_index=0,
        ),
    )

    event = canonical(message, universe)

    assert event.event_ticker == "KXBTC15M-EVENT"
    assert event.lifecycle_type == "created"


def test_market_lifecycle_derives_missing_identity_and_waits_for_result(
    tmp_path: Path,
) -> None:
    ticker = "KXBTC15M-26AUG250015-15"
    bridge, recorder = adapter(tmp_path, {ticker: Asset.BTC})
    try:
        message = SimpleNamespace(
            type="market_lifecycle_v2",
            sid=2,
            seq=1,
            msg=SimpleNamespace(
                market_ticker=ticker,
                event_type="settled",
                result=None,
                event_ticker=None,
                additional_metadata=None,
                exchange_index=0,
            ),
        )

        canonical_event = canonical(message, bridge.asset_by_ticker)
        result = bridge.accept(canonical_event)

        assert canonical_event.event_ticker == "KXBTC15M-26AUG250015"
        assert canonical_event.lifecycle_result is None
        assert result.authoritative is True
        assert result.lifecycle.value == "settlement_pending"
        assert recorder.summary()["unknown_lifecycle_count"] == 0
    finally:
        recorder.close()


def test_known_lifecycle_maps_and_unknown_is_diagnostic_only(tmp_path: Path) -> None:
    bridge, recorder = adapter(tmp_path, {BTC: Asset.BTC})
    try:
        message = SimpleNamespace(
            type="market_lifecycle_v2",
            sid=2,
            seq=1,
            msg=SimpleNamespace(
                market_ticker=BTC,
                event_type="active",
                result=None,
                event_ticker="KXBTC15M",
                exchange_index=0,
            ),
        )
        result = bridge.accept(canonical(message, bridge.asset_by_ticker))
        assert result.lifecycle.value == "open"
        unknown = unknown_lifecycle_event(
            asset=Asset.BTC,
            ticker=BTC,
            connection_id="reliability-test",
            observed_at=NOW + timedelta(seconds=1),
            wire_type="future_market_lifecycle_v9",
        )
        result = bridge.accept(unknown)
        assert result.authoritative is False
        assert result.lifecycle.value == "open"
        assert recorder.summary()["unknown_lifecycle_count"] == 1
    finally:
        recorder.close()


def test_settlement_uses_official_result_and_stale_regression_is_ignored(
    tmp_path: Path,
) -> None:
    bridge, recorder = adapter(tmp_path, {BTC: Asset.BTC})

    def lifecycle(kind: str, result: str | None = None) -> object:
        return canonical(
            SimpleNamespace(
                type="market_lifecycle_v2",
                sid=2,
                seq=1,
                msg=SimpleNamespace(
                    market_ticker=BTC,
                    event_type=kind,
                    result=result,
                    event_ticker="KXBTC15M",
                    exchange_index=0,
                ),
            ),
            bridge.asset_by_ticker,
        )

    try:
        bridge.accept(lifecycle("active"))
        bridge.accept(lifecycle("determined"))
        settled = bridge.accept(lifecycle("settled", "yes"))
        assert settled.lifecycle.value == "settled_yes"
        stale = bridge.accept(lifecycle("closed"))
        assert stale.authoritative is False
        assert stale.lifecycle.value == "settled_yes"
    finally:
        recorder.close()


def test_reconnect_quarantines_and_fresh_snapshot_resynchronizes(tmp_path: Path) -> None:
    bridge, recorder = adapter(tmp_path, {BTC: Asset.BTC})
    try:
        bridge.accept(canonical(snapshot(BTC, 1), bridge.asset_by_ticker))
        bridge.connection_state_changed("streaming", "reconnecting", NOW + timedelta(seconds=1))
        assert bridge.health(NOW + timedelta(seconds=1))["synchronized_count"] == 0
        bridge.accept(
            canonical(
                snapshot(BTC, 1, sid=2),
                bridge.asset_by_ticker,
                received_at=NOW + timedelta(seconds=2),
            )
        )
        assert bridge.health(NOW + timedelta(seconds=2))["synchronized_count"] == 1
        assert recorder.summary()["reconnect_event_count"] == 1
    finally:
        recorder.close()


def test_shadow_recorder_event_and_book_commit_is_atomic(tmp_path: Path) -> None:
    bridge, recorder = adapter(tmp_path, {BTC: Asset.BTC})
    recorder.connection.execute(
        """CREATE TRIGGER reject_book BEFORE INSERT ON validated_books
        BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"""
    )
    recorder.connection.commit()
    try:
        with pytest.raises(Exception, match="synthetic failure"):
            bridge.accept(canonical(snapshot(BTC, 1), bridge.asset_by_ticker))
        count = recorder.connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0]
        assert count == 0
        assert bridge.health(NOW)["synchronized_count"] == 0
    finally:
        recorder.close()


def test_shadow_store_uses_snapshot_plus_canonical_deltas_without_full_book_duplication(
    tmp_path: Path,
) -> None:
    bridge, recorder = adapter(tmp_path, {BTC: Asset.BTC})
    try:
        bridge.accept(canonical(snapshot(BTC, 1), bridge.asset_by_ticker))
        bridge.accept(
            canonical(
                delta(BTC, 2),
                bridge.asset_by_ticker,
                received_at=NOW + timedelta(milliseconds=1),
            )
        )
        recorder.flush()

        events = recorder.connection.execute(
            "SELECT event_type,payload_json FROM canonical_events ORDER BY id"
        ).fetchall()
        compressed = recorder.connection.execute(
            "SELECT payload FROM canonical_delta_batches ORDER BY id"
        ).fetchone()[0]
        deltas = json.loads(zlib.decompress(compressed))
        checkpoints = recorder.connection.execute(
            "SELECT COUNT(*) FROM validated_books"
        ).fetchone()[0]

        assert [row["event_type"] for row in events] == ["SNAPSHOT"]
        assert json.loads(deltas[0][9])["delta_price"] == "0.41"
        assert checkpoints == 1
    finally:
        recorder.close()


def test_rest_sanity_checks_executable_side_semantics(tmp_path: Path) -> None:
    bridge, recorder = adapter(tmp_path, {BTC: Asset.BTC})
    try:
        bridge.accept(canonical(snapshot(BTC, 1), bridge.asset_by_ticker))
        book = bridge.book(Asset.BTC)
        rest = SimpleNamespace(
            ticker=BTC,
            yes=[SimpleNamespace(price=Decimal("0.40"), quantity=Decimal("3"))],
            no=[SimpleNamespace(price=Decimal("0.50"), quantity=Decimal("4"))],
        )
        result = compare_rest_orderbook(
            asset=Asset.BTC,
            ticker=BTC,
            checked_at=NOW,
            ws_book=book,
            rest_orderbook=rest,
            request_started_at=NOW - timedelta(milliseconds=50),
            response_received_at=NOW,
            aligned_sample=BookPriceSample(
                NOW,
                book.sequence,
                (
                    Decimal("0.40"),
                    Decimal("0.50"),
                    Decimal("0.50"),
                    Decimal("0.60"),
                ),
            ),
        )
        assert result.status is RestSanityStatus.EXACT_MATCH
        assert result.ws_yes_ask == Decimal("0.50")
        assert result.ws_no_ask == Decimal("0.60")
        moved_rest = SimpleNamespace(
            ticker=BTC,
            yes=[SimpleNamespace(price=Decimal("0.39"), quantity=Decimal("3"))],
            no=rest.no,
        )
        moved = compare_rest_orderbook(
            asset=Asset.BTC,
            ticker=BTC,
            checked_at=NOW,
            ws_book=book,
            rest_orderbook=moved_rest,
            request_started_at=NOW - timedelta(milliseconds=100),
            response_received_at=NOW,
            aligned_sample=BookPriceSample(
                NOW,
                book.sequence,
                (
                    Decimal("0.40"),
                    Decimal("0.50"),
                    Decimal("0.50"),
                    Decimal("0.60"),
                ),
            ),
        )
        assert moved.status is RestSanityStatus.TRUE_MISMATCH
        interval_match = compare_rest_orderbook(
            asset=Asset.BTC,
            ticker=BTC,
            checked_at=NOW,
            ws_book=book,
            rest_orderbook=moved_rest,
            request_started_at=NOW - timedelta(milliseconds=100),
            response_received_at=NOW,
            aligned_sample=BookPriceSample(
                NOW,
                book.sequence,
                (
                    Decimal("0.40"),
                    Decimal("0.50"),
                    Decimal("0.50"),
                    Decimal("0.60"),
                ),
            ),
            interval_samples=(
                BookPriceSample(
                    NOW - timedelta(milliseconds=50),
                    book.sequence - 1,
                    (
                        Decimal("0.39"),
                        Decimal("0.50"),
                        Decimal("0.50"),
                        Decimal("0.61"),
                    ),
                ),
            ),
        )
        assert interval_match.status is RestSanityStatus.MOVED_DURING_READ
        unavailable = compare_rest_orderbook(
            asset=Asset.BTC,
            ticker=BTC,
            checked_at=NOW,
            ws_book=book,
            rest_orderbook=rest,
            request_started_at=NOW - timedelta(seconds=1),
            response_received_at=NOW,
            aligned_sample=BookPriceSample(
                NOW - timedelta(seconds=1),
                book.sequence,
                (
                    Decimal("0.40"),
                    Decimal("0.50"),
                    Decimal("0.50"),
                    Decimal("0.60"),
                ),
            ),
        )
        assert unavailable.status is RestSanityStatus.UNAVAILABLE
        assert unavailable.reason == "NO_WS_SAMPLE_WITHIN_ALIGNMENT_WINDOW"
    finally:
        recorder.close()


@pytest.mark.asyncio
async def test_rest_alignment_waits_for_consumer_watermark_not_wall_clock(
    tmp_path: Path,
) -> None:
    runner = SdkReliabilityShadowRunner(
        settings=SimpleNamespace(),
        recorder=SimpleNamespace(summary=lambda: {}),
        status_path=tmp_path / "status.json",
    )
    crossed = False

    class Adapter:
        def orderbook_processed_through(self, _target: datetime) -> bool:
            return crossed

    async def advance() -> None:
        nonlocal crossed
        await asyncio.sleep(0.02)
        crossed = True

    task = asyncio.create_task(advance())
    await runner._await_ws_alignment_watermark(
        Adapter(),
        NOW,
        timeout_seconds=0.2,
    )
    await task

    assert crossed is True


def test_documented_event_lifecycle_classification_is_window_aware() -> None:
    active = ("KXBTC15M-26AUG250015-15",)

    def diagnostic(event_ticker: str | None, series: str | None = "KXBTC15M"):
        return GatewayWireDiagnostic(
            diagnostic_kind="EVENT_LIFECYCLE",
            wire_type="event_lifecycle",
            market_ticker=None,
            event_ticker=event_ticker,
            subscription_id=1,
            sequence=2,
            received_at=NOW,
            series_ticker=series,
            exchange_index=0,
        )

    assert (
        classify_event_lifecycle(
            diagnostic("KXBTC15M-26AUG250015"),
            active_tickers=active,
            previous_event_tickers=frozenset(),
        )
        == "EVENT_LIFECYCLE_CURRENT_WINDOW"
    )
    assert (
        classify_event_lifecycle(
            diagnostic("KXBTC15M-26AUG250000"),
            active_tickers=active,
            previous_event_tickers=frozenset({"KXBTC15M-26AUG250000"}),
        )
        == "STALE_LIFECYCLE"
    )
    assert (
        classify_event_lifecycle(
            diagnostic("KXBTC15M-26AUG250030"),
            active_tickers=active,
            previous_event_tickers=frozenset(),
        )
        == "EVENT_LIFECYCLE_NONCURRENT_WINDOW"
    )
    assert (
        classify_event_lifecycle(
            diagnostic("KXSPORTS-EVENT", "KXSPORTS"),
            active_tickers=active,
            previous_event_tickers=frozenset(),
        )
        == "EVENT_LIFECYCLE_UNRELATED"
    )
    assert (
        classify_event_lifecycle(
            diagnostic(None),
            active_tickers=active,
            previous_event_tickers=frozenset(),
        )
        == "EVENT_LIFECYCLE_MALFORMED"
    )


def test_shadow_path_cannot_equal_official_recorder_path(tmp_path: Path) -> None:
    path = tmp_path / "recorder.sqlite3"
    with pytest.raises(ValueError, match="must not open"):
        SdkReliabilityShadowRecorder(path, official_recorder_path=path)


def test_shadow_store_uses_wal_normal_without_affecting_official_recorder(
    tmp_path: Path,
) -> None:
    shadow_path = tmp_path / "shadow.sqlite3"
    official_path = tmp_path / "official.sqlite3"
    recorder = SdkReliabilityShadowRecorder(
        shadow_path,
        official_recorder_path=official_path,
    )
    try:
        assert recorder.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert recorder.connection.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert not official_path.exists()
    finally:
        recorder.close()


def test_shadow_event_batch_is_bounded_atomic_and_flushable(tmp_path: Path) -> None:
    recorder = SdkReliabilityShadowRecorder(
        tmp_path / "shadow.sqlite3",
        official_recorder_path=tmp_path / "official.sqlite3",
        commit_batch_size=2,
    )
    bridge = KalshiReliabilityAdapter(
        {BTC: Asset.BTC},
        recorder,
        connection_id="reliability-test",
        stale_seconds=10.0,
    )
    observer = None
    try:
        bridge.accept(canonical(snapshot(BTC, 1), bridge.asset_by_ticker))
        observer = sqlite3.connect(recorder.path)
        assert observer.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0] == 0

        bridge.accept(
            canonical(
                delta(BTC, 2),
                bridge.asset_by_ticker,
                received_at=NOW + timedelta(milliseconds=1),
            )
        )
        assert observer.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0] == 1
        batch = observer.execute(
            "SELECT event_count,compression,payload FROM canonical_delta_batches"
        ).fetchone()
        assert batch[0:2] == (1, "zlib-json-v1")
        retained = json.loads(zlib.decompress(batch[2]))
        assert retained[0][4] == "DELTA"
        assert retained[0][5] == 2

        bridge.accept(
            canonical(
                delta(BTC, 3),
                bridge.asset_by_ticker,
                received_at=NOW + timedelta(milliseconds=2),
            )
        )
        recorder.flush()
        assert observer.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0] == 1
        assert (
            observer.execute("SELECT SUM(event_count) FROM canonical_delta_batches").fetchone()[0]
            == 2
        )
        assert recorder.summary()["event_count"] == 3
    finally:
        if observer is not None:
            observer.close()
        recorder.close()


def test_shadow_heartbeat_summary_does_not_rescan_growing_tables(tmp_path: Path) -> None:
    bridge, recorder = adapter(tmp_path, {BTC: Asset.BTC})
    statements: list[str] = []
    try:
        bridge.accept(canonical(snapshot(BTC, 1), bridge.asset_by_ticker))
        recorder.flush()
        recorder.connection.set_trace_callback(statements.append)

        summary = recorder.summary()

        assert summary["snapshot_count"] == 1
        assert not any(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    finally:
        recorder.close()


def test_terminal_shadow_status_is_not_reported_alive(tmp_path: Path) -> None:
    recorder = SimpleNamespace(summary=lambda: {})
    runner = SdkReliabilityShadowRunner(
        settings=SimpleNamespace(),
        recorder=recorder,
        status_path=tmp_path / "status.json",
    )

    assert runner.status_payload("RUNNING")["process_alive"] is True
    assert runner.status_payload("STOPPED")["process_alive"] is False
    assert runner.status_payload("ERROR")["process_alive"] is False


@pytest.mark.asyncio
async def test_unknown_lifecycle_wire_type_is_sanitized_before_sdk_dispatch() -> None:
    gateway = object.__new__(KalshiWebSocketGateway)
    gateway._orderbook_feed = None
    feed = gateway.immutable_orderbook_stream(maxsize=10)
    stream = gateway.wire_diagnostic_stream()
    feed.load(
        json.dumps(
            {
                "type": "future_market_lifecycle_v9",
                "sid": 5,
                "seq": 10,
                "msg": {
                    "market_ticker": BTC,
                    "event_ticker": "KXBTC15M-EVENT",
                    "secret_like_extra": "must-not-be-captured",
                },
            }
        )
    )
    diagnostic = await asyncio.wait_for(anext(stream), timeout=1)
    assert diagnostic.diagnostic_kind == "UNKNOWN_LIFECYCLE"
    assert diagnostic.wire_type == "future_market_lifecycle_v9"
    assert diagnostic.market_ticker == BTC
    assert "secret" not in repr(diagnostic)


@pytest.mark.asyncio
async def test_documented_event_lifecycle_fixture_is_recognized_and_sanitized() -> None:
    gateway = object.__new__(KalshiWebSocketGateway)
    gateway._orderbook_feed = None
    feed = gateway.immutable_orderbook_stream(maxsize=10)
    stream = gateway.wire_diagnostic_stream()
    feed.load(
        json.dumps(
            {
                "type": "event_lifecycle",
                "sid": 5,
                "seq": 10,
                "msg": {
                    "event_ticker": "KXBTC15M-26AUG250015",
                    "series_ticker": "KXBTC15M",
                    "exchange_index": 0,
                    "title": "not persisted",
                },
            }
        )
    )

    diagnostic = await asyncio.wait_for(anext(stream), timeout=1)

    assert diagnostic.diagnostic_kind == "EVENT_LIFECYCLE"
    assert diagnostic.event_ticker == "KXBTC15M-26AUG250015"
    assert diagnostic.series_ticker == "KXBTC15M"
    assert "not persisted" not in repr(diagnostic)


@pytest.mark.asyncio
async def test_pump_preserves_decode_time_and_yields_in_bounded_batches(tmp_path: Path) -> None:
    bridge, recorder = adapter(tmp_path, {BTC: Asset.BTC})
    runner = SdkReliabilityShadowRunner(
        settings=SimpleNamespace(),
        recorder=recorder,
        status_path=tmp_path / "status.json",
    )
    runner.adapter = bridge
    yielded = asyncio.Event()

    async def always_ready():
        for sequence in range(1, 258):
            yield GatewayReceivedMessage(
                snapshot(BTC, sequence), NOW + timedelta(milliseconds=sequence)
            )

    async def peer() -> None:
        await asyncio.sleep(0)
        yielded.set()

    peer_task = asyncio.create_task(peer())
    try:
        await runner._pump(always_ready(), rollover=asyncio.Event())
        await peer_task
        assert yielded.is_set()
        samples = bridge.price_samples(
            Asset.BTC,
            since=NOW,
            until=NOW + timedelta(seconds=1),
        )
        assert samples[-1].observed_at == NOW + timedelta(milliseconds=257)
    finally:
        recorder.close()


@pytest.mark.asyncio
async def test_redundant_sdk_orderbook_stream_is_drained_in_bounded_batches(tmp_path: Path) -> None:
    runner = SdkReliabilityShadowRunner(
        settings=SimpleNamespace(),
        recorder=SimpleNamespace(summary=lambda: {}),
        status_path=tmp_path / "status.json",
    )
    peer_ran = asyncio.Event()

    async def burst():
        for value in range(1_024):
            yield value

    async def peer() -> None:
        await asyncio.sleep(0)
        peer_ran.set()

    peer_task = asyncio.create_task(peer())
    await runner._drain(burst())
    await peer_task

    assert peer_ran.is_set()


@pytest.mark.asyncio
async def test_lifecycle_channel_end_resubscribes_without_socket_reconnect(tmp_path: Path) -> None:
    bridge, recorder = adapter(tmp_path, {BTC: Asset.BTC})
    runner = SdkReliabilityShadowRunner(
        settings=SimpleNamespace(),
        recorder=recorder,
        status_path=tmp_path / "status.json",
    )
    runner.adapter = bridge
    runner.active_tickers = (BTC,)
    subscribe_calls = 0

    async def empty_stream():
        if False:
            yield None

    async def stopping_stream():
        runner.stop_event.set()
        if False:
            yield None

    class Session:
        async def subscribe_market_lifecycle(self, **_kwargs: object):
            nonlocal subscribe_calls
            subscribe_calls += 1
            return stopping_stream()

    try:
        await runner._pump_lifecycle_channel(
            Session(),
            empty_stream(),
            rollover=asyncio.Event(),
        )
        assert subscribe_calls == 1
        assert recorder.summary()["reconnect_count"] == 0
        diagnostic = recorder.connection.execute(
            "SELECT diagnostic_type FROM diagnostics ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert diagnostic == "SDK_LIFECYCLE_CHANNEL_ENDED"
    finally:
        recorder.close()


@pytest.mark.asyncio
async def test_orderbook_channel_end_quarantines_and_resubscribes_same_socket(
    tmp_path: Path,
) -> None:
    bridge, recorder = adapter(tmp_path, {BTC: Asset.BTC})
    bridge.accept(canonical(snapshot(BTC, 1), bridge.asset_by_ticker))
    runner = SdkReliabilityShadowRunner(
        settings=SimpleNamespace(),
        recorder=recorder,
        status_path=tmp_path / "status.json",
    )
    runner.adapter = bridge
    runner.active_tickers = (BTC,)
    subscribe_calls = 0

    async def empty_stream():
        if False:
            yield None

    async def stopping_stream():
        runner.stop_event.set()
        if False:
            yield None

    class Session:
        async def subscribe_orderbook_delta(self, **_kwargs: object):
            nonlocal subscribe_calls
            subscribe_calls += 1
            return stopping_stream()

    try:
        await runner._drain_orderbook_channel(
            Session(),
            empty_stream(),
            rollover=asyncio.Event(),
        )
        assert subscribe_calls == 1
        assert bridge.health(NOW + timedelta(seconds=1))["synchronized_count"] == 0
        assert recorder.summary()["reconnect_count"] == 0
        diagnostic = recorder.connection.execute(
            "SELECT diagnostic_type FROM diagnostics ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert diagnostic == "SDK_ORDERBOOK_CHANNEL_ENDED"
    finally:
        recorder.close()


@pytest.mark.asyncio
async def test_ticker_channel_end_resubscribes_without_socket_reconnect(tmp_path: Path) -> None:
    bridge, recorder = adapter(tmp_path, {BTC: Asset.BTC})
    runner = SdkReliabilityShadowRunner(
        settings=SimpleNamespace(),
        recorder=recorder,
        status_path=tmp_path / "status.json",
    )
    runner.adapter = bridge
    runner.active_tickers = (BTC,)
    subscribe_calls = 0

    async def empty_stream():
        if False:
            yield None

    async def stopping_stream():
        runner.stop_event.set()
        if False:
            yield None

    class Session:
        async def subscribe_ticker(self, **_kwargs: object):
            nonlocal subscribe_calls
            subscribe_calls += 1
            return stopping_stream()

    try:
        await runner._pump_ticker_channel(
            Session(),
            empty_stream(),
            rollover=asyncio.Event(),
        )
        assert subscribe_calls == 1
        assert recorder.summary()["reconnect_count"] == 0
        diagnostic = recorder.connection.execute(
            "SELECT diagnostic_type FROM diagnostics ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert diagnostic == "SDK_TICKER_CHANNEL_ENDED"
    finally:
        recorder.close()


@pytest.mark.asyncio
async def test_ambiguous_snapshot_emits_fail_closed_diagnostic(tmp_path: Path) -> None:
    gateway = object.__new__(KalshiWebSocketGateway)
    gateway._orderbook_feed = None
    feed = gateway.immutable_orderbook_stream(maxsize=10)
    stream = gateway.wire_diagnostic_stream()
    with pytest.raises(ValidationError):
        feed.load(
            json.dumps(
                {
                    "type": "orderbook_snapshot",
                    "sid": 1,
                    "seq": 2,
                    "msg": {"market_ticker": BTC, "market_id": "market"},
                }
            )
        )
    diagnostic = await asyncio.wait_for(anext(stream), timeout=1)
    assert diagnostic.diagnostic_kind == "MALFORMED_ORDERBOOK"
    bridge, recorder = adapter(tmp_path, {BTC: Asset.BTC})
    try:
        bridge.accept(canonical(snapshot(BTC, 1), bridge.asset_by_ticker))
        bridge.payload_invalidated(
            ticker=diagnostic.market_ticker,
            subscription_id=diagnostic.subscription_id,
            sequence=diagnostic.sequence,
            observed_at=NOW + timedelta(seconds=1),
            reason=diagnostic.wire_type,
        )
        health = bridge.health(NOW + timedelta(seconds=1))
        assert health["synchronized_count"] == 0
        assert health["assets"]["BTC"]["state"] == ReliabilityState.QUARANTINED.value
        assert recorder.summary()["payload_invalid_count"] == 1
        assert recorder.summary()["unrecovered_gap_count"] == 1
    finally:
        recorder.close()
