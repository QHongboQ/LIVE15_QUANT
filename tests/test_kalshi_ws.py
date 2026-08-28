from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path
from typing import Any

import pytest

import live15_quant.storage as storage_module
from live15_quant.config import Settings
from live15_quant.kalshi_ws import (
    KALSHI_WS_PROVENANCE,
    KalshiAtomicOrderBookCoordinator,
    KalshiAtomicSessionProcessor,
    KalshiBookInvariantError,
    KalshiBookSide,
    KalshiBookSyncStatus,
    KalshiCommandAcknowledged,
    KalshiOrderBookDelta,
    KalshiOrderBookSnapshot,
    KalshiRecoveryStage,
    KalshiSequenceGapError,
    KalshiSubscribed,
    KalshiSubscriptionRollover,
    KalshiTickerUpdate,
    KalshiUnsynchronizedBookError,
    KalshiWsPayloadError,
    KalshiWsPayloadIssue,
    KalshiWsProtocolNotice,
    KalshiWsRecoveryExhausted,
    parse_kalshi_server_message,
    replay_orderbook_events,
    subscribe_command,
    update_subscription_command,
)
from live15_quant.models import OrderBookLevel
from live15_quant.providers.kalshi_ws import (
    KalshiProductionCredentialFiles,
    KalshiProductionReadOnlyWebSocket,
    KalshiReadOnlyWsError,
    websocket_signature_message,
)
from live15_quant.storage import RecorderStorageError, RecorderStore

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
BTC = "KXBTC15M-26AUG211215-15"
BTC_NEXT = "KXBTC15M-26AUG211230-30"
MARKET_ID = "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1"


def snapshot(
    sequence: int = 10,
    *,
    ticker: str = BTC,
    market_id: str = MARKET_ID,
    connection_id: str = "connection-1",
) -> KalshiOrderBookSnapshot:
    return KalshiOrderBookSnapshot(
        connection_id=connection_id,
        subscription_id=2,
        sequence=sequence,
        ticker=ticker,
        market_id=market_id,
        yes_bids=(OrderBookLevel(Decimal("0.5000"), Decimal("12.00")),),
        no_bids=(OrderBookLevel(Decimal("0.4800"), Decimal("8.00")),),
        source_timestamp=None,
        socket_received_timestamp=NOW,
        parse_timestamp=NOW + timedelta(microseconds=20),
    )


def delta(
    sequence: int = 11,
    *,
    side: KalshiBookSide = KalshiBookSide.YES,
    price: Decimal = Decimal("0.5000"),
    quantity: Decimal = Decimal("-2.00"),
    ticker: str = BTC,
    market_id: str = MARKET_ID,
) -> KalshiOrderBookDelta:
    return KalshiOrderBookDelta(
        connection_id="connection-1",
        subscription_id=2,
        sequence=sequence,
        ticker=ticker,
        market_id=market_id,
        side=side,
        price=price,
        quantity_delta=quantity,
        source_timestamp=NOW,
        socket_received_timestamp=NOW + timedelta(milliseconds=5),
        parse_timestamp=NOW + timedelta(milliseconds=5, microseconds=20),
    )


def acknowledgement(sequence: int, *tickers: str) -> KalshiCommandAcknowledged:
    return KalshiCommandAcknowledged(
        connection_id="connection-1",
        request_id=99,
        subscription_id=2,
        sequence=sequence,
        market_tickers=tickers,
        socket_received_timestamp=NOW,
        parse_timestamp=NOW,
    )


def snapshot_payload(sequence: int = 10) -> str:
    return json.dumps(
        {
            "type": "orderbook_snapshot",
            "sid": 2,
            "seq": sequence,
            "msg": {
                "market_ticker": BTC,
                "market_id": MARKET_ID,
                "yes_dollars_fp": [["0.5000", "12.00"]],
                "no_dollars_fp": [["0.4800", "8.00"]],
            },
        }
    )


def test_official_payload_parser_preserves_decimal_and_timestamp_semantics() -> None:
    parsed = parse_kalshi_server_message(
        snapshot_payload(),
        connection_id="connection-1",
        socket_received_timestamp=NOW,
        parse_timestamp=NOW + timedelta(microseconds=20),
    )
    assert isinstance(parsed, KalshiOrderBookSnapshot)
    assert parsed.yes_bids[0] == OrderBookLevel(Decimal("0.5000"), Decimal("12.00"))
    delta_message = parse_kalshi_server_message(
        {
            "type": "orderbook_delta",
            "sid": 2,
            "seq": 11,
            "msg": {
                "market_ticker": BTC,
                "market_id": MARKET_ID,
                "price_dollars": "0.5000",
                "delta_fp": "-2.00",
                "side": "yes",
                "ts": "2026-08-21T12:00:00Z",
                "ts_ms": 1787313600000,
            },
        },
        connection_id="connection-1",
        socket_received_timestamp=NOW,
        parse_timestamp=NOW,
    )
    assert isinstance(delta_message, KalshiOrderBookDelta)
    assert delta_message.quantity_delta == Decimal("-2.00")
    assert delta_message.source_timestamp == NOW


