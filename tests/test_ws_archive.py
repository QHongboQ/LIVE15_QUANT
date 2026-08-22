from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from live15_quant.kalshi_ws import (
    KalshiBookSide,
    KalshiBookSyncStatus,
    KalshiWsEventKind,
    replay_orderbook_events,
)
from live15_quant.models import DataRole, OrderBookLevel
from live15_quant.records import KalshiWsOrderBookEventRecord
from live15_quant.sqlite_attribution import SqliteAttributionError, attribute_sqlite_snapshot
from live15_quant.storage_scaling import benchmark_storage_schemes, compare_sampling_policies
from live15_quant.ws_archive import (
    WsArchiveError,
    WsArchiveManifest,
    decode_archive_chunk,
    encode_archive_chunk,
    write_verified_archive_chunk,
)

NOW = datetime(2026, 8, 22, 1, tzinfo=UTC)
TICKER = "KXBTC15M-26AUG220115-15"


def record(sequence: int) -> KalshiWsOrderBookEventRecord:
    kind = (
        KalshiWsEventKind.SUBSCRIPTION_ACK
        if sequence == 1
        else (KalshiWsEventKind.SNAPSHOT if sequence == 2 else KalshiWsEventKind.DELTA)
    )
    return KalshiWsOrderBookEventRecord(
        row_id=sequence,
        schema_version=10,
        connection_id="connection-1",
        subscription_id=3,
        sequence=sequence,
        event_kind=kind,
        ticker=None if sequence == 1 else TICKER,
        market_id=None if sequence == 1 else "market-1",
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


def test_archive_round_trip_preserves_decimal_and_replay() -> None:
    records = tuple(record(sequence) for sequence in range(1, 5))
    blob, metadata = encode_archive_chunk(records)
    decoded, header = decode_archive_chunk(blob)
    assert decoded == records
    assert decoded[-1].quantity_delta.as_tuple() == Decimal("1.2500").as_tuple()
    assert header["checksum_sha256"] == metadata.checksum_sha256


@pytest.mark.parametrize("mutation", ["truncate", "checksum"])
def test_archive_rejects_truncation_and_checksum_mismatch(mutation: str) -> None:
    blob, _ = encode_archive_chunk((record(1), record(2)))
    broken = blob[:-3] if mutation == "truncate" else blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(WsArchiveError):
        decode_archive_chunk(broken)


def test_manifest_is_idempotent_conflict_loud_and_never_cleans_hot_rows(tmp_path: Path) -> None:
    records = (record(1), record(2))
    source = tmp_path / "hot.sqlite3"
    source.write_bytes(b"immutable-hot-fact")
    with WsArchiveManifest(tmp_path / "manifest.sqlite3") as manifest:
        _, committed = write_verified_archive_chunk(
            tmp_path / "archive", "2026/08/chunk.zlib", records, manifest, committed_at=NOW
        )
        assert committed
        _, committed = write_verified_archive_chunk(
            tmp_path / "archive", "2026/08/chunk.zlib", records, manifest, committed_at=NOW
        )
        assert not committed
        assert manifest.count() == 1
        with pytest.raises(WsArchiveError):
            write_verified_archive_chunk(
                tmp_path / "archive",
                "../escape.zlib",
                records,
                manifest,
                committed_at=NOW,
            )
    assert source.read_bytes() == b"immutable-hot-fact"


def test_crash_before_manifest_commit_leaves_verified_uncommitted_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = WsArchiveManifest(tmp_path / "manifest.sqlite3")

    def fail_commit(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("simulated crash before manifest commit")

    monkeypatch.setattr(manifest, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="simulated crash"):
        write_verified_archive_chunk(
            tmp_path / "archive", "chunk.zlib", (record(1), record(2)), manifest, committed_at=NOW
        )
    assert manifest.count() == 0
    decoded, _ = decode_archive_chunk((tmp_path / "archive" / "chunk.zlib").read_bytes())
    assert decoded == (record(1), record(2))
    manifest.close()


def test_sequence_gap_is_preserved_not_repaired() -> None:
    records = (record(1), record(2), replace(record(3), row_id=4, sequence=4))
    decoded, _ = decode_archive_chunk(encode_archive_chunk(records)[0])
    assert [item.sequence for item in decoded] == [1, 2, 4]


def test_replay_tracks_incremental_subscription_acknowledgements() -> None:
    second = "KXETH15M-26AUG220115-15"
    records = (
        record(1),
        record(2),
        replace(
            record(3),
            event_kind=KalshiWsEventKind.SUBSCRIPTION_ACK,
            ticker=None,
            market_id=None,
            market_tickers=(TICKER, second),
            side=None,
            price=None,
            quantity_delta=None,
        ),
        replace(
            record(2),
            row_id=4,
            sequence=4,
            ticker=second,
            market_id="market-2",
        ),
        replace(record(3), row_id=5, sequence=5),
    )
    books = replay_orderbook_events(records, (TICKER, second))
    assert set(books) == {TICKER, second}
    assert books[TICKER].yes_bids[0].quantity == Decimal("3.2500")


def test_all_four_benchmark_schemes_replay_identically(tmp_path: Path) -> None:
    records = tuple(record(sequence) for sequence in range(1, 20))
    results = benchmark_storage_schemes(records, tmp_path)
    assert {item.scheme for item in results} == {
        "sqlite_row_per_event",
        "compact_normalized_sqlite",
        "chunked_compressed_archive",
        "compressed_archive_with_manifest",
    }
    assert len({item.book_hash for item in results}) == 1
    sampling = compare_sampling_policies(records)
    assert {item.policy for item in sampling} == {
        "100ms",
        "250ms",
        "500ms",
        "1s",
        "top_of_book_change",
        "meaningful_depth_or_imbalance_change",
    }
    assert all(0 < item.state_retention_percent <= 100 for item in sampling)


def test_snapshot_attribution_is_read_only_and_counts_index_pages(tmp_path: Path) -> None:
    database = tmp_path / "snapshot.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        "CREATE TABLE facts(id INTEGER PRIMARY KEY,value TEXT NOT NULL);"
        "CREATE INDEX facts_value ON facts(value);"
    )
    connection.executemany("INSERT INTO facts(value) VALUES(?)", [("x" * 500,), ("y" * 500,)])
    connection.commit()
    connection.close()
    before = (database.stat().st_size, database.stat().st_mtime_ns, database.read_bytes())
    attribution = attribute_sqlite_snapshot(database)
    after = (database.stat().st_size, database.stat().st_mtime_ns, database.read_bytes())
    objects = {item.name: item for item in attribution.objects}
    assert objects["facts"].entries == 2
    assert objects["facts_value"].entries == 2
    assert before == after


def test_snapshot_attribution_rejects_nonempty_wal(tmp_path: Path) -> None:
    database = tmp_path / "snapshot.sqlite3"
    database.write_bytes(b"not consulted")
    database.with_name("snapshot.sqlite3-wal").write_bytes(b"active")
    with pytest.raises(SqliteAttributionError, match="WAL-free"):
        attribute_sqlite_snapshot(database)
