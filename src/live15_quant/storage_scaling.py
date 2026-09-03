"""Offline-only storage scaling and lossless replay benchmarks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa

from live15_quant.archive_arrow import (
    ARROW_WS_EVENT_SCHEMA,
    batch_to_records,
    read_parquet_snapshot,
    records_to_batch,
    write_parquet_snapshot,
)
from live15_quant.kalshi_ws import replay_orderbook_events
from live15_quant.records import KalshiWsOrderBookEventRecord
from live15_quant.storage import RecorderStore


class StorageScalingError(RuntimeError):
    """The supplied database is not a fixed, read-only analysis snapshot."""


def event_to_wire(record: KalshiWsOrderBookEventRecord) -> dict[str, object]:
    """Offline SQLite benchmark mapping; not an archive serialization codec."""

    return records_to_batch((record,)).to_pylist()[0]


def event_from_wire(value: object) -> KalshiWsOrderBookEventRecord:
    """Inverse of the offline benchmark mapping through the canonical Arrow adapter."""

    if not isinstance(value, dict):
        raise StorageScalingError("benchmark wire record is malformed")
    return batch_to_records(pa.RecordBatch.from_pylist([value], schema=ARROW_WS_EVENT_SCHEMA))[0]


@dataclass(frozen=True, slots=True)
class StorageBenchmarkResult:
    scheme: str
    records: int
    bytes_on_disk: int
    bytes_per_record: float
    write_records_per_second: float
    replay_records_per_second: float
    book_hash: str


@dataclass(frozen=True, slots=True)
class SamplingRetention:
    policy: str
    selected_states: int
    state_retention_percent: float
    top_of_book_change_retention_percent: float
    meaningful_change_retention_percent: float


def _fixed_connection(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    wal = resolved.with_name(f"{resolved.name}-wal")
    if not resolved.is_file() or (wal.exists() and wal.stat().st_size):
        raise StorageScalingError("analysis requires an existing WAL-free snapshot")
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_replayable_ws_sample(
    snapshot: Path, *, maximum_records: int = 100_000
) -> tuple[KalshiWsOrderBookEventRecord, ...]:
    """Load one indexed replay stream from a fixed snapshot without touching active storage."""

    if maximum_records < 1:
        raise ValueError("maximum_records must be positive")
    connection = _fixed_connection(snapshot)
    try:
        stream = connection.execute(
            """SELECT connection_id,subscription_id
            FROM kalshi_ws_orderbook_events ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        if stream is None:
            raise StorageScalingError("snapshot contains no replayable WebSocket stream")
        first = connection.execute(
            """SELECT id FROM kalshi_ws_orderbook_events
            WHERE connection_id=? AND subscription_id=? AND event_kind='subscription_ack'
            ORDER BY id LIMIT 1""",
            (stream["connection_id"], stream["subscription_id"]),
        ).fetchone()
        if first is None:
            raise StorageScalingError("snapshot stream has no replay baseline acknowledgement")
        rows = connection.execute(
            """SELECT * FROM kalshi_ws_orderbook_events
            WHERE connection_id=? AND subscription_id=? AND id>=?
            ORDER BY id LIMIT ?""",
            (
                stream["connection_id"],
                stream["subscription_id"],
                first["id"],
                maximum_records,
            ),
        )
        return tuple(RecorderStore._kalshi_ws_event_record(row) for row in rows)
    finally:
        connection.close()


def _book_hash(records: tuple[KalshiWsOrderBookEventRecord, ...]) -> str:
    tickers = sorted(
        {
            ticker
            for record in records
            for ticker in ((record.ticker,) if record.ticker else record.market_tickers)
        }
    )
    books = replay_orderbook_events(records, tickers)
    facts = [
        [
            ticker,
            book.sequence,
            book.market_id,
            [[str(level.price), str(level.quantity)] for level in book.yes_bids],
            [[str(level.price), str(level.quantity)] for level in book.no_bids],
        ]
        for ticker, book in sorted(books.items())
    ]
    return hashlib.sha256(json.dumps(facts, separators=(",", ":")).encode()).hexdigest()


def _timed_replay(records: tuple[KalshiWsOrderBookEventRecord, ...]) -> tuple[float, str]:
    started = time.perf_counter()
    result = _book_hash(records)
    elapsed = max(time.perf_counter() - started, 1e-9)
    return len(records) / elapsed, result


def _directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _epoch_microseconds(value: datetime) -> int:
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000) + delta.microseconds


