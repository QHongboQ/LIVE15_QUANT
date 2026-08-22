"""Lossless chunked archive primitives for immutable Kalshi WebSocket events."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from live15_quant.kalshi_ws import KalshiBookSide, KalshiBookSyncStatus, KalshiWsEventKind
from live15_quant.models import DataRole, OrderBookLevel
from live15_quant.records import KalshiWsOrderBookEventRecord

_MAGIC = b"LIVE15-WS-ARCHIVE-V1\n"
_WIRE_VERSION = 1


class WsArchiveError(RuntimeError):
    """An archive fact, checksum, path, or manifest is invalid."""


@dataclass(frozen=True, slots=True)
class WsArchiveChunkMetadata:
    chunk_id: str
    relative_path: str
    checksum_sha256: str
    record_count: int
    first_row_id: int
    last_row_id: int
    connection_id: str
    subscription_id: int
    first_sequence: int
    last_sequence: int
    tickers: tuple[str, ...]
    first_received_timestamp: datetime
    last_received_timestamp: datetime
    uncompressed_bytes: int
    compressed_bytes: int


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_timestamp(value: object, field: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise WsArchiveError(f"archive {field} is malformed")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WsArchiveError(f"archive {field} is not timezone-aware")
    return parsed


def _decimal(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WsArchiveError(f"archive {field} is malformed")
    try:
        return Decimal(value)
    except ArithmeticError as error:
        raise WsArchiveError(f"archive {field} is malformed") from error


def _levels(value: object, field: str) -> tuple[OrderBookLevel, ...]:
    if not isinstance(value, list):
        raise WsArchiveError(f"archive {field} is malformed")
    levels: list[OrderBookLevel] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise WsArchiveError(f"archive {field} is malformed")
        price = _decimal(item[0], field)
        quantity = _decimal(item[1], field)
        if price is None or quantity is None:
            raise WsArchiveError(f"archive {field} is malformed")
        levels.append(OrderBookLevel(price, quantity))
    return tuple(levels)


def event_to_wire(record: KalshiWsOrderBookEventRecord) -> list[object]:
    """Encode every replay-relevant and provenance field without float conversion."""

    return [
        _WIRE_VERSION,
        record.row_id,
        record.schema_version,
        record.connection_id,
        record.subscription_id,
        record.sequence,
        record.event_kind.value,
        record.ticker,
        record.market_id,
        list(record.market_tickers),
        None if record.side is None else record.side.value,
        None if record.price is None else str(record.price),
        None if record.quantity_delta is None else str(record.quantity_delta),
        [[str(level.price), str(level.quantity)] for level in record.yes_bids],
        [[str(level.price), str(level.quantity)] for level in record.no_bids],
        _timestamp(record.source_timestamp),
        _timestamp(record.socket_received_timestamp),
        _timestamp(record.enqueue_timestamp),
        _timestamp(record.parse_timestamp),
        _timestamp(record.persisted_timestamp),
        (
            None
            if record.receive_enqueue_latency_ms is None
            else str(record.receive_enqueue_latency_ms)
        ),
        (
            None
            if record.receive_persist_latency_ms is None
            else str(record.receive_persist_latency_ms)
        ),
        record.sync_status_after.value,
        record.provenance,
        record.role.value,
    ]


def event_from_wire(value: object) -> KalshiWsOrderBookEventRecord:
    if not isinstance(value, list) or len(value) != 25 or value[0] != _WIRE_VERSION:
        raise WsArchiveError("archive event wire record is malformed")
    try:
        if not isinstance(value[9], list) or any(
            not isinstance(item, str) or not item for item in value[9]
        ):
            raise WsArchiveError("archive market_tickers is malformed")
        market_tickers = tuple(value[9])
        if not isinstance(value[3], str) or not value[3]:
            raise WsArchiveError("archive connection_id is malformed")
        if not isinstance(value[23], str) or not value[23]:
            raise WsArchiveError("archive provenance is malformed")
        side = None if value[10] is None else KalshiBookSide(str(value[10]))
        received = _parse_timestamp(value[16], "socket_received_timestamp")
        parsed = _parse_timestamp(value[18], "parse_timestamp")
        assert received is not None and parsed is not None
        return KalshiWsOrderBookEventRecord(
            row_id=int(value[1]),
            schema_version=int(value[2]),
            connection_id=str(value[3]),
            subscription_id=int(value[4]),
            sequence=int(value[5]),
            event_kind=KalshiWsEventKind(str(value[6])),
            ticker=None if value[7] is None else str(value[7]),
            market_id=None if value[8] is None else str(value[8]),
            market_tickers=market_tickers,
            side=side,
            price=_decimal(value[11], "price"),
            quantity_delta=_decimal(value[12], "quantity_delta"),
            yes_bids=_levels(value[13], "yes_bids"),
            no_bids=_levels(value[14], "no_bids"),
            source_timestamp=_parse_timestamp(value[15], "source_timestamp", optional=True),
            socket_received_timestamp=received,
            enqueue_timestamp=_parse_timestamp(value[17], "enqueue_timestamp", optional=True),
            parse_timestamp=parsed,
            persisted_timestamp=_parse_timestamp(value[19], "persisted_timestamp", optional=True),
            receive_enqueue_latency_ms=_decimal(value[20], "receive_enqueue_latency_ms"),
            receive_persist_latency_ms=_decimal(value[21], "receive_persist_latency_ms"),
            sync_status_after=KalshiBookSyncStatus(str(value[22])),
            provenance=str(value[23]),
            role=DataRole(str(value[24])),
        )
    except (AssertionError, TypeError, ValueError) as error:
        raise WsArchiveError("archive event wire record is malformed") from error


def _payload(records: Sequence[KalshiWsOrderBookEventRecord]) -> bytes:
    return b"".join(
        json.dumps(event_to_wire(record), separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        for record in records
    )


def encode_archive_chunk(
    records: Sequence[KalshiWsOrderBookEventRecord], *, compression_level: int = 6
) -> tuple[bytes, WsArchiveChunkMetadata]:
    if not records:
        raise WsArchiveError("archive chunk cannot be empty")
    if not 0 <= compression_level <= 9:
        raise ValueError("compression_level must be in 0..9")
    row_ids = [record.row_id for record in records]
    if row_ids != sorted(row_ids) or len(set(row_ids)) != len(row_ids):
        raise WsArchiveError("archive records must have unique ascending row ids")
    identities = {(record.connection_id, record.subscription_id) for record in records}
    sequences = [record.sequence for record in records]
    if (
        len(identities) != 1
        or sequences != sorted(sequences)
        or len(set(sequences)) != len(sequences)
    ):
        raise WsArchiveError("archive chunk must contain one ascending subscription stream")
    connection_id, subscription_id = next(iter(identities))
    tickers = tuple(
        sorted(
            {
                ticker
                for record in records
                for ticker in ((record.ticker,) if record.ticker else record.market_tickers)
            }
        )
    )
    received = [record.socket_received_timestamp for record in records]
    raw = _payload(records)
    checksum = hashlib.sha256(raw).hexdigest()
    chunk_id = f"{row_ids[0]}-{row_ids[-1]}-{checksum[:16]}"
    header = {
        "chunk_id": chunk_id,
        "codec": "zlib",
        "checksum_sha256": checksum,
        "record_count": len(records),
        "first_row_id": row_ids[0],
        "last_row_id": row_ids[-1],
        "connection_id": connection_id,
        "subscription_id": subscription_id,
        "first_sequence": sequences[0],
        "last_sequence": sequences[-1],
        "tickers": tickers,
        "first_received_timestamp": min(received).isoformat(),
        "last_received_timestamp": max(received).isoformat(),
        "uncompressed_bytes": len(raw),
        "wire_version": _WIRE_VERSION,
    }
    encoded_header = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    blob = _MAGIC + encoded_header + b"\n" + zlib.compress(raw, compression_level)
    metadata = WsArchiveChunkMetadata(
        chunk_id=chunk_id,
        relative_path="",
        checksum_sha256=checksum,
        record_count=len(records),
        first_row_id=row_ids[0],
        last_row_id=row_ids[-1],
        connection_id=connection_id,
        subscription_id=subscription_id,
        first_sequence=sequences[0],
        last_sequence=sequences[-1],
        tickers=tickers,
        first_received_timestamp=min(received),
        last_received_timestamp=max(received),
        uncompressed_bytes=len(raw),
        compressed_bytes=len(blob),
    )
    return blob, metadata


def decode_archive_chunk(
    blob: bytes,
) -> tuple[tuple[KalshiWsOrderBookEventRecord, ...], dict[str, Any]]:
    if not blob.startswith(_MAGIC):
        raise WsArchiveError("archive chunk magic is invalid")
    try:
        encoded_header, compressed = blob[len(_MAGIC) :].split(b"\n", 1)
        header = json.loads(encoded_header)
        raw = zlib.decompress(compressed)
    except (ValueError, json.JSONDecodeError, zlib.error) as error:
        raise WsArchiveError("archive chunk is truncated or malformed") from error
    if not isinstance(header, dict) or header.get("wire_version") != _WIRE_VERSION:
        raise WsArchiveError("archive chunk header is malformed")
    if header.get("codec") != "zlib" or not isinstance(header.get("checksum_sha256"), str):
        raise WsArchiveError("archive chunk header is malformed")
    checksum = hashlib.sha256(raw).hexdigest()
    if checksum != header.get("checksum_sha256"):
        raise WsArchiveError("archive chunk checksum mismatch")
    try:
        records = tuple(event_from_wire(json.loads(line)) for line in raw.splitlines())
    except json.JSONDecodeError as error:
        raise WsArchiveError("archive chunk payload is malformed") from error
    if len(records) != header.get("record_count"):
        raise WsArchiveError("archive chunk record count mismatch")
    if (
        not records
        or records[0].row_id != header.get("first_row_id")
        or records[-1].row_id != header.get("last_row_id")
    ):
        raise WsArchiveError("archive chunk row range mismatch")
    tickers = sorted(
        {
            ticker
            for record in records
            for ticker in ((record.ticker,) if record.ticker else record.market_tickers)
        }
    )
    received = [record.socket_received_timestamp for record in records]
    if (
        len({(record.connection_id, record.subscription_id) for record in records}) != 1
        or header.get("connection_id") != records[0].connection_id
        or header.get("subscription_id") != records[0].subscription_id
        or header.get("first_sequence") != records[0].sequence
        or header.get("last_sequence") != records[-1].sequence
        or header.get("tickers") != tickers
        or header.get("first_received_timestamp") != min(received).isoformat()
        or header.get("last_received_timestamp") != max(received).isoformat()
    ):
        raise WsArchiveError("archive chunk identity or range metadata mismatch")
    return records, header


def _safe_destination(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".zlib":
        raise WsArchiveError("archive path is outside the configured archive root")
    resolved_root = root.resolve()
    destination = (resolved_root / relative).resolve()
    if destination.parent != resolved_root and resolved_root not in destination.parents:
        raise WsArchiveError("archive path is outside the configured archive root")
    return destination


class WsArchiveManifest:
    """Small SQLite manifest; it never deletes HOT source rows."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS ws_archive_chunks(
                chunk_id TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL UNIQUE,
                checksum_sha256 TEXT NOT NULL,
                record_count INTEGER NOT NULL,
                first_row_id INTEGER NOT NULL,
                last_row_id INTEGER NOT NULL,
                connection_id TEXT NOT NULL,
                subscription_id INTEGER NOT NULL,
                first_sequence INTEGER NOT NULL,
                last_sequence INTEGER NOT NULL,
                tickers TEXT NOT NULL,
                first_received_timestamp TEXT NOT NULL,
                last_received_timestamp TEXT NOT NULL,
                uncompressed_bytes INTEGER NOT NULL,
                compressed_bytes INTEGER NOT NULL,
                committed_at TEXT NOT NULL
            ) STRICT"""
        )
        self._connection.commit()

    def commit(self, metadata: WsArchiveChunkMetadata, *, committed_at: datetime) -> bool:
        row = self._connection.execute(
            "SELECT checksum_sha256,relative_path FROM ws_archive_chunks WHERE chunk_id=?",
            (metadata.chunk_id,),
        ).fetchone()
        if row is not None:
            if (str(row[0]), str(row[1])) != (
                metadata.checksum_sha256,
                metadata.relative_path,
            ):
                raise WsArchiveError("conflicting archive manifest fact")
            return False
        with self._connection:
            self._connection.execute(
                """INSERT INTO ws_archive_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    metadata.chunk_id,
                    metadata.relative_path,
                    metadata.checksum_sha256,
                    metadata.record_count,
                    metadata.first_row_id,
                    metadata.last_row_id,
                    metadata.connection_id,
                    metadata.subscription_id,
                    metadata.first_sequence,
                    metadata.last_sequence,
                    json.dumps(metadata.tickers, separators=(",", ":")),
                    metadata.first_received_timestamp.isoformat(),
                    metadata.last_received_timestamp.isoformat(),
                    metadata.uncompressed_bytes,
                    metadata.compressed_bytes,
                    committed_at.isoformat(),
                ),
            )
        return True

    def count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM ws_archive_chunks").fetchone()[0])

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> WsArchiveManifest:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def write_verified_archive_chunk(
    root: Path,
    relative_path: str,
    records: Sequence[KalshiWsOrderBookEventRecord],
    manifest: WsArchiveManifest,
    *,
    committed_at: datetime,
) -> tuple[WsArchiveChunkMetadata, bool]:
    """Atomically publish, reopen, verify, then commit one manifest fact."""

    destination = _safe_destination(root, relative_path)
    blob, metadata = encode_archive_chunk(records)
    metadata = WsArchiveChunkMetadata(
        chunk_id=metadata.chunk_id,
        relative_path=relative_path.replace("\\", "/"),
        checksum_sha256=metadata.checksum_sha256,
        record_count=metadata.record_count,
        first_row_id=metadata.first_row_id,
        last_row_id=metadata.last_row_id,
        connection_id=metadata.connection_id,
        subscription_id=metadata.subscription_id,
        first_sequence=metadata.first_sequence,
        last_sequence=metadata.last_sequence,
        tickers=metadata.tickers,
        first_received_timestamp=metadata.first_received_timestamp,
        last_received_timestamp=metadata.last_received_timestamp,
        uncompressed_bytes=metadata.uncompressed_bytes,
        compressed_bytes=metadata.compressed_bytes,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(f"{destination.suffix}.{os.getpid()}.partial")
    try:
        if destination.exists():
            existing_records, header = decode_archive_chunk(destination.read_bytes())
            if (
                header.get("checksum_sha256") != metadata.checksum_sha256
                or len(existing_records) != metadata.record_count
            ):
                raise WsArchiveError("conflicting archive file already exists")
        else:
            with partial.open("xb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            partial.replace(destination)
        verified, header = decode_archive_chunk(destination.read_bytes())
        if (
            len(verified) != metadata.record_count
            or header["checksum_sha256"] != metadata.checksum_sha256
        ):
            raise WsArchiveError("published archive verification failed")
        committed = manifest.commit(metadata, committed_at=committed_at)
        return metadata, committed
    finally:
        partial.unlink(missing_ok=True)