def test_unified_yes_price_payload_is_normalized_once_to_canonical_no_leg_prices() -> None:
    """The wire subscription is unified yes-leg; LIVE15 remains no-leg internally."""

    parsed_snapshot = parse_kalshi_server_message(
        snapshot_payload(),
        connection_id="connection-1",
        socket_received_timestamp=NOW,
        parse_timestamp=NOW,
    )
    parsed_delta = parse_kalshi_server_message(
        {
            "type": "orderbook_delta",
            "sid": 2,
            "seq": 11,
            "msg": {
                "market_ticker": BTC,
                "market_id": MARKET_ID,
                # `use_yes_price=true`: this is 48c YES-leg, therefore 52c NO-leg.
                "price_dollars": "0.4800",
                "delta_fp": "2.00",
                "side": "no",
                "ts_ms": 1787313600000,
            },
        },
        connection_id="connection-1",
        socket_received_timestamp=NOW,
        parse_timestamp=NOW,
    )
    assert isinstance(parsed_snapshot, KalshiOrderBookSnapshot)
    assert isinstance(parsed_delta, KalshiOrderBookDelta)
    assert parsed_snapshot.no_bids == (OrderBookLevel(Decimal("0.5200"), Decimal("8.00")),)
    assert parsed_delta.side is KalshiBookSide.NO
    assert parsed_delta.price == Decimal("0.5200")


def test_ticker_is_typed_but_not_claimed_as_sequenced_orderbook() -> None:
    parsed = parse_kalshi_server_message(
        {
            "type": "ticker",
            "sid": 11,
            "msg": {
                "market_ticker": BTC,
                "market_id": MARKET_ID,
                "price_dollars": "0.510",
                "yes_bid_dollars": "0.500",
                "yes_ask_dollars": "0.520",
                "volume_fp": "33896.00",
                "ts": 1787313600,
                "ts_ms": 1787313600000,
            },
        },
        connection_id="connection-1",
        socket_received_timestamp=NOW,
        parse_timestamp=NOW,
    )
    assert isinstance(parsed, KalshiTickerUpdate)
    assert parsed.yes_bid == Decimal("0.500")
    assert not hasattr(parsed, "sequence")


def test_update_ack_preserves_subscription_sequence_for_replay() -> None:
    parsed = parse_kalshi_server_message(
        {
            "id": 99,
            "sid": 2,
            "seq": 11,
            "type": "ok",
            "msg": {"market_tickers": [BTC, BTC_NEXT]},
        },
        connection_id="connection-1",
        socket_received_timestamp=NOW,
        parse_timestamp=NOW,
    )
    assert isinstance(parsed, KalshiCommandAcknowledged)
    assert parsed.sequence == 11
    assert parsed.market_tickers == (BTC, BTC_NEXT)


def test_documented_subscription_control_message_is_dispatched_before_market_fields() -> None:
    parsed = parse_kalshi_server_message(
        {"id": 1, "type": "subscribed", "msg": {"channel": "ticker", "sid": 11}},
        connection_id="connection-1",
        socket_received_timestamp=NOW,
        parse_timestamp=NOW,
    )
    assert parsed == KalshiSubscribed(request_id=1, subscription_id=11, channel="ticker")


def test_initial_snapshot_and_sequential_delta_reconstruct_exact_depth() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    coordinator.accept(snapshot())
    book = coordinator.accept(delta())
    assert book.sequence == 11
    assert book.status is KalshiBookSyncStatus.SYNCHRONIZED
    assert book.yes_bids == (OrderBookLevel(Decimal("0.5000"), Decimal("10.00")),)
    assert book.no_bids == (OrderBookLevel(Decimal("0.4800"), Decimal("8.00")),)


@pytest.mark.parametrize("sequence,relation", [(10, "duplicate"), (9, "backward"), (12, "gap")])
def test_non_contiguous_sequence_invalidates_and_blocks_book(sequence: int, relation: str) -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    coordinator.accept(snapshot())
    with pytest.raises(KalshiSequenceGapError, match=relation) as caught:
        coordinator.accept(delta(sequence))
    assert caught.value.tickers == (BTC,)
    with pytest.raises(KalshiUnsynchronizedBookError):
        coordinator.book(BTC)


def test_get_snapshot_resynchronizes_after_gap() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    coordinator.accept(snapshot())
    with pytest.raises(KalshiSequenceGapError):
        coordinator.accept(delta(13))
    command = update_subscription_command(9, 2, "get_snapshot", (BTC,)).as_object()
    assert command["params"] == {
        "sids": [2],
        "market_tickers": [BTC],
        "action": "get_snapshot",
    }
    recovered = coordinator.accept(snapshot(20))
    assert recovered.status is KalshiBookSyncStatus.SYNCHRONIZED
    assert recovered.sequence == 20


def test_sequenced_subscription_ack_is_part_of_deterministic_sequence() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    coordinator.accept(snapshot())
    coordinator.accept_ack(acknowledgement(11, BTC, BTC_NEXT))
    book = coordinator.accept(delta(12))
    assert book is not None and book.sequence == 12