def _read_sqlite_wire(
    path: Path, *, normalized: bool = False
) -> tuple[KalshiWsOrderBookEventRecord, ...]:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if not normalized:
            return tuple(
                RecorderStore._kalshi_ws_event_record(row)
                for row in connection.execute("SELECT * FROM events ORDER BY id")
            )
        dictionary = (
            {
                int(row[0]): str(row[1])
                for row in connection.execute("SELECT id,value FROM dictionary")
            }
            if normalized
            else {}
        )
        records: list[KalshiWsOrderBookEventRecord] = []
        for row in connection.execute("SELECT wire_json FROM events ORDER BY id"):
            wire = json.loads(row[0])
            if normalized:
                for field in (3, 7, 8, 23):
                    wire[field] = None if wire[field] is None else dictionary[int(wire[field])]
                wire[9] = [dictionary[int(identifier)] for identifier in wire[9]]
            records.append(event_from_wire(wire))
        return tuple(records)
    finally:
        connection.close()


def _sqlite_baseline(path: Path, records: tuple[KalshiWsOrderBookEventRecord, ...]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """CREATE TABLE events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version INTEGER NOT NULL,
                connection_id TEXT NOT NULL,
                subscription_id INTEGER NOT NULL,
                sequence INTEGER NOT NULL,
                event_kind TEXT NOT NULL,
                ticker TEXT,
                market_id TEXT,
                market_tickers TEXT NOT NULL,
                side TEXT,
                price TEXT,
                quantity_delta TEXT,
                yes_bids TEXT NOT NULL,
                no_bids TEXT NOT NULL,
                source_timestamp TEXT,
                socket_received_timestamp TEXT NOT NULL,
                enqueue_timestamp TEXT,
                parse_timestamp TEXT NOT NULL,
                persisted_timestamp TEXT,
                receive_enqueue_latency_ms TEXT,
                receive_persist_latency_ms TEXT,
                sync_status_after TEXT NOT NULL,
                provenance TEXT NOT NULL,
                data_role TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                UNIQUE(connection_id,subscription_id,sequence)
            ) STRICT;
            CREATE INDEX event_replay ON events(connection_id,subscription_id,id);
            CREATE INDEX event_time ON events(ticker,socket_received_timestamp,id);"""
        )
        with connection:
            connection.executemany(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    (
                        record.row_id,
                        record.schema_version,
                        record.connection_id,
                        record.subscription_id,
                        record.sequence,
                        record.event_kind.value,
                        record.ticker,
                        record.market_id,
                        json.dumps(record.market_tickers, separators=(",", ":")),
                        None if record.side is None else record.side.value,
                        None if record.price is None else str(record.price),
                        None if record.quantity_delta is None else str(record.quantity_delta),
                        json.dumps(
                            [[str(item.price), str(item.quantity)] for item in record.yes_bids],
                            separators=(",", ":"),
                        ),
                        json.dumps(
                            [[str(item.price), str(item.quantity)] for item in record.no_bids],
                            separators=(",", ":"),
                        ),
                        None
                        if record.source_timestamp is None
                        else record.source_timestamp.isoformat(),
                        record.socket_received_timestamp.isoformat(),
                        None
                        if record.enqueue_timestamp is None
                        else record.enqueue_timestamp.isoformat(),
                        record.parse_timestamp.isoformat(),
                        None
                        if record.persisted_timestamp is None
                        else record.persisted_timestamp.isoformat(),
                        None
                        if record.receive_enqueue_latency_ms is None
                        else str(record.receive_enqueue_latency_ms),
                        None
                        if record.receive_persist_latency_ms is None
                        else str(record.receive_persist_latency_ms),
                        record.sync_status_after.value,
                        record.provenance,
                        record.role.value,
                        hashlib.sha256(
                            json.dumps(event_to_wire(record), separators=(",", ":")).encode()
                        ).hexdigest(),
                    )
                    for record in records
                ),
            )
    finally:
        connection.close()


