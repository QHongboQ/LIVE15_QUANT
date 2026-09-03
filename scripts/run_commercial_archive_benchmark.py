"""Run the fixed-snapshot JSONL+zlib versus Arrow IPC+ZSTD benchmark.

This offline driver intentionally uses the existing LIVE15 encoders, decoders, and
replay implementation unchanged.  It refuses any stream other than the fixed PR156
complete replay stream and writes a machine-readable measurement receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from live15_quant.archive_arrow import ArrowArchiveError, read_ipc_snapshot, write_ipc_snapshot
from live15_quant.kalshi_ws import replay_orderbook_events
from live15_quant.records import KalshiWsOrderBookEventRecord
from live15_quant.storage import RecorderStore
from live15_quant.ws_archive import decode_archive_chunk, encode_archive_chunk

EXPECTED_FIRST_ROW_ID = 232_652
EXPECTED_LAST_ROW_ID = 1_251_327
EXPECTED_RECORDS = 1_018_676

def _fixed_connection(snapshot: Path) -> sqlite3.Connection:
    resolved = snapshot.resolve()
    if not resolved.is_file():
        raise ValueError("fixed snapshot does not exist")
    wal = resolved.with_name(f"{resolved.name}-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("fixed snapshot must be WAL-free")
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_complete_stream(snapshot: Path) -> tuple[KalshiWsOrderBookEventRecord, ...]:
    """Load exactly the known complete PR156 replay stream from a read-only snapshot."""
    connection = _fixed_connection(snapshot)
    try:
        rows = tuple(
            RecorderStore._kalshi_ws_event_record(row)
            for row in connection.execute(
                """SELECT * FROM kalshi_ws_orderbook_events
                WHERE id BETWEEN ? AND ?
                ORDER BY id""",
                (EXPECTED_FIRST_ROW_ID, EXPECTED_LAST_ROW_ID),
            )
        )
    finally:
        connection.close()
    if len(rows) != EXPECTED_RECORDS:
        raise ValueError("fixed snapshot no longer contains the complete expected replay stream")
    if rows[0].row_id != EXPECTED_FIRST_ROW_ID or rows[-1].row_id != EXPECTED_LAST_ROW_ID:
        raise ValueError(
            "fixed snapshot replay stream row bounds differ from the benchmark contract"
        )
    stream = {(row.connection_id, row.subscription_id) for row in rows}
    if len(stream) != 1:
        raise ValueError("benchmark contract requires one complete subscription stream")
    if [row.sequence for row in rows] != list(range(1, EXPECTED_RECORDS + 1)):
        raise ValueError("benchmark contract requires contiguous sequence ordering")
    return rows


def _timed[T](action: Callable[[], T]) -> tuple[T, float]:
    started = time.perf_counter()
    result = action()
    return result, max(time.perf_counter() - started, 1e-9)


def _decimal_fidelity(
    original: Sequence[KalshiWsOrderBookEventRecord],
    decoded: Sequence[KalshiWsOrderBookEventRecord],
) -> bool:
    fields = (
        "price",
        "quantity_delta",
        "receive_enqueue_latency_ms",
        "receive_persist_latency_ms",
    )
    for before, after in zip(original, decoded, strict=True):
        for field in fields:
            left, right = getattr(before, field), getattr(after, field)
            if (left is None) != (right is None):
                return False
            if left is not None and right is not None and left.as_tuple() != right.as_tuple():
                return False
        for side in ("yes_bids", "no_bids"):
            for left, right in zip(getattr(before, side), getattr(after, side), strict=True):
                if left.price.as_tuple() != right.price.as_tuple():
                    return False
                if left.quantity.as_tuple() != right.quantity.as_tuple():
                    return False
    return True


def _timestamp_fidelity(
    original: Sequence[KalshiWsOrderBookEventRecord],
    decoded: Sequence[KalshiWsOrderBookEventRecord],
) -> bool:
    fields = (
        "source_timestamp",
        "socket_received_timestamp",
        "enqueue_timestamp",
        "parse_timestamp",
        "persisted_timestamp",
    )
    for before, after in zip(original, decoded, strict=True):
        for field in fields:
            left, right = getattr(before, field), getattr(after, field)
            if left != right or (right is not None and right.tzinfo != UTC):
                return False
            if right is not None and right.microsecond != left.microsecond:
                return False
    return True


def _replay_hash(records: Sequence[KalshiWsOrderBookEventRecord]) -> str:
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


def _measure_jsonl(
    records: tuple[KalshiWsOrderBookEventRecord, ...], directory: Path
) -> dict[str, object]:
    blob, encode_elapsed = _timed(lambda: encode_archive_chunk(records)[0])
    path = directory / "archive.zlib"
    path.write_bytes(blob)
    decoded, decode_elapsed = _timed(lambda: decode_archive_chunk(path.read_bytes())[0])
    replay_hash, replay_elapsed = _timed(lambda: _replay_hash(decoded))
    return {
        "format": "JSONL+zlib",
        "total_bytes": path.stat().st_size,
        "bytes_per_event": path.stat().st_size / len(records),
        "encode_events_per_second": len(records) / encode_elapsed,
        "decode_events_per_second": len(records) / decode_elapsed,
        "replay_events_per_second": len(records) / replay_elapsed,
        "decoded": decoded,
        "replay_hash": replay_hash,
    }


def _measure_arrow(
    records: tuple[KalshiWsOrderBookEventRecord, ...], directory: Path
) -> dict[str, object]:
    path = directory / "archive.arrow"
    _, encode_elapsed = _timed(lambda: write_ipc_snapshot(path, records))
    decoded, decode_elapsed = _timed(lambda: read_ipc_snapshot(path))
    replay_hash, replay_elapsed = _timed(lambda: _replay_hash(decoded))
    truncated = directory / "archive-truncated.arrow"
    truncated.write_bytes(path.read_bytes()[:-8])
    try:
        read_ipc_snapshot(truncated)
    except ArrowArchiveError:
        truncated_fails_closed = True
    else:
        truncated_fails_closed = False
    return {
        "format": "Arrow IPC+ZSTD",
        "total_bytes": path.stat().st_size,
        "bytes_per_event": path.stat().st_size / len(records),
        "encode_events_per_second": len(records) / encode_elapsed,
        "decode_events_per_second": len(records) / decode_elapsed,
        "replay_events_per_second": len(records) / replay_elapsed,
        "decoded": decoded,
        "replay_hash": replay_hash,
        "truncated_arrow_fails_closed": truncated_fails_closed,
    }


def _verification(
    original: tuple[KalshiWsOrderBookEventRecord, ...], result: dict[str, object], expected: str
) -> dict[str, bool]:
    decoded = result.pop("decoded")
    assert isinstance(decoded, tuple)
    return {
        "exact_round_trip": decoded == original,
        "decimal_fidelity": _decimal_fidelity(original, decoded),
        "utc_microsecond_timestamp_fidelity": _timestamp_fidelity(original, decoded),
        "ordering": [row.row_id for row in decoded] == [row.row_id for row in original],
        "deterministic_replay_equivalence": result["replay_hash"] == expected,
        **(
            {"truncated_arrow_fails_closed": bool(result["truncated_arrow_fails_closed"])}
            if "truncated_arrow_fails_closed" in result
            else {}
        ),
    }


def run(snapshot: Path, sample_size: int, output: Path) -> dict[str, object]:
    records = load_complete_stream(snapshot)
    samples = (("sample_100000", records[:sample_size]), ("full_1018676", records))
    receipt: dict[str, object] = {
        "benchmark_id": "COMPLETE-COMMERCIAL-ARCHIVE-BENCHMARK-001",
        "generated_at": datetime.now(UTC).isoformat(),
        "snapshot": {"name": snapshot.name, "sha256": _file_sha256(snapshot)},
        "stream": {
            "records": len(records),
            "first_row_id": records[0].row_id,
            "last_row_id": records[-1].row_id,
            "connection_id": records[0].connection_id,
            "subscription_id": records[0].subscription_id,
        },
        "implementations": {
            "jsonl_zlib": "live15_quant.ws_archive.encode_archive_chunk/decode_archive_chunk",
            "arrow_ipc_zstd": "live15_quant.archive_arrow.write_ipc_snapshot/read_ipc_snapshot",
            "replay": "live15_quant.kalshi_ws.replay_orderbook_events",
        },
        "runs": {},
    }
    for name, selected in samples:
        with tempfile.TemporaryDirectory(prefix="live15-commercial-archive-benchmark-") as temp:
            directory = Path(temp)
            expected = _replay_hash(selected)
            jsonl = _measure_jsonl(selected, directory)
            arrow = _measure_arrow(selected, directory)
            jsonl_verification = _verification(selected, jsonl, expected)
            arrow_verification = _verification(selected, arrow, expected)
            receipt["runs"][name] = {
                "records": len(selected),
                "first_row_id": selected[0].row_id,
                "last_row_id": selected[-1].row_id,
                "formats": [
                    {**jsonl, "verification": jsonl_verification},
                    {**arrow, "verification": arrow_verification},
                ],
            }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def _file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=100_000)
    arguments = parser.parse_args()
    if arguments.sample_size != 100_000:
        raise SystemExit("benchmark contract requires exactly a 100000-event sample")
    receipt = run(arguments.snapshot, arguments.sample_size, arguments.output)
    print(json.dumps(receipt, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