def test_multi_market_resync_blocks_every_book_until_all_snapshots_arrive() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC, BTC_NEXT))
    coordinator.accept(snapshot(10))
    coordinator.accept(snapshot(11, ticker=BTC_NEXT, market_id="successor-market"))
    with pytest.raises(KalshiSequenceGapError):
        coordinator.accept(delta(13))
    assert coordinator.accept(snapshot(20)) is None
    with pytest.raises(KalshiUnsynchronizedBookError):
        coordinator.book(BTC)
    recovered = coordinator.accept(snapshot(21, ticker=BTC_NEXT, market_id="successor-market"))
    assert recovered is not None
    assert coordinator.book(BTC).status is KalshiBookSyncStatus.SYNCHRONIZED


@pytest.mark.asyncio
async def test_session_processor_requests_one_snapshot_and_measures_resync() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    sent: list[str] = []

    async def sender(payload: str) -> None:
        sent.append(payload)

    clock = iter((10.0, 10.25)).__next__
    processor = KalshiAtomicSessionProcessor(coordinator, sender, monotonic=clock)
    assert await processor.process(snapshot()) is not None
    assert await processor.process(delta(13)) is None
    assert await processor.process(delta(14)) is None
    assert len(sent) == 1
    assert json.loads(sent[0])["params"]["action"] == "get_snapshot"
    recovered = await processor.process(snapshot(20))
    assert recovered is not None
    assert processor.diagnostics.requests == 1
    assert processor.diagnostics.completed == 1
    assert processor.diagnostics.last_duration_seconds == 0.25


@pytest.mark.asyncio
async def test_stalled_resync_uses_bounded_snapshot_resubscribe_reconnect_ladder() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    sent: list[str] = []

    async def sender(payload: str) -> None:
        sent.append(payload)

    processor = KalshiAtomicSessionProcessor(coordinator, sender)
    await processor.process(snapshot())
    await processor.process(delta(13))

    assert await processor.advance_recovery() is KalshiRecoveryStage.SNAPSHOT_RETRY
    assert len(sent) == 2
    assert json.loads(sent[-1])["params"]["action"] == "get_snapshot"
    assert await processor.advance_recovery() is KalshiRecoveryStage.RESUBSCRIBE
    command = processor.resubscribe_command().as_object()
    assert command["cmd"] == "subscribe"
    assert await processor.advance_recovery() is KalshiRecoveryStage.RECONNECT
    processor.mark_reconnect_requested()
    assert processor.diagnostics.snapshot_retries == 1
    assert processor.diagnostics.resubscribe_requests == 1
    assert processor.diagnostics.reconnect_requests == 1


@pytest.mark.asyncio
async def test_subscription_identity_conflict_requires_new_subscription_not_old_sid_snapshot() -> (
    None
):
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    sent: list[str] = []

    async def sender(payload: str) -> None:
        sent.append(payload)

    processor = KalshiAtomicSessionProcessor(coordinator, sender)
    await processor.process(snapshot())
    conflicting = replace(delta(), subscription_id=3)
    assert await processor.process(conflicting) is None
    command = processor.resubscribe_command().as_object()
    assert command["cmd"] == "subscribe"
    assert await processor.process(delta()) is None
    assert processor.accept_subscribed(KalshiSubscribed(1000, 3, "orderbook_delta"))
    assert await processor.process(replace(snapshot(20), subscription_id=3)) is not None
    assert sent == []
    assert processor.diagnostics.identity_recoveries == 1


@pytest.mark.asyncio
async def test_malformed_payload_with_wrong_sid_requires_new_subscription() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))

    async def sender(_payload: str) -> None:
        return None

    processor = KalshiAtomicSessionProcessor(coordinator, sender)
    await processor.process(snapshot())
    issue = KalshiWsPayloadIssue(
        connection_id="connection-1",
        message_type="orderbook_delta",
        channel=None,
        subscription_id=3,
        sequence=11,
        ticker=BTC,
        parser_stage="data_payload",
        reason="malformed payload",
        schema_keys=("top:type",),
        payload_shape_hash="0123456789abcdef",
        affects_orderbook=True,
        socket_received_timestamp=NOW,
        parse_timestamp=NOW,
    )

    await processor.recover_payload_issue(issue)

    assert processor.resubscribe_command().as_object()["cmd"] == "subscribe"
    assert processor.diagnostics.identity_recoveries == 1


@pytest.mark.asyncio
async def test_invariant_failure_resnapshots_instead_of_exposing_damaged_book() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    sent: list[str] = []

    async def sender(payload: str) -> None:
        sent.append(payload)

    processor = KalshiAtomicSessionProcessor(coordinator, sender)
    assert await processor.process(snapshot()) is not None
    assert await processor.process(delta(11, quantity=Decimal("-12.01"))) is None
    with pytest.raises(KalshiUnsynchronizedBookError):
        coordinator.book(BTC)
    assert json.loads(sent[-1])["params"]["action"] == "get_snapshot"
    recovered = replace(
        snapshot(20),
        yes_bids=(OrderBookLevel(Decimal("0.50"), Decimal("7")),),
    )
    book = await processor.process(recovered)
    assert book is not None and book.yes_bids[0].quantity == Decimal("7")
    assert processor.diagnostics.invariant_recoveries == 1


