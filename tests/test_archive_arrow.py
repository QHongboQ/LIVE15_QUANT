from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest

from live15_quant.archive_arrow import (
    ArrowArchiveError,
    batch_to_records,
    read_ipc_snapshot,
    records_to_batch,
    write_ipc_snapshot,
)
from live15_quant.kalshi_ws import (
    KalshiBookSide,
    KalshiBookSyncStatus,
    KalshiWsEventKind,
    replay_orderbook_events,
)
from live15_quant.models import DataRole, OrderBookLevel
from live15_quant.records import KalshiWsOrderBookEventRecord

NOW = datetime(2026, 9, 3, 5, 40, tzinfo=UTC)
TICKER = "KXBTC15M-26SEP030145-45"


def record(sequence: int) -> KalshiWsOrderBookEventRecord:
    kind = (
        KalshiWsEventKind.SUBSCRIPTION_ACK
        if sequence == 1
        else KalshiWsEventKind.SNAPSHOT
        if sequence == 2
        else KalshiWsEventKind.DELTA
    )
    return KalshiWsOrderBookEventRecord(
        row_id=sequence,
        schema_version=10,
        connection_id="connection-arrow",
        subscription_id=11,
        sequence=sequence,
        event_kind=kind,
        ticker=None if sequence == 1 else TICKER,
        market_id=None if sequence == 1 else "market-arrow",
        market_tickers=(TICKER,) if sequence == 1 else (),
        side=KalshiBookSide.YES if sequence > 2 else None,
        price=Decimal("0.5000") if sequence > 2 else None,
        quantity_delta=Decimal("1.2500") if sequence > 2 else None,
        yes_bids=(OrderBookLevel(Decimal("0.5000"), Decimal("2.0000")),) if sequence == 2 else (),
        no_bids=(OrderBookLevel(Decimal("0.4900"), Decimal("3.0000")),) if sequence == 2 else (),
        source_timestamp=NOW + timedelta(microseconds=sequence),
        socket_received_timestamp=NOW + timedelta(microseconds=sequence * 2),
        enqueue_timestamp=NOW + timedelta(microseconds=sequence * 2 + 1),
        parse_timestamp=NOW + timedelta(microseconds=sequence * 2 + 2),
        persisted_timestamp=NOW + timedelta(microseconds=sequence * 2 + 3),
        receive_enqueue_latency_ms=Decimal("0.0010"),
        receive_persist_latency_ms=Decimal("0.0030"),
        sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED,
        provenance="kalshi_official_websocket",
        role=DataRole.CONTRACT_MARKET_QUOTE,
    )


def test_exact_batch_and_ipc_round_trip_and_replay_equivalence(tmp_path: Path) -> None:
    records = tuple(record(i) for i in range(1, 5))
    assert batch_to_records(records_to_batch(records)) == records
    path = tmp_path / "snapshot.arrow"
    assert write_ipc_snapshot(path, records) == path.stat().st_size
    decoded = read_ipc_snapshot(path)
    assert decoded == records
    assert [item.row_id for item in decoded] == [item.row_id for item in records]
    before, after = (
        replay_orderbook_events(records, (TICKER,))[TICKER],
        replay_orderbook_events(decoded, (TICKER,))[TICKER],
    )
    assert after == before


def test_zstd_is_owned_by_pyarrow(tmp_path: Path) -> None:
    write_ipc_snapshot(tmp_path / "compressed.arrow", (record(1), record(2)))
    assert pa.Codec.is_available("zstd")
    assert pa.Codec("zstd").name == "zstd"


def test_decimal_and_timestamp_contract_is_lossless(tmp_path: Path) -> None:
    special = replace(
        record(3),
        price=Decimal("1.2300E-19"),
        quantity_delta=Decimal("999999999999999999999999999999999999999.000"),
        receive_enqueue_latency_ms=Decimal("0E-100"),
    )
    path = tmp_path / "decimal.arrow"
    write_ipc_snapshot(path, (record(1), record(2), special))
    decoded = read_ipc_snapshot(path)[-1]
    assert decoded.price is not None and decoded.price.as_tuple() == special.price.as_tuple()
    assert (
        decoded.quantity_delta is not None
        and decoded.quantity_delta.as_tuple() == special.quantity_delta.as_tuple()
    )
    assert (
        decoded.receive_enqueue_latency_ms is not None
        and decoded.receive_enqueue_latency_ms.as_tuple()
        == special.receive_enqueue_latency_ms.as_tuple()
    )
    assert (
        decoded.parse_timestamp == special.parse_timestamp and decoded.parse_timestamp.tzinfo == UTC
    )


@pytest.mark.parametrize(
    ("records", "match"),
    [
        ((), "cannot be empty"),
        ((record(2), record(1)), "ascending row ids"),
        ((record(1), replace(record(2), subscription_id=12)), "one subscription stream"),
        ((replace(record(1), ticker=TICKER),), "subscription_ack"),
        ((replace(record(3), yes_bids=(OrderBookLevel(Decimal("1"), Decimal("1")),)),), "delta"),
    ],
)
def test_invalid_input_fails_closed(
    records: tuple[KalshiWsOrderBookEventRecord, ...], match: str
) -> None:
    with pytest.raises(ArrowArchiveError, match=match):
        records_to_batch(records)


def test_naive_timestamp_and_nonfinite_decimal_fail_closed() -> None:
    with pytest.raises(ArrowArchiveError, match="timezone-aware"):
        records_to_batch((replace(record(1), parse_timestamp=datetime(2026, 9, 3, 5, 40)),))
    with pytest.raises(ArrowArchiveError, match="finite Decimal"):
        records_to_batch((replace(record(1), receive_enqueue_latency_ms=Decimal("NaN")),))


@pytest.mark.parametrize("payload", [b"not Arrow", b"ARROW1", b""])
def test_corrupt_or_truncated_ipc_fails_loudly(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "bad.arrow"
    path.write_bytes(payload)
    with pytest.raises(ArrowArchiveError, match="cannot be decoded"):
        read_ipc_snapshot(path)


def test_truncated_valid_ipc_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "truncated.arrow"
    write_ipc_snapshot(path, (record(1), record(2)))
    path.write_bytes(path.read_bytes()[:-8])
    with pytest.raises(ArrowArchiveError, match="cannot be decoded"):
        read_ipc_snapshot(path)
