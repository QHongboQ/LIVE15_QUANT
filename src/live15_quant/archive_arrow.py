"""Fixed-snapshot Arrow IPC adapter for LIVE15 Kalshi WebSocket replay records.

This is not wired into Recorder, archive publication, retention, or purge. PyArrow
owns arrays, IPC encoding/decoding, and Zstandard; LIVE15 owns record mapping/validation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from live15_quant.kalshi_ws import KalshiBookSide, KalshiBookSyncStatus, KalshiWsEventKind
from live15_quant.models import DataRole, OrderBookLevel
from live15_quant.records import KalshiWsOrderBookEventRecord

ARROW_ARCHIVE_SCHEMA_VERSION = 1
_LEVEL = pa.struct(
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
        # Fixed decimal128/256 bounds would be speculative; preserve every finite Decimal
        # exactly as text.
        pa.field("price_decimal", pa.string()),
        pa.field("quantity_delta_decimal", pa.string()),
        pa.field("yes_bids", pa.list_(_LEVEL), nullable=False),
        pa.field("no_bids", pa.list_(_LEVEL), nullable=False),
        # Recorder's persisted contract is UTC with Python's microsecond precision.
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
        b"live15.archive.format": b"arrow-ipc-file",
        b"live15.archive.schema_version": b"1",
        b"live15.compression": b"zstd",
        b"live15.decimal.encoding": b"decimal-string-no-fixed-bound",
        b"live15.timestamp.contract": b"utc-microseconds",
        b"live15.truth": b"kalshi-ws-replay",
    },
)


class ArrowArchiveError(RuntimeError):
    """An Arrow snapshot violates the fixed LIVE15 replay contract."""


PARQUET_COMPRESSION = "zstd"
PARQUET_ROW_GROUP_SIZE = 100_000


def _decimal_text(value: Decimal | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ArrowArchiveError(f"{field} must be a finite Decimal")
    return str(value)


def _decimal(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ArrowArchiveError(f"{field} is malformed")
    try:
        result = Decimal(value)
    except Exception as error:
        raise ArrowArchiveError(f"{field} is malformed") from error
    if not result.is_finite():
        raise ArrowArchiveError(f"{field} must be finite")
    return result


def _utc(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ArrowArchiveError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _levels_to_values(levels: tuple[OrderBookLevel, ...]) -> list[dict[str, str]]:
    if not isinstance(levels, tuple):
        raise ArrowArchiveError("order-book levels must be tuples")
    result = []
    for level in levels:
        if not isinstance(level, OrderBookLevel):
            raise ArrowArchiveError("order-book level is malformed")
        result.append(
            {
                "price_decimal": _decimal_text(level.price, "order-book price"),
                "quantity_decimal": _decimal_text(level.quantity, "order-book quantity"),
            }
        )
    return result


def _levels(value: object) -> tuple[OrderBookLevel, ...]:
    if not isinstance(value, list):
        raise ArrowArchiveError("Arrow order-book levels are malformed")
    levels = []
    for item in value:
        if not isinstance(item, dict):
            raise ArrowArchiveError("Arrow order-book level is malformed")
        try:
            levels.append(
                OrderBookLevel(
                    _decimal(item["price_decimal"], "order-book price"),
                    _decimal(item["quantity_decimal"], "order-book quantity"),
                )
            )
        except (KeyError, TypeError) as error:
            raise ArrowArchiveError("Arrow order-book level is malformed") from error
    return tuple(levels)


def _validate_record(record: KalshiWsOrderBookEventRecord) -> None:
    if not isinstance(record, KalshiWsOrderBookEventRecord):
        raise ArrowArchiveError("snapshot contains a non-LIVE15 record")
    if (
        record.row_id < 1
        or record.subscription_id < 1
        or record.sequence < 1
        or not record.connection_id
        or not record.provenance
    ):
        raise ArrowArchiveError("record identity/provenance is invalid")
    if any(not isinstance(ticker, str) or not ticker for ticker in record.market_tickers):
        raise ArrowArchiveError("market_tickers is malformed")
    for field in (
        "source_timestamp",
        "socket_received_timestamp",
        "enqueue_timestamp",
        "parse_timestamp",
        "persisted_timestamp",
    ):
        _utc(getattr(record, field), field)
    for field in (
        "price",
        "quantity_delta",
        "receive_enqueue_latency_ms",
        "receive_persist_latency_ms",
    ):
        _decimal_text(getattr(record, field), field)
    _levels_to_values(record.yes_bids)
    _levels_to_values(record.no_bids)
    if record.event_kind is KalshiWsEventKind.SUBSCRIPTION_ACK:
        valid = not any(
            (
                record.ticker,
                record.market_id,
                record.side,
                record.price,
                record.quantity_delta,
                record.yes_bids,
                record.no_bids,
            )
        )
    elif record.event_kind is KalshiWsEventKind.SNAPSHOT:
        valid = bool(record.ticker and record.market_id) and not any(
            (record.side, record.price, record.quantity_delta)
        )
    elif record.event_kind is KalshiWsEventKind.DELTA:
        valid = (
            bool(record.ticker and record.market_id)
            and None not in (record.side, record.price, record.quantity_delta)
            and not record.yes_bids
            and not record.no_bids
        )
    else:
        valid = False
    if not valid:
        raise ArrowArchiveError(f"{record.event_kind.value} fields are malformed")


def _validate_records(records: Sequence[KalshiWsOrderBookEventRecord]) -> None:
    if not records:
        raise ArrowArchiveError("Arrow snapshot cannot be empty")
    for record in records:
        _validate_record(record)
    rows, sequences = [r.row_id for r in records], [r.sequence for r in records]
    if rows != sorted(rows) or len(set(rows)) != len(rows):
        raise ArrowArchiveError("Arrow snapshot requires unique ascending row ids")
    if len({(r.connection_id, r.subscription_id) for r in records}) != 1:
        raise ArrowArchiveError("Arrow snapshot must contain one subscription stream")
    if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
        raise ArrowArchiveError("Arrow snapshot requires unique ascending sequences")


def records_to_batch(records: Sequence[KalshiWsOrderBookEventRecord]) -> pa.RecordBatch:
    """Map one ordered LIVE15 replay stream into a typed Arrow RecordBatch."""
    _validate_records(records)

    def row(r: KalshiWsOrderBookEventRecord) -> dict[str, object]:
        return {
            "archive_schema_version": ARROW_ARCHIVE_SCHEMA_VERSION,
            "row_id": r.row_id,
            "schema_version": r.schema_version,
            "connection_id": r.connection_id,
            "subscription_id": r.subscription_id,
            "sequence": r.sequence,
            "event_kind": r.event_kind.value,
            "ticker": r.ticker,
            "market_id": r.market_id,
            "market_tickers": list(r.market_tickers),
            "side": None if r.side is None else r.side.value,
            "price_decimal": _decimal_text(r.price, "price"),
            "quantity_delta_decimal": _decimal_text(r.quantity_delta, "quantity_delta"),
            "yes_bids": _levels_to_values(r.yes_bids),
            "no_bids": _levels_to_values(r.no_bids),
            "source_timestamp": _utc(r.source_timestamp, "source_timestamp"),
            "socket_received_timestamp": _utc(
                r.socket_received_timestamp, "socket_received_timestamp"
            ),
            "enqueue_timestamp": _utc(r.enqueue_timestamp, "enqueue_timestamp"),
            "parse_timestamp": _utc(r.parse_timestamp, "parse_timestamp"),
            "persisted_timestamp": _utc(r.persisted_timestamp, "persisted_timestamp"),
            "receive_enqueue_latency_decimal": _decimal_text(
                r.receive_enqueue_latency_ms, "receive_enqueue_latency_ms"
            ),
            "receive_persist_latency_decimal": _decimal_text(
                r.receive_persist_latency_ms, "receive_persist_latency_ms"
            ),
            "sync_status_after": r.sync_status_after.value,
            "provenance": r.provenance,
            "role": r.role.value,
        }

    return pa.RecordBatch.from_pylist(
        [row(record) for record in records], schema=ARROW_WS_EVENT_SCHEMA
    )


def batch_to_records(batch: pa.RecordBatch) -> tuple[KalshiWsOrderBookEventRecord, ...]:
    """Map an Arrow batch back to exact LIVE15 records without float conversion."""
    if not isinstance(batch, pa.RecordBatch) or batch.schema != ARROW_WS_EVENT_SCHEMA:
        raise ArrowArchiveError("Arrow snapshot schema does not match the LIVE15 archive schema")
    try:
        result = tuple(
            KalshiWsOrderBookEventRecord(
                row_id=r["row_id"],
                schema_version=r["schema_version"],
                connection_id=r["connection_id"],
                subscription_id=r["subscription_id"],
                sequence=r["sequence"],
                event_kind=KalshiWsEventKind(r["event_kind"]),
                ticker=r["ticker"],
                market_id=r["market_id"],
                market_tickers=tuple(r["market_tickers"]),
                side=None if r["side"] is None else KalshiBookSide(r["side"]),
                price=_decimal(r["price_decimal"], "price"),
                quantity_delta=_decimal(r["quantity_delta_decimal"], "quantity_delta"),
                yes_bids=_levels(r["yes_bids"]),
                no_bids=_levels(r["no_bids"]),
                source_timestamp=_utc(r["source_timestamp"], "source_timestamp"),
                socket_received_timestamp=_utc(
                    r["socket_received_timestamp"], "socket_received_timestamp"
                ),
                enqueue_timestamp=_utc(r["enqueue_timestamp"], "enqueue_timestamp"),
                parse_timestamp=_utc(r["parse_timestamp"], "parse_timestamp"),
                persisted_timestamp=_utc(r["persisted_timestamp"], "persisted_timestamp"),
                receive_enqueue_latency_ms=_decimal(
                    r["receive_enqueue_latency_decimal"], "receive_enqueue_latency_ms"
                ),
                receive_persist_latency_ms=_decimal(
                    r["receive_persist_latency_decimal"], "receive_persist_latency_ms"
                ),
                sync_status_after=KalshiBookSyncStatus(r["sync_status_after"]),
                provenance=r["provenance"],
                role=DataRole(r["role"]),
            )
            for r in batch.to_pylist()
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ArrowArchiveError("Arrow snapshot contains malformed LIVE15 values") from error
    _validate_records(result)
    return result


def write_ipc_snapshot(
    path: Path,
    records: Sequence[KalshiWsOrderBookEventRecord],
    *,
    compression_level: int | None = None,
) -> int:
    """Write one Arrow IPC file using PyArrow's Zstandard codec."""
    if not pa.Codec.is_available("zstd"):
        raise ArrowArchiveError("PyArrow was built without Zstandard support")
    batch = records_to_batch(records)
    with pa.OSFile(str(path), "wb") as sink:
        with pa.ipc.new_file(
            sink,
            batch.schema,
            options=pa.ipc.IpcWriteOptions(compression=pa.Codec("zstd", compression_level)),
        ) as writer:
            writer.write_batch(batch)
    return path.stat().st_size