def _compact_sqlite(path: Path, records: tuple[KalshiWsOrderBookEventRecord, ...]) -> None:
    values = sorted(
        {
            value
            for record in records
            for value in (
                record.connection_id,
                record.ticker,
                record.market_id,
                record.provenance,
                *record.market_tickers,
            )
            if value is not None
        }
    )
    identifiers = {value: index for index, value in enumerate(values, 1)}
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """CREATE TABLE dictionary(id INTEGER PRIMARY KEY,value TEXT NOT NULL UNIQUE) STRICT;
            CREATE TABLE events(
                id INTEGER PRIMARY KEY,
                connection_ref INTEGER NOT NULL,
                subscription_id INTEGER NOT NULL,
                sequence INTEGER NOT NULL,
                ticker_ref INTEGER,
                market_ref INTEGER,
                provenance_ref INTEGER NOT NULL,
                received_us INTEGER NOT NULL,
                wire_json BLOB NOT NULL,
                FOREIGN KEY(connection_ref) REFERENCES dictionary(id)
            ) STRICT;
            CREATE UNIQUE INDEX event_sequence ON events(connection_ref,subscription_id,sequence);
            CREATE INDEX event_time ON events(ticker_ref,received_us,id);"""
        )
        with connection:
            connection.executemany(
                "INSERT INTO dictionary VALUES(?,?)",
                ((identifier, value) for value, identifier in identifiers.items()),
            )

            def rows() -> Iterator[tuple[object, ...]]:
                for record in records:
                    wire = event_to_wire(record)
                    for field in (3, 7, 8, 23):
                        wire[field] = None if wire[field] is None else identifiers[str(wire[field])]
                    wire[9] = [identifiers[str(value)] for value in wire[9]]
                    yield (
                        record.row_id,
                        identifiers[record.connection_id],
                        record.subscription_id,
                        record.sequence,
                        identifiers.get(record.ticker),
                        identifiers.get(record.market_id),
                        identifiers[record.provenance],
                        _epoch_microseconds(record.socket_received_timestamp),
                        json.dumps(wire, separators=(",", ":")).encode(),
                    )

            connection.executemany(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                rows(),
            )
    finally:
        connection.close()


def benchmark_storage_schemes(
    records: tuple[KalshiWsOrderBookEventRecord, ...], root: Path
) -> tuple[StorageBenchmarkResult, ...]:
    """Compare four lossless representations in an explicitly temporary directory."""

    if not records:
        raise ValueError("records are required")
    root.mkdir(parents=True, exist_ok=True)
    _, expected_hash = _timed_replay(records)
    results: list[StorageBenchmarkResult] = []

    def result(
        scheme: str,
        path: Path,
        write_elapsed: float,
        replay: float,
        book_hash: str,
    ) -> None:
        size = _directory_bytes(path) if path.is_dir() else path.stat().st_size
        results.append(
            StorageBenchmarkResult(
                scheme=scheme,
                records=len(records),
                bytes_on_disk=size,
                bytes_per_record=size / len(records),
                write_records_per_second=len(records) / max(write_elapsed, 1e-9),
                replay_records_per_second=replay,
                book_hash=book_hash,
            )
        )

    baseline = root / "baseline.sqlite3"
    started = time.perf_counter()
    _sqlite_baseline(baseline, records)
    write_elapsed = time.perf_counter() - started
    baseline_records = _read_sqlite_wire(baseline)
    baseline_rate, baseline_hash = _timed_replay(baseline_records)
    result("sqlite_row_per_event", baseline, write_elapsed, baseline_rate, baseline_hash)

    compact = root / "compact.sqlite3"
    started = time.perf_counter()
    _compact_sqlite(compact, records)
    write_elapsed = time.perf_counter() - started
    compact_records = _read_sqlite_wire(compact, normalized=True)
    compact_rate, compact_hash = _timed_replay(compact_records)
    result("compact_normalized_sqlite", compact, write_elapsed, compact_rate, compact_hash)

    archive = root / "parquet"
    archive.mkdir()
    started = time.perf_counter()
    archive_file = archive / "chunk.parquet"
    write_parquet_snapshot(archive_file, records)
    write_elapsed = time.perf_counter() - started
    decoded = read_parquet_snapshot(archive_file)
    decoded_rate, decoded_hash = _timed_replay(decoded)
    result("parquet_zstd_archive", archive, write_elapsed, decoded_rate, decoded_hash)

    if any(item.book_hash != expected_hash for item in results):
        raise StorageScalingError("lossless scheme changed reconstructed orderbook state")
    return tuple(results)


def benchmark_snapshot(
    snapshot: Path, *, maximum_records: int = 100_000
) -> tuple[StorageBenchmarkResult, ...]:
    """Convenience benchmark that owns and removes every generated artifact."""

    records = load_replayable_ws_sample(snapshot, maximum_records=maximum_records)
    with TemporaryDirectory(prefix="live15-storage-benchmark-") as directory:
        return benchmark_storage_schemes(records, Path(directory))


