from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest

from live15_quant.archive_arrow import (
    ARROW_WS_EVENT_SCHEMA,
    ArrowArchiveError,
    read_ipc_snapshot,
    records_to_batch,
    write_ipc_snapshot,
)
from live15_quant.kalshi_ws import KalshiBookSide, KalshiBookSyncStatus, KalshiWsEventKind
from live15_quant.models import DataRole, OrderBookLevel
from live15_quant.records import KalshiWsOrderBookEventRecord

NOW = datetime(2026, 9, 3, 5, 40, tzinfo=UTC)
TICKER = "KXBTC15M-26SEP030145-45"


def record(sequence: int) -> KalshiWsOrderBookEventRecord:
    kind = (
        KalshiWsEventKind.SUBSCRIPTION_ACK
        if sequence == 1
        else (KalshiWsEventKind.SNAPSHOT if sequence == 2 else KalshiWsEventKind.DELTA)
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


def test_arrow_batch_round_trip_preserves_exact_live15_semantics() -> None:
    records = tuple(record(sequence) for sequence in range(1, 5))
    batch = records_to_batch(records)

    assert batch.schema == ARROW_WS_EVENT_SCHEMA
    assert batch.num_rows == 4
    assert batch.column("price_decimal")[3].as_py() == "0.5000"
    assert batch.column("quantity_delta_decimal")[3].as_py() == "1.2500"


def test_arrow_ipc_zstd_file_round_trip_is_exact_and_atomic(tmp_path: Path) -> None:
    records = tuple(record(sequence) for sequence in range(1, 5))
    path = tmp_path / "snapshot.arrow"

    size = write_ipc_snapshot(path, records, compression_level=3)
    decoded = read_ipc_snapshot(path)

    assert size == path.stat().st_size
    assert decoded == records
    assert decoded[-1].price is not None
    assert decoded[-1].price.as_tuple() == Decimal("0.5000").as_tuple()
    assert decoded[-1].quantity_delta is not None
    assert decoded[-1].quantity_delta.as_tuple() == Decimal("1.2500").as_tuple()
    assert not (tmp_path / ".snapshot.arrow.tmp").exists()


def test_arrow_ipc_uses_zstd_capable_upstream_codec() -> None:
    assert pa.Codec.is_available("zstd")
    assert pa.Codec("zstd", compression_level=3).name == "zstd"


def test_arrow_snapshot_rejects_mixed_streams_and_nonascending_rows() -> None:
    first = record(1)
    with pytest.raises(ArrowArchiveError, match="one subscription stream"):
        records_to_batch((first, replace(record(2), subscription_id=12)))
    with pytest.raises(ArrowArchiveError, match="ascending row ids"):
        records_to_batch((record(2), record(1)))


def test_arrow_snapshot_rejects_naive_timestamps() -> None:
    with pytest.raises(ArrowArchiveError, match="timezone-aware"):
        records_to_batch((replace(record(1), parse_timestamp=datetime(2026, 9, 3, 5, 40)),))