def read_ipc_snapshot(path: Path) -> tuple[KalshiWsOrderBookEventRecord, ...]:
    """Read one fixed Arrow IPC file and enforce the LIVE15 schema contract."""
    try:
        with pa.memory_map(str(path), "r") as source:
            reader = pa.ipc.open_file(source)
            if reader.schema != ARROW_WS_EVENT_SCHEMA:
                raise ArrowArchiveError("Arrow IPC file schema does not match the LIVE15 contract")
            if reader.num_record_batches != 1:
                raise ArrowArchiveError("Arrow IPC prototype expects exactly one record batch")
            return batch_to_records(reader.get_batch(0))
    except ArrowArchiveError:
        raise
    except (OSError, pa.ArrowException) as error:
        raise ArrowArchiveError("Arrow IPC snapshot cannot be decoded") from error


def write_parquet_snapshot(path: Path, records: Sequence[KalshiWsOrderBookEventRecord]) -> int:
    """Write one exact LIVE15 stream with upstream Parquet+ZSTD defaults."""

    if not pa.Codec.is_available(PARQUET_COMPRESSION):
        raise ArrowArchiveError("PyArrow was built without Zstandard support")
    table = pa.Table.from_batches((records_to_batch(records),))
    try:
        pq.write_table(
            table,
            path,
            compression=PARQUET_COMPRESSION,
            use_dictionary=True,
            row_group_size=PARQUET_ROW_GROUP_SIZE,
        )
    except (OSError, pa.ArrowException) as error:
        raise ArrowArchiveError("Parquet snapshot cannot be written") from error
    return path.stat().st_size


def read_parquet_snapshot(path: Path) -> tuple[KalshiWsOrderBookEventRecord, ...]:
    """Read Parquet and enforce the same Arrow/LIVE15 semantic contract."""

    try:
        table = pq.read_table(path)
        if table.schema != ARROW_WS_EVENT_SCHEMA:
            raise ArrowArchiveError("Parquet schema does not match the LIVE15 archive schema")
        records = tuple(
            record
            for batch in table.combine_chunks().to_batches()
            for record in batch_to_records(batch)
        )
        _validate_records(records)
        return records
    except ArrowArchiveError:
        raise
    except (OSError, pa.ArrowException) as error:
        raise ArrowArchiveError("Parquet snapshot cannot be decoded") from error


def canonical_semantic_digest(records: Sequence[KalshiWsOrderBookEventRecord]) -> tuple[str, int]:
    """Return the codec-independent exact LIVE15 semantic digest and logical bytes.

    The canonical payload is the existing Arrow semantic mapping, not a file-format
    encoding. It therefore stays stable across Parquet write/read and does not
    couple new archive verification to the retired JSONL/zlib codec.
    """

    batch = records_to_batch(records)
    payload = json.dumps(
        batch.to_pylist(), default=str, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), len(payload)
