"""Bounded Apache Arrow IPC + ZSTD prototype for immutable Kalshi WS snapshots.

This module deliberately does not own Recorder scheduling, manifests, retention, purge,
or Production deployment.  It only adapts existing LIVE15 replay-truth records into a
standard Arrow RecordBatch and reads/writes one fixed immutable IPC file.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa

from live15_quant.kalshi_ws import KalshiBookSide, KalshiBookSyncStatus, KalshiWsEventKind
from live15_quant.models import DataRole, OrderBookLevel
from live15_quant.records import KalshiWsOrderBookEventRecord

ARROW_ARCHIVE_SCHEMA_VERSION = 1

_LEVEL_TYPE = pa.struct(
    [
        pa.field("price_decimal", pa.string(), nullable=False),
        pa.field("quantity_decimal", pa.string(), nullable=False),
    ]
)

ARROW_WS_EVENT_SCHEMA = pa.schema(
    [
        pa.field("archive_schema_version", pa.int16(), nullable=False),
        pa.field("row_id", pa.int64(), nullable=False),
        pa.field("schema_version", pa.int32(), nullable=False),
        pa.field("connection_id", pa.string(), nullable=False),
        pa.field("subscription_id", pa.int64(), nullable=False),
        pa.field("sequence", pa.int64(), nullable=False),
        pa.field("event_kind", pa.string(), nullable=False),
        pa.field("ticker", pa.string()),
        pa.field("market_id", pa.string()),
        pa.field("market_tickers", pa.list_(pa.string()), nullable=False),
        pa.field("side", pa.string()),
        pa.field("price_decimal", pa.string()),
        pa.field("quantity_delta_decimal", pa.string()),
        pa.field("yes_bids", pa.list_(_LEVEL_TYPE), nullable=False),
        pa.field("no_bids", pa.list_(_LEVEL_TYPE), nullable=False),
        pa.field("source_timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("socket_received_timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("enqueue_timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("parse_timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("persisted_timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("receive_enqueue_latency_decimal", pa.string()),
        pa.field("receive_persist_latency_decimal", pa.string()),
        pa.field("sync_status_after", pa.string(), nullable=False),
        pa.field("provenance", pa.string(), nullable=False),
        pa.field("role", pa.string(), nullable=False),
    ],
    metadata={
        b"live15.archive.format": b"arrow-ipc",
        b"live15.archive.schema_version": b"1",
        b"live15.compression": b"zstd",
        b"live15.decimal.encoding": b"canonical-string",
        b"live15.truth": b"kalshi-ws-replay",
    },
)


class ArrowArchiveError(RuntimeError):
    """A bounded Arrow archive snapshot is invalid or semantically incompatible."""


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _utc(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ArrowArchiveError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _level_values(levels: tuple[OrderBookLevel, ...]) -> list[dict[str, str]]:
    return [
        {"price_decimal": str(level.price), "quantity_decimal": str(level.quantity)}
        for level in levels
    ]


def _validate_records(records: Sequence[KalshiWsOrderBookEventRecord]) -> None:
    if not records:
        raise ArrowArchiveError("Arrow snapshot cannot be empty")
    row_ids = [record.row_id for record in records]
    if row_ids != sorted(row_ids) or len(set(row_ids)) != len(row_ids):
        raise ArrowArchiveError("Arrow snapshot requires unique ascending row ids")
    identities = {(record.connection_id, record.subscription_id) for record in records}
    sequences = [record.sequence for record in records]
    if len(identities) != 1:
        raise ArrowArchiveError("Arrow snapshot must contain one subscription stream")
    if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
        raise ArrowArchiveError("Arrow snapshot requires unique ascending sequences")


def records_to_batch(records: Sequence[KalshiWsOrderBookEventRecord]) -> pa.RecordBatch:
    """Convert one ordered LIVE15 replay stream to a typed Arrow RecordBatch."""

    _validate_records(records)
    rows = []
    for record in records:
        rows.append(
            {
                "archive_schema_version": ARROW_ARCHIVE_SCHEMA_VERSION,
                "row_id": record.row_id,
                "schema_version": record.schema_version,
                "connection_id": record.connection_id,
                "subscription_id": record.subscription_id,
                "sequence": record.sequence,
                "event_kind": record.event_kind.value,
                "ticker": record.ticker,
                "market_id": record.market_id,
                "market_tickers": list(record.market_tickers),
                "side": None if record.side is None else record.side.value,
                "price_decimal": _decimal_text(record.price),
                "quantity_delta_decimal": _decimal_text(record.quantity_delta),
                "yes_bids": _level_values(record.yes_bids),
                "no_bids": _level_values(record.no_bids),
                "source_timestamp": _utc(record.source_timestamp, "source_timestamp"),
                "socket_received_timestamp": _utc(
                    record.socket_received_timestamp, "socket_received_timestamp"
                ),
                "enqueue_timestamp": _utc(record.enqueue_timestamp, "enqueue_timestamp"),
                "parse_timestamp": _utc(record.parse_timestamp, "parse_timestamp"),
                "persisted_timestamp": _utc(record.persisted_timestamp, "persisted_timestamp"),
                "receive_enqueue_latency_decimal": _decimal_text(
                    record.receive_enqueue_latency_ms
                ),
                "receive_persist_latency_decimal": _decimal_text(
                    record.receive_persist_latency_ms
                ),
                "sync_status_after": record.sync_status_after.value,
                "provenance": record.provenance,
                "role": record.role.value,
            }
        )
    return pa.RecordBatch.from_pylist(rows, schema=ARROW_WS_EVENT_SCHEMA)


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _levels(value: object) -> tuple[OrderBookLevel, ...]:
    if not isinstance(value, list):
        raise ArrowArchiveError("Arrow order-book levels are malformed")
    levels = []
    for item in value:
        if not isinstance(item, dict):
            raise ArrowArchiveError("Arrow order-book level is malformed")
        levels.append(
            OrderBookLevel(
                Decimal(str(item["price_decimal"])),
                Decimal(str(item["quantity_decimal"])),
            )
        )
    return tuple(levels)


def batch_to_records(batch: pa.RecordBatch) -> tuple[KalshiWsOrderBookEventRecord, ...]:
    """Reconstruct LIVE15 replay records without float conversion."""

    if batch.schema != ARROW_WS_EVENT_SCHEMA:
        raise ArrowArchiveError("Arrow snapshot schema does not match LIVE15 archive schema")
    records = []
    for row in batch.to_pylist():
        if row["archive_schema_version"] != ARROW_ARCHIVE_SCHEMA_VERSION:
            raise ArrowArchiveError("Arrow archive schema version is unsupported")
        records.append(
            KalshiWsOrderBookEventRecord(
                row_id=int(row["row_id"]),
                schema_version=int(row["schema_version"]),
                connection_id=str(row["connection_id"]),
                subscription_id=int(row["subscription_id"]),
                sequence=int(row["sequence"]),
                event_kind=KalshiWsEventKind(str(row["event_kind"])),
                ticker=None if row["ticker"] is None else str(row["ticker"]),
                market_id=None if row["market_id"] is None else str(row["market_id"]),
                market_tickers=tuple(str(value) for value in row["market_tickers"]),
                side=None if row["side"] is None else KalshiBookSide(str(row["side"])),
                price=_decimal(row["price_decimal"]),
                quantity_delta=_decimal(row["quantity_delta_decimal"]),
                yes_bids=_levels(row["yes_bids"]),
                no_bids=_levels(row["no_bids"]),
                source_timestamp=row["source_timestamp"],
                socket_received_timestamp=row["socket_received_timestamp"],
                enqueue_timestamp=row["enqueue_timestamp"],
                parse_timestamp=row["parse_timestamp"],
                persisted_timestamp=row["persisted_timestamp"],
                receive_enqueue_latency_ms=_decimal(row["receive_enqueue_latency_decimal"]),
                receive_persist_latency_ms=_decimal(row["receive_persist_latency_decimal"]),
                sync_status_after=KalshiBookSyncStatus(str(row["sync_status_after"])),
                provenance=str(row["provenance"]),
                role=DataRole(str(row["role"])),
            )
        )
    result = tuple(records)
    _validate_records(result)
    return result


def write_ipc_snapshot(
    path: Path,
    records: Sequence[KalshiWsOrderBookEventRecord],
    *,
    compression_level: int = 3,
) -> int:
    """Atomically publish one fixed Arrow IPC file compressed by upstream ZSTD."""

    if not pa.Codec.is_available("zstd"):
        raise ArrowArchiveError("PyArrow was built without ZSTD support")
    batch = records_to_batch(records)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    codec = pa.Codec("zstd", compression_level=compression_level)
    options = pa.ipc.IpcWriteOptions(compression=codec)
    try:
        with pa.OSFile(str(temporary), "wb") as sink:
            with pa.ipc.new_file(sink, batch.schema, options=options) as writer:
                writer.write_batch(batch)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return path.stat().st_size


def read_ipc_snapshot(path: Path) -> tuple[KalshiWsOrderBookEventRecord, ...]:
    """Read one fixed Arrow IPC snapshot and enforce the LIVE15 schema contract."""

    with pa.memory_map(str(path), "r") as source:
        reader = pa.ipc.open_file(source)
        if reader.schema != ARROW_WS_EVENT_SCHEMA:
            raise ArrowArchiveError("Arrow IPC file schema does not match LIVE15 archive schema")
        if reader.num_record_batches != 1:
            raise ArrowArchiveError("Arrow IPC prototype expects exactly one record batch")
        return batch_to_records(reader.get_batch(0))