@pytest.mark.asyncio
async def test_repeated_invariant_recovery_is_bounded_by_connection_budget() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))

    async def sender(_payload: str) -> None:
        return None

    processor = KalshiAtomicSessionProcessor(
        coordinator,
        sender,
        max_payload_issues=1,
    )
    await processor.process(snapshot())
    await processor.process(delta(11, quantity=Decimal("-12.01")))
    await processor.process(snapshot(20))
    with pytest.raises(KalshiWsRecoveryExhausted, match="recovery budget exhausted"):
        await processor.process(delta(21, quantity=Decimal("-12.01")))
    with pytest.raises(KalshiUnsynchronizedBookError):
        coordinator.book(BTC)


@pytest.mark.asyncio
async def test_delta_interleaved_with_multi_market_resync_stays_blocked_until_complete() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC, BTC_NEXT))
    sent: list[str] = []

    async def sender(payload: str) -> None:
        sent.append(payload)

    processor = KalshiAtomicSessionProcessor(coordinator, sender)
    await processor.process(snapshot(10))
    await processor.process(snapshot(11, ticker=BTC_NEXT, market_id="successor-market"))
    assert await processor.process(delta(13)) is None
    assert await processor.process(snapshot(20)) is None
    # Contiguous traffic may arrive before the other market's resnapshot. It is
    # applied internally but cannot make a partial subscription consumable.
    assert await processor.process(delta(21)) is None
    with pytest.raises(KalshiUnsynchronizedBookError):
        coordinator.book(BTC)
    completed = await processor.process(snapshot(22, ticker=BTC_NEXT, market_id="successor-market"))
    assert completed is not None
    assert coordinator.book(BTC).sequence == 22


@pytest.mark.asyncio
async def test_malformed_subscription_payload_resnapshots_before_book_recovers() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    sent: list[str] = []

    async def sender(payload: str) -> None:
        sent.append(payload)

    processor = KalshiAtomicSessionProcessor(coordinator, sender)
    assert await processor.process(snapshot()) is not None
    issue = KalshiWsPayloadIssue(
        connection_id="connection-1",
        message_type="orderbook_delta",
        channel=None,
        subscription_id=2,
        sequence=11,
        ticker=BTC,
        parser_stage="data_payload",
        reason="malformed Kalshi WebSocket market_id",
        schema_keys=("top:type", "msg:market_ticker"),
        payload_shape_hash="0123456789abcdef",
        affects_orderbook=True,
        socket_received_timestamp=NOW,
        parse_timestamp=NOW,
    )
    await processor.recover_payload_issue(issue)
    with pytest.raises(KalshiUnsynchronizedBookError):
        coordinator.book(BTC)
    assert len(sent) == 1
    assert json.loads(sent[0])["params"]["action"] == "get_snapshot"
    recovered = await processor.process(snapshot(20))
    assert recovered is not None
    assert coordinator.book(BTC).sequence == 20


@pytest.mark.asyncio
async def test_repeated_local_payload_damage_exhausts_bounded_recovery_budget() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))

    async def sender(_payload: str) -> None:
        return None

    processor = KalshiAtomicSessionProcessor(coordinator, sender, max_payload_issues=2)
    issue = KalshiWsPayloadIssue(
        connection_id="connection-1",
        message_type="orderbook_delta",
        channel=None,
        subscription_id=2,
        sequence=11,
        ticker=BTC,
        parser_stage="data_payload",
        reason="malformed Kalshi WebSocket market_id",
        schema_keys=("top:type",),
        payload_shape_hash="0123456789abcdef",
        affects_orderbook=True,
        socket_received_timestamp=NOW,
        parse_timestamp=NOW,
    )
    await processor.recover_payload_issue(issue)
    assert await processor.process(snapshot(20)) is not None
    await processor.recover_payload_issue(issue)
    assert await processor.process(snapshot(30)) is not None
    with pytest.raises(KalshiWsRecoveryExhausted, match="recovery budget exhausted"):
        await processor.recover_payload_issue(issue)


def test_ticker_or_market_identity_conflict_fails_loudly() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    coordinator.accept(snapshot())
    with pytest.raises(KalshiBookInvariantError, match="identity conflict"):
        coordinator.accept(delta(market_id="different-market"))
    with pytest.raises(KalshiBookInvariantError, match="not subscribed"):
        coordinator.accept(delta(ticker="KXETH15M-OTHER"))


def test_negative_depth_is_impossible_and_zero_depth_removes_level() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    coordinator.accept(snapshot())
    book = coordinator.accept(delta(quantity=Decimal("-12.00")))
    assert book.yes_bids == ()
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    coordinator.accept(snapshot())
    with pytest.raises(KalshiBookInvariantError, match="negative depth"):
        coordinator.accept(delta(quantity=Decimal("-12.01")))
    with pytest.raises(KalshiUnsynchronizedBookError):
        coordinator.book(BTC)


def test_rollover_adds_successor_before_removing_predecessor() -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    coordinator.accept(snapshot())
    rollover = KalshiSubscriptionRollover(coordinator)
    add = rollover.add_successor(
        request_id=20,
        subscription_id=2,
        predecessor=BTC,
        successor=BTC_NEXT,
    ).as_object()
    assert add["params"]["action"] == "add_markets"
    with pytest.raises(KalshiUnsynchronizedBookError):
        rollover.successor_synchronized(request_id=21, subscription_id=2, successor=BTC_NEXT)
    coordinator.accept(snapshot(11, ticker=BTC_NEXT, market_id="successor-market"))
    remove = rollover.successor_synchronized(
        request_id=21, subscription_id=2, successor=BTC_NEXT
    ).as_object()
    assert remove["params"] == {
        "sids": [2],
        "market_tickers": [BTC],
        "action": "delete_markets",
    }
    rollover.predecessor_removed(BTC_NEXT)
    assert coordinator.subscribed_tickers == (BTC_NEXT,)