def compare_sampling_policies(
    records: tuple[KalshiWsOrderBookEventRecord, ...],
) -> tuple[SamplingRetention, ...]:
    """Compare compact model-state policies while retaining raw events separately."""

    books: dict[str, dict[str, dict[Decimal, Decimal]]] = {}
    states: dict[str, list[tuple[datetime, tuple[Decimal | None, ...]]]] = {}
    for record in records:
        if record.ticker is None:
            continue
        if record.event_kind.value == "orderbook_snapshot":
            books[record.ticker] = {
                "yes": {item.price: item.quantity for item in record.yes_bids},
                "no": {item.price: item.quantity for item in record.no_bids},
            }
        elif record.event_kind.value == "orderbook_delta":
            if record.ticker not in books or record.side is None:
                continue
            assert record.price is not None and record.quantity_delta is not None
            side = books[record.ticker][record.side.value]
            quantity = side.get(record.price, Decimal(0)) + record.quantity_delta
            if quantity < 0:
                continue
            if quantity == 0:
                side.pop(record.price, None)
            else:
                side[record.price] = quantity
        else:
            continue
        book = books[record.ticker]
        yes_best = max(book["yes"], default=None)
        no_best = max(book["no"], default=None)
        yes_depth = sum(book["yes"].values(), Decimal(0))
        no_depth = sum(book["no"].values(), Decimal(0))
        total = yes_depth + no_depth
        imbalance = None if total == 0 else yes_depth / total
        states.setdefault(record.ticker, []).append(
            (
                record.socket_received_timestamp,
                (yes_best, no_best, yes_depth, no_depth, imbalance),
            )
        )

    flattened = [state for ticker_states in states.values() for state in ticker_states]
    raw_count = len(flattened)
    if raw_count == 0:
        return ()

    def top(value: tuple[Decimal | None, ...]) -> tuple[Decimal | None, Decimal | None]:
        return value[0], value[1]

    def meaningful(
        previous: tuple[Decimal | None, ...], current: tuple[Decimal | None, ...]
    ) -> bool:
        if top(previous) != top(current):
            return True
        previous_total = Decimal(previous[2] or 0) + Decimal(previous[3] or 0)
        current_total = Decimal(current[2] or 0) + Decimal(current[3] or 0)
        depth_change = (
            Decimal(0)
            if previous_total == 0
            else abs(current_total - previous_total) / previous_total
        )
        imbalance_change = abs(Decimal(current[4] or 0) - Decimal(previous[4] or 0))
        return depth_change >= Decimal("0.10") or imbalance_change >= Decimal("0.05")

    raw_top_changes = sum(
        top(current[1]) != top(previous[1])
        for ticker_states in states.values()
        for previous, current in pairwise(ticker_states)
    )
    raw_meaningful = sum(
        meaningful(previous[1], current[1])
        for ticker_states in states.values()
        for previous, current in pairwise(ticker_states)
    )
    policies: dict[str, timedelta | None] = {
        "100ms": timedelta(milliseconds=100),
        "250ms": timedelta(milliseconds=250),
        "500ms": timedelta(milliseconds=500),
        "1s": timedelta(seconds=1),
        "top_of_book_change": None,
        "meaningful_depth_or_imbalance_change": None,
    }
    results: list[SamplingRetention] = []
    for name, interval in policies.items():
        selected: list[tuple[datetime, tuple[Decimal | None, ...]]] = []
        selected_top = 0
        selected_meaningful = 0
        for ticker_states in states.values():
            prior_selected: tuple[datetime, tuple[Decimal | None, ...]] | None = None
            for state in ticker_states:
                choose = prior_selected is None
                if prior_selected is not None and interval is not None:
                    choose = state[0] - prior_selected[0] >= interval
                elif prior_selected is not None and name == "top_of_book_change":
                    choose = top(state[1]) != top(prior_selected[1])
                elif prior_selected is not None:
                    choose = meaningful(prior_selected[1], state[1])
                if choose:
                    if prior_selected is not None:
                        selected_top += top(state[1]) != top(prior_selected[1])
                        selected_meaningful += meaningful(prior_selected[1], state[1])
                    selected.append(state)
                    prior_selected = state
        results.append(
            SamplingRetention(
                policy=name,
                selected_states=len(selected),
                state_retention_percent=100 * len(selected) / raw_count,
                top_of_book_change_retention_percent=(
                    100.0
                    if raw_top_changes == 0
                    else min(100.0, 100 * selected_top / raw_top_changes)
                ),
                meaningful_change_retention_percent=(
                    100.0
                    if raw_meaningful == 0
                    else min(100.0, 100 * selected_meaningful / raw_meaningful)
                ),
            )
        )
    return tuple(results)