def test_subscribe_command_is_market_data_only() -> None:
    command = subscribe_command(1, (BTC,)).as_object()
    assert command == {
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta", "ticker"],
            "market_tickers": [BTC],
            "use_yes_price": True,
        },
    }
    serialized = json.dumps(command).lower()
    assert all(
        word not in serialized
        for word in ("submit_order", "create_order", "cancel_order", "account", "portfolio")
    )


def test_schema_v7_to_v8_storage_is_append_only_and_replay_deterministic(tmp_path: Path) -> None:
    path = tmp_path / "raw.sqlite3"
    with RecorderStore(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE kalshi_ws_orderbook_events")
    connection.execute("DROP TABLE kalshi_ws_book_checkpoints")
    connection.execute("UPDATE recorder_metadata SET value='7'")
    connection.commit()
    connection.close()
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    first = snapshot()
    second = delta()
    with RecorderStore(path) as store:
        first_book = coordinator.accept(first)
        assert store.append_kalshi_ws_orderbook_event(
            first, sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED
        )
        assert store.append_kalshi_ws_checkpoint(first_book)
        coordinator.accept(second)
        assert store.append_kalshi_ws_orderbook_event(
            second, sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED
        )
        ack = acknowledgement(12, BTC)
        coordinator.accept_ack(ack)
        assert store.append_kalshi_ws_orderbook_event(
            ack, sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED
        )
        assert not store.append_kalshi_ws_orderbook_event(
            second, sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED
        )
        records = tuple(store.replay_kalshi_ws_orderbook_events("connection-1", 2))
        replayed = replay_orderbook_events(records, (BTC,))
        assert replayed[BTC] == coordinator.book(BTC)
        assert len(tuple(store.replay_kalshi_ws_checkpoints(BTC))) == 1
        assert store.integrity_check() == "ok"
        assert store._connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_v7_to_v8_migration_rolls_back_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.sqlite3"
    with RecorderStore(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE kalshi_ws_book_checkpoints")
    connection.execute("DROP TABLE kalshi_ws_orderbook_events")
    connection.execute("UPDATE recorder_metadata SET value='7'")
    connection.commit()
    connection.close()

    monkeypatch.setattr(storage_module, "_KALSHI_WS_CHECKPOINT_TABLE_SQL", "INVALID SQL")
    with pytest.raises(sqlite3.Error):
        RecorderStore(path)

    connection = sqlite3.connect(path)
    try:
        version = connection.execute("SELECT value FROM recorder_metadata").fetchone()[0]
        ws_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'kalshi_ws_%'"
        ).fetchall()
    finally:
        connection.close()
    assert version == "7"
    assert ws_tables == []


def test_schema_v9_to_v10_adds_nullable_enqueue_timing_without_rewriting(tmp_path: Path) -> None:
    path = tmp_path / "v9.sqlite3"
    with RecorderStore(path) as store:
        store.append_kalshi_ws_orderbook_event(
            snapshot(), sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED
        )
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE kalshi_ws_orderbook_events DROP COLUMN enqueue_timestamp")
    connection.execute(
        "ALTER TABLE kalshi_ws_orderbook_events DROP COLUMN receive_enqueue_latency_ms"
    )
    connection.execute("UPDATE kalshi_ws_orderbook_events SET schema_version=9")
    connection.execute("UPDATE recorder_metadata SET value='9' WHERE key='schema_version'")
    connection.commit()
    connection.close()

    with RecorderStore(path) as migrated:
        record = next(migrated.replay_kalshi_ws_orderbook_events("connection-1", 2))
        version = migrated._connection.execute(
            "SELECT value FROM recorder_metadata WHERE key='schema_version'"
        ).fetchone()[0]
        assert version == "10"
        assert record.enqueue_timestamp is None
        assert record.receive_enqueue_latency_ms is None
        assert migrated.integrity_check() == "ok"


def test_persisted_gap_and_official_snapshot_resync_replay_deterministically(
    tmp_path: Path,
) -> None:
    coordinator = KalshiAtomicOrderBookCoordinator("connection-1", (BTC,))
    first = snapshot(10)
    missed = delta(13)
    ack = acknowledgement(14, BTC)
    recovered = snapshot(15)
    with RecorderStore(tmp_path / "resync.sqlite3") as store:
        coordinator.accept(first)
        store.append_kalshi_ws_orderbook_event(
            first, sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED
        )
        with pytest.raises(KalshiSequenceGapError):
            coordinator.accept(missed)
        store.append_kalshi_ws_orderbook_event(
            missed, sync_status_after=KalshiBookSyncStatus.UNSYNCHRONIZED
        )
        with pytest.raises(KalshiSequenceGapError):
            coordinator.accept_ack(ack)
        store.append_kalshi_ws_orderbook_event(
            ack, sync_status_after=KalshiBookSyncStatus.UNSYNCHRONIZED
        )
        coordinator.accept(recovered)
        store.append_kalshi_ws_orderbook_event(
            recovered, sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED
        )
        records = tuple(store.replay_kalshi_ws_orderbook_events("connection-1", 2))
    replayed = replay_orderbook_events(records, (BTC,))
    assert replayed[BTC] == coordinator.book(BTC)


def test_receive_to_persist_latency_uses_socket_monotonic_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = replace(snapshot(), socket_received_monotonic_ns=1_000_000)
    monkeypatch.setattr(storage_module.time, "perf_counter_ns", lambda: 2_500_000)
    with RecorderStore(tmp_path / "latency.sqlite3") as store:
        store.append_kalshi_ws_orderbook_event(
            message, sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED
        )
        record = next(store.replay_kalshi_ws_orderbook_events("connection-1", 2))
    assert record.receive_persist_latency_ms == Decimal("1.5")


def test_conflicting_same_connection_sequence_fails_loudly(tmp_path: Path) -> None:
    with RecorderStore(tmp_path / "raw.sqlite3") as store:
        message = snapshot()
        assert store.append_kalshi_ws_orderbook_event(
            message, sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED
        )
        with pytest.raises(RecorderStorageError, match="conflicting Kalshi WS fact"):
            store.append_kalshi_ws_orderbook_event(
                replace(message, market_id="conflict"),
                sync_status_after=KalshiBookSyncStatus.UNSYNCHRONIZED,
            )


def test_bounded_ws_batch_is_idempotent_and_conflicts_fail_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = replace(snapshot(10), socket_received_monotonic_ns=1_000_000)
    second = replace(delta(11), socket_received_monotonic_ns=1_500_000)
    monkeypatch.setattr(storage_module.time, "perf_counter_ns", lambda: 2_500_000)
    batch = (
        (first, KalshiBookSyncStatus.SYNCHRONIZED),
        (second, KalshiBookSyncStatus.SYNCHRONIZED),
    )
    with RecorderStore(tmp_path / "batch.sqlite3") as store:
        inserted, maximum_latency = store.append_kalshi_ws_orderbook_event_batch(batch)
        assert inserted == 2
        assert maximum_latency == Decimal("1.5")
        assert store.append_kalshi_ws_orderbook_event_batch(batch) == (0, None)
        with pytest.raises(RecorderStorageError, match="conflicting Kalshi WS fact"):
            store.append_kalshi_ws_orderbook_event_batch(
                ((replace(first, market_id="conflict"), KalshiBookSyncStatus.UNSYNCHRONIZED),)
            )
        records = tuple(store.replay_kalshi_ws_orderbook_events("connection-1", 2))
    assert len(records) == 2
    assert records[0].receive_persist_latency_ms == Decimal("1.5")
    assert records[1].receive_persist_latency_ms == Decimal("1")


def test_rest_and_ws_provenance_are_not_interchangeable() -> None:
    assert snapshot().provenance == KALSHI_WS_PROVENANCE
    assert snapshot().provenance != "kalshi_rest"


class _FakeSigner:
    def sign(self, message: bytes) -> str:
        assert message == b"1700000000000GET/trade-api/ws/v2"
        return "secret-signature"


class _FakeWebSocket:
    def __init__(self, messages: list[str | BaseException]) -> None:
        self.messages = deque(messages)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self.messages:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        value = self.messages.popleft()
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self) -> None:
        self.closed = True


class _FakeConnection:
    def __init__(self, websocket: _FakeWebSocket | None = None, error: Exception | None = None):
        self.websocket = websocket
        self.error = error

    async def __aenter__(self) -> _FakeWebSocket:
        if self.error is not None:
            raise self.error
        assert self.websocket is not None
        return self.websocket

    async def __aexit__(self, *_args: object) -> None:
        return None


def credentials(tmp_path: Path) -> KalshiProductionCredentialFiles:
    key_id = tmp_path / "production-key-id.txt"
    private = tmp_path / "production-private.pem"
    key_id.write_text("key-id", encoding="utf-8")
    private.write_text("not-loaded-with-fake-signer", encoding="utf-8")
    return KalshiProductionCredentialFiles(key_id, private)


@pytest.mark.asyncio
async def test_read_only_adapter_authenticates_reconnects_and_resubscribes(tmp_path: Path) -> None:
    first = _FakeConnection(error=OSError("offline"))
    websocket = _FakeWebSocket([snapshot_payload()])
    second = _FakeConnection(websocket)
    attempts = 0
    captured_headers: list[dict[str, str]] = []

    def connector(_url: str, **kwargs: Any) -> _FakeConnection:
        nonlocal attempts
        attempts += 1
        captured_headers.append(dict(kwargs["additional_headers"]))
        return first if attempts == 1 else second

    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    times = iter(NOW + timedelta(microseconds=offset) for offset in (0, 10, 20, 30, 40, 50, 60, 70))
    adapter = KalshiProductionReadOnlyWebSocket(
        credentials(tmp_path),
        connector=connector,
        signer=_FakeSigner(),
        clock=lambda: next(times),
        clock_ms=lambda: 1700000000000,
        monotonic=iter((0.0, 0.1, 1.0, 1.1)).__next__,
        sleeper=sleeper,
        connection_id_factory=lambda: "connection-1",
        repository_root=Path.cwd(),
    )
    message = await anext(adapter.messages((BTC,)))
    await adapter.close()
    assert isinstance(message, KalshiOrderBookSnapshot)
    assert attempts == 2 and sleeps == [0.5]
    assert adapter.diagnostics.reconnects == 1
    assert adapter.diagnostics.receive_queue_high_watermark >= 1
    assert json.loads(websocket.sent[0])["cmd"] == "subscribe"
    assert set(captured_headers[0]) == {
        "KALSHI-ACCESS-KEY",
        "KALSHI-ACCESS-SIGNATURE",
        "KALSHI-ACCESS-TIMESTAMP",
    }


@pytest.mark.asyncio
async def test_receive_queue_backpressure_is_lossless_and_bounded(tmp_path: Path) -> None:
    websocket = _FakeWebSocket([snapshot_payload(10), snapshot_payload(11)])
    adapter = KalshiProductionReadOnlyWebSocket(
        credentials(tmp_path),
        connector=lambda *_args, **_kwargs: _FakeConnection(websocket),
        signer=_FakeSigner(),
        clock_ms=lambda: 1700000000000,
        connection_id_factory=lambda: "connection-1",
        receive_queue_capacity=1,
        repository_root=Path.cwd(),
    )
    messages = adapter.messages((BTC,))
    first = await anext(messages)
    second = await anext(messages)
    await adapter.close()
    assert (first.sequence, second.sequence) == (10, 11)
    assert adapter.diagnostics.messages == 2
    assert adapter.diagnostics.receive_queue_high_watermark == 1


@pytest.mark.asyncio
async def test_adapter_payload_error_is_localized_without_raw_payload(
    tmp_path: Path,
) -> None:
    malformed = json.loads(snapshot_payload())
    del malformed["msg"]["market_id"]
    malformed["msg"]["private_token"] = "must-never-appear"
    websocket = _FakeWebSocket([json.dumps(malformed)])
    adapter = KalshiProductionReadOnlyWebSocket(
        credentials(tmp_path),
        connector=lambda *_args, **_kwargs: _FakeConnection(websocket),
        signer=_FakeSigner(),
        clock_ms=lambda: 1700000000000,
        connection_id_factory=lambda: "connection-1",
        repository_root=Path.cwd(),
    )

    issue = await anext(adapter.messages((BTC,)))
    await adapter.close()

    assert isinstance(issue, KalshiWsPayloadIssue)
    assert issue.message_type == "orderbook_snapshot"
    assert issue.ticker == BTC
    assert issue.subscription_id == 2 and issue.sequence == 10
    assert issue.affects_orderbook is True
    assert issue.reason == "malformed Kalshi WebSocket market_id"
    assert "must-never-appear" not in repr(issue)
    assert "private_token" not in issue.schema_keys
    assert adapter.diagnostics.payload_issues == 1


@pytest.mark.asyncio
async def test_unknown_non_data_message_is_a_benign_typed_notice(tmp_path: Path) -> None:
    websocket = _FakeWebSocket([json.dumps({"type": "status", "msg": {"state": "ok"}})])
    adapter = KalshiProductionReadOnlyWebSocket(
        credentials(tmp_path),
        connector=lambda *_args, **_kwargs: _FakeConnection(websocket),
        signer=_FakeSigner(),
        clock_ms=lambda: 1700000000000,
        connection_id_factory=lambda: "connection-1",
        repository_root=Path.cwd(),
    )

    notice = await anext(adapter.messages((BTC,)))
    await adapter.close()
    assert isinstance(notice, KalshiWsProtocolNotice)
    assert notice.message_type == "status"
    assert adapter.diagnostics.protocol_notices == 1


@pytest.mark.asyncio
async def test_only_parsed_market_data_advances_application_liveness(tmp_path: Path) -> None:
    """Control traffic must not make a stalled orderbook transport look fresh."""

    websocket = _FakeWebSocket(
        [
            json.dumps({"type": "status", "msg": {"state": "ok"}}),
            snapshot_payload(),
        ]
    )
    adapter = KalshiProductionReadOnlyWebSocket(
        credentials(tmp_path),
        connector=lambda *_args, **_kwargs: _FakeConnection(websocket),
        signer=_FakeSigner(),
        clock_ms=lambda: 1700000000000,
        connection_id_factory=lambda: "connection-1",
        repository_root=Path.cwd(),
    )

    messages = adapter.messages((BTC,))
    notice = await anext(messages)
    assert isinstance(notice, KalshiWsProtocolNotice)
    assert adapter.diagnostics.last_message_received_at is None

    market_data = await anext(messages)
    await adapter.close()
    assert isinstance(market_data, KalshiOrderBookSnapshot)
    assert adapter.diagnostics.last_message_received_at == market_data.socket_received_timestamp


@pytest.mark.asyncio
async def test_repeated_global_protocol_damage_reconnects_then_fails_loudly(tmp_path: Path) -> None:
    attempts = 0
    sleeps: list[float] = []

    def connector(_url: str, **_kwargs: Any) -> _FakeConnection:
        nonlocal attempts
        attempts += 1
        return _FakeConnection(_FakeWebSocket(["not-json"]))

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    adapter = KalshiProductionReadOnlyWebSocket(
        credentials(tmp_path),
        connector=connector,
        signer=_FakeSigner(),
        clock_ms=lambda: 1700000000000,
        sleeper=sleeper,
        connection_id_factory=lambda: f"connection-{attempts}",
        repository_root=Path.cwd(),
    )

    with pytest.raises(KalshiWsPayloadError, match="reason=malformed_json"):
        await anext(adapter.messages((BTC,)))
    await adapter.close()
    assert attempts == 3
    assert adapter.diagnostics.protocol_reconnects == 2
    assert sleeps == [0.5, 1.0]


@pytest.mark.asyncio
async def test_deterministic_burst_backpressure_is_lossless_and_cooperative(
    tmp_path: Path,
) -> None:
    burst = 10_000
    capacity = 32
    websocket = _FakeWebSocket([snapshot_payload(sequence + 1) for sequence in range(burst)])
    monotonic_ns = count(0, 100_000).__next__
    adapter = KalshiProductionReadOnlyWebSocket(
        credentials(tmp_path),
        connector=lambda *_args, **_kwargs: _FakeConnection(websocket),
        signer=_FakeSigner(),
        clock_ms=lambda: 1700000000000,
        perf_counter_ns=monotonic_ns,
        connection_id_factory=lambda: "connection-1",
        receive_queue_capacity=capacity,
        repository_root=Path.cwd(),
    )
    stop_pulse = asyncio.Event()
    pulse_count = 0

    async def pulse() -> None:
        nonlocal pulse_count
        while not stop_pulse.is_set():
            pulse_count += 1
            await asyncio.sleep(0)

    pulse_task = asyncio.create_task(pulse())
    messages = adapter.messages((BTC,))
    observed = [await anext(messages) for _ in range(burst)]
    stop_pulse.set()
    await pulse_task
    await adapter.close()
    diagnostics = adapter.diagnostics
    assert [message.sequence for message in observed] == list(range(1, burst + 1))
    assert diagnostics.receive_queue_enqueued == burst
    assert diagnostics.receive_queue_dequeued == burst
    assert diagnostics.receive_queue_depth == 0
    assert diagnostics.receive_queue_high_watermark == capacity
    assert diagnostics.receive_queue_full_waits > 0
    assert diagnostics.receive_queue_dropped == 0
    assert diagnostics.receive_queue_max_backlog_seconds > 0
    assert diagnostics.receive_queue_above_50_seconds > 0
    assert diagnostics.receive_queue_above_75_seconds > 0
    assert diagnostics.receive_queue_above_90_seconds > 0
    assert pulse_count > 0
    assert all(
        message.enqueue_monotonic_ns >= message.socket_received_monotonic_ns for message in observed
    )


def test_credential_repr_errors_and_interface_do_not_expose_secrets(tmp_path: Path) -> None:
    files = credentials(tmp_path)
    assert "key-id" not in repr(files)
    assert str(files.private_key_path) not in repr(files)
    assert websocket_signature_message("1700000000000") == (b"1700000000000GET/trade-api/ws/v2")
    outside = KalshiProductionCredentialFiles(
        tmp_path / "missing-id.txt", tmp_path / "missing-private.pem"
    )
    with pytest.raises(KalshiReadOnlyWsError) as caught:
        outside.validate(Path.cwd())
    assert str(tmp_path) not in str(caught.value)
    forbidden = {
        "submit_order",
        "create_order",
        "cancel_order",
        "get_balance",
        "get_positions",
        "get_account",
    }
    assert forbidden.isdisjoint(KalshiProductionReadOnlyWebSocket.__dict__)


def test_production_credential_validation_accepts_only_pem_text_file(tmp_path: Path) -> None:
    key_id = tmp_path / "production-key-id.txt"
    private_key = tmp_path / "production-private.txt"
    key_id.write_text("key-id", encoding="utf-8")
    private_key.write_text("-----BEGIN PRIVATE KEY-----\n", encoding="ascii")

    KalshiProductionCredentialFiles(key_id, private_key).validate(Path.cwd())

    private_key.write_text("not a key", encoding="ascii")
    with pytest.raises(KalshiReadOnlyWsError, match="must be PEM"):
        KalshiProductionCredentialFiles(key_id, private_key).validate(Path.cwd())


def test_settings_factory_stops_at_disabled_or_missing_production_credentials() -> None:
    with pytest.raises(KalshiReadOnlyWsError, match="not enabled"):
        KalshiProductionReadOnlyWebSocket.from_settings(Settings())
    with pytest.raises(KalshiReadOnlyWsError, match="not configured"):
        KalshiProductionReadOnlyWebSocket.from_settings(
            Settings(enable_kalshi_production_websocket=True)
        )


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        {"type": "orderbook_snapshot", "sid": 2, "seq": 1, "msg": {}},
        {
            "type": "orderbook_snapshot",
            "sid": 2,
            "seq": 1,
            "msg": {
                "market_ticker": BTC,
                "market_id": MARKET_ID,
                "yes_dollars_fp": [["0.5", "-1"]],
            },
        },
    ],
)
def test_malformed_payload_fails_closed(payload: object) -> None:
    with pytest.raises(KalshiWsPayloadError):
        parse_kalshi_server_message(
            payload,  # type: ignore[arg-type]
            connection_id="connection-1",
            socket_received_timestamp=NOW,
            parse_timestamp=NOW,
        )
