from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from live15_quant.kalshi_ws import (
    KalshiBookSide,
    KalshiBookSyncStatus,
    KalshiCommandAcknowledged,
    KalshiOrderBookDelta,
    KalshiOrderBookSnapshot,
)
from live15_quant.models import OrderBookLevel
from live15_quant.storage import RecorderStore
from live15_quant.ws_archive import decode_archive_chunk, encode_archive_chunk
from live15_quant.ws_retention import (
    ArchiveState,
    CompactionBenefitGate,
    DiskQuota,
    DiskThresholdState,
    WsArchiveService,
    WsMaintenanceBusy,
    WsPurgeService,
    WsRetentionError,
    WsRetentionManifest,
    _read_records,
    _ReplayState,
    _usable_replay_baseline,
    assess_purge_benefit,
    compact_database_offline,
    evaluate_database_compaction,
    swap_compacted_database,
)

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
TICKER = "KXBTC15M-26AUG221215-15"


def _populate(path: Path, count: int = 31) -> None:
    store = RecorderStore(path)
    received = NOW - timedelta(hours=8)
    acknowledgement = KalshiCommandAcknowledged(
        connection_id="connection-1",
        request_id=1,
        subscription_id=2,
        sequence=1,
        market_tickers=(TICKER,),
        socket_received_timestamp=received,
        parse_timestamp=received + timedelta(microseconds=1),
    )
    store.append_kalshi_ws_orderbook_event(
        acknowledgement, sync_status_after=KalshiBookSyncStatus.UNSYNCHRONIZED
    )
    snapshot = KalshiOrderBookSnapshot(
        connection_id="connection-1",
        subscription_id=2,
        sequence=2,
        ticker=TICKER,
        market_id="market-1",
        yes_bids=(OrderBookLevel(Decimal("0.5000"), Decimal("10.0000")),),
        no_bids=(OrderBookLevel(Decimal("0.4900"), Decimal("11.0000")),),
        source_timestamp=received,
        socket_received_timestamp=received + timedelta(microseconds=2),
        parse_timestamp=received + timedelta(microseconds=3),
    )
    store.append_kalshi_ws_orderbook_event(
        snapshot, sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED
    )
    for sequence in range(3, count + 1):
        observed = received + timedelta(microseconds=sequence)
        delta = KalshiOrderBookDelta(
            connection_id="connection-1",
            subscription_id=2,
            sequence=sequence,
            ticker=TICKER,
            market_id="market-1",
            side=KalshiBookSide.YES,
            price=Decimal("0.5000"),
            quantity_delta=Decimal("0.0001"),
            source_timestamp=observed,
            socket_received_timestamp=observed,
            parse_timestamp=observed + timedelta(microseconds=1),
        )
        store.append_kalshi_ws_orderbook_event(
            delta, sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED
        )
    store.close()


def _service(
    tmp_path: Path, *, chunk: int = 10, count: int = 31
) -> tuple[WsArchiveService, WsRetentionManifest]:
    database = tmp_path / "raw.sqlite3"
    _populate(database, count=count)
    manifest = WsRetentionManifest(tmp_path / "archive-manifest.sqlite3")
    return (
        WsArchiveService(
            database,
            tmp_path / "archive",
            manifest,
            hot_retention=timedelta(hours=6),
            chunk_records=chunk,
        ),
        manifest,
    )


def test_sequential_chunks_are_exact_verified_and_restart_safe(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    first = service.run_once(now=NOW)
    second = service.run_once(now=NOW)
    assert first.chunk is not None and second.chunk is not None
    assert first.chunk.state is ArchiveState.PURGE_ELIGIBLE
    assert second.chunk.first_event_id == first.chunk.last_event_id + 1
    assert first.chunk.event_type_counts == {
        "orderbook_delta": 8,
        "orderbook_snapshot": 1,
        "subscription_ack": 1,
    }
    assert first.chunk.logical_checksum
    assert first.chunk.file_checksum
    assert first.chunk.source_replay_hash == first.chunk.archive_replay_hash
    assert first.chunk.tickers == (TICKER,)
    restarted = WsArchiveService(
        service.source_database,
        service.archive_root,
        WsRetentionManifest(manifest.path),
        hot_retention=timedelta(hours=6),
        chunk_records=10,
    )
    third = restarted.run_once(now=NOW)
    assert third.chunk is not None and third.chunk.first_event_id == 21


def test_eligibility_is_non_blocking_and_resumable(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    waiting = service.eligibility(now=NOW - timedelta(hours=3))
    assert waiting.status == "WAITING_FOR_RETENTION_ELIGIBILITY"
    assert waiting.eligible_rows_bounded == 0
    assert waiting.next_eligible_at == NOW - timedelta(hours=2)
    assert manifest.chunks() == ()

    eligible = service.eligibility(now=NOW)
    assert eligible.status == "ELIGIBLE"
    assert eligible.eligible_first_event_id == 1
    assert eligible.eligible_last_event_id == 10
    assert eligible.eligible_rows_bounded == 10
    assert eligible.eligible_rows_capped is True

    archived = service.run_once(now=NOW)
    assert archived.chunk is not None
    resumed = service.eligibility(now=NOW)
    assert resumed.eligible_first_event_id == 11


def test_waiting_eligibility_never_loads_raw_event_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _manifest = _service(tmp_path)

    def forbidden_read(*_args: object, **_kwargs: object) -> tuple[()]:
        raise AssertionError("waiting eligibility loaded raw rows")

    monkeypatch.setattr("live15_quant.ws_retention._read_records", forbidden_read)
    result = service.eligibility(now=NOW - timedelta(hours=3))
    assert result.status == "WAITING_FOR_RETENTION_ELIGIBILITY"


def test_overlap_conflict_fails_loudly(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    archived = service.run_once(now=NOW).chunk
    assert archived is not None
    records = service._range_records(archived)
    with pytest.raises(WsRetentionError, match="overlaps"):
        manifest.reserve(records[1:], relative_path="conflict.zlib", created_at=NOW)


@pytest.mark.parametrize(
    "crash_state",
    [
        ArchiveState.WRITTEN,
        ArchiveState.CHECKSUM_VERIFIED,
        ArchiveState.REPLAY_VERIFIED,
        ArchiveState.COMMITTED,
        ArchiveState.PURGE_ELIGIBLE,
    ],
)
def test_partial_file_and_manifest_crash_boundaries_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_state: ArchiveState
) -> None:
    service, manifest = _service(tmp_path)
    partial = tmp_path / "archive" / "2026-08-22" / "04" / "chunk-1-10.parquet.partial"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"truncated")
    original = manifest.advance
    crashed = False

    def crash_after_publish(chunk_id, state, **kwargs):
        nonlocal crashed
        if state is crash_state and not crashed:
            crashed = True
            raise KeyboardInterrupt("simulated process crash")
        return original(chunk_id, state, **kwargs)

    monkeypatch.setattr(manifest, "advance", crash_after_publish)
    with pytest.raises(KeyboardInterrupt):
        service.run_once(now=NOW)
    intermediate = manifest.chunks()[0]
    assert intermediate.state is not ArchiveState.FAILED
    assert int(manifest.metrics()["retention_verified"] or 0) == int(
        intermediate.state
        in {ArchiveState.COMMITTED, ArchiveState.PURGE_ELIGIBLE, ArchiveState.PURGED}
    )
    monkeypatch.setattr(manifest, "advance", original)
    recovered = service.run_once(now=NOW).chunk
    assert recovered is not None and recovered.state is ArchiveState.PURGE_ELIGIBLE
    assert not tuple((tmp_path / "archive").rglob("*.partial"))


def test_missing_sequence_in_source_is_not_archived_or_repaired(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    connection = sqlite3.connect(service.source_database)
    with connection:
        connection.execute("DELETE FROM kalshi_ws_orderbook_events WHERE id=5")
    connection.close()
    with pytest.raises(WsRetentionError, match="sequence discontinuity"):
        service.run_once(now=NOW)
    assert manifest.chunks()[0].state is ArchiveState.FAILED
    connection = sqlite3.connect(service.source_database)
    try:
        assert (
            connection.execute("SELECT COUNT(*) FROM kalshi_ws_orderbook_events").fetchone()[0]
            == 30
        )
    finally:
        connection.close()


def test_manifest_cannot_skip_verification_states(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    records = _read_records(
        service.source_database,
        after_id=0,
        cutoff=NOW - timedelta(hours=6),
        maximum_records=10,
    )
    chunk = manifest.reserve(records, relative_path="chunk.zlib", created_at=NOW)
    with pytest.raises(WsRetentionError, match="skipped verification"):
        manifest.advance(chunk.chunk_id, ArchiveState.REPLAY_VERIFIED, now=NOW)


def test_purge_refuses_unverified_and_deletes_only_exact_verified_range(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    assert (
        WsPurgeService(service.source_database, service.archive_root, manifest, batch_rows=3)
        .run_once()
        .chunk_id
        is None
    )
    chunk = service.run_once(now=NOW).chunk
    assert chunk is not None
    purge = WsPurgeService(service.source_database, service.archive_root, manifest, batch_rows=3)
    deleted = 0
    while True:
        result = purge.run_once(now=NOW)
        deleted += result.deleted_events
        if result.remaining_events == 0:
            break
    assert deleted == chunk.event_count
    connection = sqlite3.connect(service.source_database)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM kalshi_ws_orderbook_events WHERE id BETWEEN ? AND ?",
                (chunk.first_event_id, chunk.last_event_id),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM kalshi_ws_orderbook_events WHERE id>?", (chunk.last_event_id,)
            ).fetchone()[0]
            == 21
        )
    finally:
        connection.close()
    assert manifest.chunks()[0].state is ArchiveState.PURGED


def test_purge_restart_infers_committed_delete_before_manifest_update(
    tmp_path: Path, monkeypatch
) -> None:
    service, manifest = _service(tmp_path)
    chunk = service.run_once(now=NOW).chunk
    assert chunk is not None
    purge = WsPurgeService(service.source_database, service.archive_root, manifest, batch_rows=4)
    original = manifest.update_purge_progress
    monkeypatch.setattr(
        manifest,
        "update_purge_progress",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        purge.run_once(now=NOW)
    monkeypatch.setattr(manifest, "update_purge_progress", original)
    while purge.run_once(now=NOW).remaining_events:
        pass
    assert manifest.chunks()[0].state is ArchiveState.PURGED


def test_partial_purge_recovery_rejects_non_prefix_hole(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    chunk = service.run_once(now=NOW).chunk
    assert chunk is not None
    connection = sqlite3.connect(service.source_database)
    with connection:
        connection.execute(
            "DELETE FROM kalshi_ws_orderbook_events WHERE id=?",
            (chunk.first_event_id + 4,),
        )
    connection.close()
    purge = WsPurgeService(service.source_database, service.archive_root, manifest, batch_rows=3)
    with pytest.raises(WsRetentionError, match="exact contiguous suffix"):
        purge.run_once(now=NOW)
    assert manifest.chunks()[0].purged_events == 0


def test_purge_reopens_and_reauthorizes_archive_before_delete(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    chunk = service.run_once(now=NOW).chunk
    assert chunk is not None
    archive = service.archive_root / chunk.relative_path
    archive.write_bytes(b"corrupted-after-verification")
    purge = WsPurgeService(service.source_database, service.archive_root, manifest, batch_rows=3)
    with pytest.raises(WsRetentionError, match="checksum changed"):
        purge.run_once(now=NOW)
    connection = sqlite3.connect(service.source_database)
    try:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM kalshi_ws_orderbook_events WHERE id BETWEEN ? AND ?",
            (chunk.first_event_id, chunk.last_event_id),
        ).fetchone()[0]
    finally:
        connection.close()
    assert remaining == chunk.event_count


def test_purged_archive_can_be_reopened_and_verified(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    chunk = service.run_once(now=NOW).chunk
    assert chunk is not None
    purge = WsPurgeService(
        service.source_database, service.archive_root, manifest, batch_rows=20_000
    )
    assert purge.run_once(now=NOW).remaining_events == 0
    purged = manifest.chunks()[0]
    assert purged.state is ArchiveState.PURGED
    purge.verify_preserved_archive(purged)


def test_bounded_purge_creates_reusable_pages_without_shrinking_file(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path, count=4000, chunk=3999)
    chunk = service.run_once(now=NOW).chunk
    assert chunk is not None
    physical_before = service.source_database.stat().st_size
    purge = WsPurgeService(
        service.source_database, service.archive_root, manifest, batch_rows=20_000
    )
    result = purge.run_once(now=NOW)
    assert result.deleted_events == chunk.event_count
    assert result.freelist_pages_after > result.freelist_pages_before
    assert result.reusable_bytes_increase > 0
    assert service.source_database.stat().st_size == physical_before

    metrics = manifest.storage_metrics(service.source_database)
    assert metrics.freelist_reusable_bytes > 0
    assert metrics.physical_database_bytes >= metrics.hot_sqlite_used_bytes
    assert metrics.cold_archive_bytes == chunk.compressed_bytes


def test_sqlite_new_writes_reuse_freelist_before_file_growth(tmp_path: Path) -> None:
    database = tmp_path / "page-reuse.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE observations(id INTEGER PRIMARY KEY,payload BLOB NOT NULL)")
    connection.executemany(
        "INSERT INTO observations(payload) VALUES(?)", ((b"x" * 2000,) for _ in range(5000))
    )
    connection.commit()
    page_count_before = int(connection.execute("PRAGMA page_count").fetchone()[0])
    connection.execute("DELETE FROM observations")
    connection.commit()
    freelist_after_delete = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
    assert freelist_after_delete > 0
    connection.executemany(
        "INSERT INTO observations(payload) VALUES(?)", ((b"y" * 2000,) for _ in range(4000))
    )
    connection.commit()
    page_count_after_reuse = int(connection.execute("PRAGMA page_count").fetchone()[0])
    freelist_after_reuse = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
    connection.close()
    assert page_count_after_reuse == page_count_before
    assert freelist_after_reuse < freelist_after_delete


def test_storage_growth_samples_are_low_frequency_and_bounded(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    metrics = manifest.storage_metrics(service.source_database)
    first = manifest.record_storage_sample(metrics, observed_at=NOW, maximum_samples=3)
    assert first.net_disk_growth_bytes_per_hour is None
    unchanged = manifest.record_storage_sample(
        metrics, observed_at=NOW + timedelta(seconds=30), maximum_samples=3
    )
    assert unchanged.net_disk_growth_bytes_per_hour is None
    grown = manifest.record_storage_sample(
        type(metrics)(
            hot_sqlite_used_bytes=metrics.hot_sqlite_used_bytes + 3600,
            freelist_reusable_bytes=metrics.freelist_reusable_bytes,
            physical_database_bytes=metrics.physical_database_bytes + 3600,
            wal_bytes=metrics.wal_bytes,
            cold_archive_bytes=metrics.cold_archive_bytes,
            cold_archive_growth_bytes_per_hour=metrics.cold_archive_growth_bytes_per_hour,
            cold_archive_growth_bytes_per_day=metrics.cold_archive_growth_bytes_per_day,
        ),
        observed_at=NOW + timedelta(hours=1),
        maximum_samples=3,
    )
    assert grown.net_disk_growth_bytes_per_hour == pytest.approx(3600)
    for hour in range(2, 7):
        manifest.record_storage_sample(
            metrics, observed_at=NOW + timedelta(hours=hour), maximum_samples=3
        )
    connection = sqlite3.connect(manifest.path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM ws_storage_samples").fetchone()[0] == 3
    finally:
        connection.close()


def test_failed_verification_keeps_raw_source(tmp_path: Path, monkeypatch) -> None:
    service, manifest = _service(tmp_path)
    before = service.source_database.stat().st_size
    monkeypatch.setattr(
        "live15_quant.ws_retention.read_parquet_snapshot",
        lambda _blob: (_ for _ in ()).throw(WsRetentionError("bad checksum")),
    )
    with pytest.raises(WsRetentionError, match="bad checksum"):
        service.run_once(now=NOW)
    assert service.source_database.stat().st_size == before
    assert manifest.chunks()[0].state is ArchiveState.FAILED


def test_parquet_archive_path_never_invokes_legacy_encoder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _manifest = _service(tmp_path)
    monkeypatch.setattr(
        "live15_quant.ws_archive.encode_archive_chunk",
        lambda _records: (_ for _ in ()).throw(AssertionError("legacy encoder invoked")),
    )
    archived = service.run_once(now=NOW).chunk
    assert archived is not None
    assert archived.codec == "parquet-zstd"


def test_legacy_zlib_artifact_remains_verifiable(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    records = _read_records(
        service.source_database,
        after_id=0,
        cutoff=NOW,
        maximum_records=10,
    )
    blob, metadata = encode_archive_chunk(records)
    relative_path = "legacy/chunk.zlib"
    archive = service.archive_root / relative_path
    archive.parent.mkdir(parents=True)
    archive.write_bytes(blob)
    chunk = manifest.reserve(records, relative_path=relative_path, created_at=NOW)
    connection = sqlite3.connect(manifest.path)
    with connection:
        connection.execute(
            "UPDATE ws_retention_chunks SET codec='zlib' WHERE chunk_id=?", (chunk.chunk_id,)
        )
    connection.close()
    file_checksum = hashlib.sha256(blob).hexdigest()
    for state, facts in (
        (
            ArchiveState.WRITTEN,
            {
                "logical_checksum": metadata.checksum_sha256,
                "file_checksum": file_checksum,
                "uncompressed_bytes": metadata.uncompressed_bytes,
                "compressed_bytes": len(blob),
            },
        ),
        (ArchiveState.CHECKSUM_VERIFIED, {}),
        (
            ArchiveState.REPLAY_VERIFIED,
            {"source_replay_hash": "legacy", "archive_replay_hash": "legacy"},
        ),
        (ArchiveState.COMMITTED, {}),
        (ArchiveState.PURGE_ELIGIBLE, {}),
    ):
        manifest.advance(chunk.chunk_id, state, now=NOW, **facts)
    verified = manifest.latest()
    assert verified is not None
    purge = WsPurgeService(service.source_database, service.archive_root, manifest)
    purge.verify_preserved_archive(verified)


def test_failed_archive_blocks_later_ranges_instead_of_skipping_raw_truth(
    tmp_path: Path, monkeypatch
) -> None:
    service, manifest = _service(tmp_path)
    monkeypatch.setattr(
        "live15_quant.ws_retention.read_parquet_snapshot",
        lambda _blob: (_ for _ in ()).throw(WsRetentionError("verification failed")),
    )
    with pytest.raises(WsRetentionError, match="verification failed"):
        service.run_once(now=NOW)
    monkeypatch.undo()
    with pytest.raises(WsRetentionError, match="blocks later retention"):
        service.run_once(now=NOW)
    assert len(manifest.chunks()) == 1
    assert manifest.chunks()[0].state is ArchiveState.FAILED


def test_failed_chunk_can_be_explicitly_quarantined_and_resume_pointer_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, manifest = _service(tmp_path)
    monkeypatch.setattr(
        "live15_quant.ws_retention.read_parquet_snapshot",
        lambda _blob: (_ for _ in ()).throw(WsRetentionError("missing replay baseline")),
    )
    with pytest.raises(WsRetentionError, match="missing replay baseline"):
        service.run_once(now=NOW)
    failed = manifest.chunks()[0]
    quarantined = manifest.quarantine_failed_chunk(failed.chunk_id, now=NOW)
    assert quarantined.state is ArchiveState.QUARANTINED_REPLAY_BASELINE_MISSING
    assert quarantined.failure == "REPLAY_BASELINE_MISSING"
    assert manifest.last_event_id() == failed.last_event_id
    assert manifest.metrics()["quarantined"] == 1
    # The explicit transition is idempotent for an operator retry.
    assert (
        manifest.quarantine_failed_chunk(failed.chunk_id, now=NOW).state
        is ArchiveState.QUARANTINED_REPLAY_BASELINE_MISSING
    )


def test_failed_baseline_reconciliation_requires_explicit_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, manifest = _service(tmp_path)
    monkeypatch.setattr(
        "live15_quant.ws_retention.read_parquet_snapshot",
        lambda _blob: (_ for _ in ()).throw(WsRetentionError("checksum failure")),
    )
    with pytest.raises(WsRetentionError, match="checksum failure"):
        service.run_once(now=NOW)
    failed = manifest.chunks()[0]
    with pytest.raises(WsRetentionError, match="lacks explicit"):
        manifest.quarantine_replay_baseline_failure(failed.chunk_id, now=NOW, evidence="wrong")
    assert manifest.chunks()[0].state is ArchiveState.FAILED


def test_mixed_failed_range_quarantines_only_proven_missing_market_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, manifest = _service(tmp_path)
    monkeypatch.setattr(
        "live15_quant.ws_retention.read_parquet_snapshot",
        lambda _blob: (_ for _ in ()).throw(WsRetentionError("missing replay baseline")),
    )
    with pytest.raises(WsRetentionError, match="missing replay baseline"):
        service.run_once(now=NOW)
    failed = manifest.chunks()[0]
    records = list(service._range_records(failed))
    missing_ticker = "KXBNB15M-26AUG221215-15"
    records[0] = replace(records[0], connection_id="connection-2", subscription_id=3)
    records[1] = replace(records[1], connection_id="connection-2", subscription_id=3, ticker=TICKER)
    records[2] = replace(
        records[2],
        connection_id="connection-2",
        subscription_id=3,
        ticker=missing_ticker,
        market_id="market-bnb",
    )
    monkeypatch.setattr(service, "_range_records", lambda _chunk: tuple(records))
    snapshot_time = NOW - timedelta(hours=7)
    store = RecorderStore(service.source_database)
    store.append_kalshi_ws_orderbook_event(
        KalshiOrderBookSnapshot(
            connection_id="connection-2",
            subscription_id=3,
            sequence=1,
            ticker=missing_ticker,
            market_id="market-bnb",
            yes_bids=(),
            no_bids=(),
            source_timestamp=snapshot_time,
            socket_received_timestamp=snapshot_time,
            parse_timestamp=snapshot_time + timedelta(microseconds=1),
        ),
        sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED,
    )
    store.close()
    raw_before = service.source_database.stat().st_size
    monkeypatch.setattr("live15_quant.ws_retention.decode_archive_chunk", decode_archive_chunk)
    service._reconcile_failed_baseline(NOW)
    assert manifest.chunks()[0].state is ArchiveState.QUARANTINED_REPLAY_BASELINE_MISSING
    assert service.source_database.stat().st_size == raw_before
    assert manifest.last_event_id() == failed.last_event_id


def test_delta_only_chunk_waits_for_baseline_without_publishing_or_failing(
    tmp_path: Path,
) -> None:
    service, _manifest = _service(tmp_path)
    connection = sqlite3.connect(service.source_database)
    with connection:
        connection.execute("DELETE FROM kalshi_ws_orderbook_events WHERE id IN (1,2)")
    result = service.run_once(now=NOW)
    assert result.chunk is not None
    assert result.chunk.state is ArchiveState.WAITING_FOR_REPLAY_BASELINE
    assert result.chunk.failure == "REPLAY_BASELINE_MISSING"
    assert not tuple(service.archive_root.rglob("*.zlib"))
    with pytest.raises(WsRetentionError, match="unreplayable archive chunk"):
        service.run_once(now=NOW)


def test_non_null_baseline_requires_matching_synchronized_book(tmp_path: Path) -> None:
    service, _manifest = _service(tmp_path)
    records = _read_records(
        service.source_database,
        after_id=0,
        cutoff=datetime.max.replace(tzinfo=UTC),
        maximum_records=3,
    )
    baseline = _ReplayState.empty()
    baseline.apply(records[0])
    baseline.apply(records[1])
    payload = baseline.as_json()
    assert _usable_replay_baseline(payload, records[2])
    assert not _usable_replay_baseline(payload, replace(records[2], ticker="missing"))
    assert not _usable_replay_baseline(payload, replace(records[2], market_id="wrong"))


def test_unreplayable_prefix_is_quarantined_when_later_snapshot_exists(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    with sqlite3.connect(service.source_database) as connection:
        connection.execute("DELETE FROM kalshi_ws_orderbook_events WHERE id=2")
    store = RecorderStore(service.source_database)
    snapshot_time = NOW - timedelta(hours=7)
    store.append_kalshi_ws_orderbook_event(
        KalshiOrderBookSnapshot(
            connection_id="connection-2",
            subscription_id=2,
            sequence=1,
            ticker=TICKER,
            market_id="market-1",
            yes_bids=(OrderBookLevel(Decimal("0.5000"), Decimal("10.0000")),),
            no_bids=(),
            source_timestamp=snapshot_time,
            socket_received_timestamp=snapshot_time,
            parse_timestamp=snapshot_time + timedelta(microseconds=1),
        ),
        sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED,
    )
    store.close()
    prefixes = []
    while True:
        chunk = service.run_once(now=NOW).chunk
        assert chunk is not None
        if chunk.state is not ArchiveState.QUARANTINED_REPLAY_BASELINE_MISSING:
            suffix = chunk
            break
        prefixes.append(chunk)
    assert prefixes
    assert all(item.failure == "REPLAY_BASELINE_MISSING" for item in prefixes)
    assert suffix is not None
    assert suffix.state is ArchiveState.PURGE_ELIGIBLE
    assert suffix.first_event_id > prefixes[-1].last_event_id
    assert manifest.metrics()["failed"] == 0


def test_waiting_prefix_reconciles_after_snapshot_arrives(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    with sqlite3.connect(service.source_database) as connection:
        connection.execute("DELETE FROM kalshi_ws_orderbook_events WHERE id=2")
    waiting = service.run_once(now=NOW).chunk
    assert waiting is not None
    assert waiting.state is ArchiveState.WAITING_FOR_REPLAY_BASELINE
    store = RecorderStore(service.source_database)
    snapshot_time = NOW - timedelta(hours=7)
    store.append_kalshi_ws_orderbook_event(
        KalshiOrderBookSnapshot(
            connection_id="connection-2",
            subscription_id=2,
            sequence=1,
            ticker=TICKER,
            market_id="market-1",
            yes_bids=(),
            no_bids=(),
            source_timestamp=snapshot_time,
            socket_received_timestamp=snapshot_time,
            parse_timestamp=snapshot_time + timedelta(microseconds=1),
        ),
        sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED,
    )
    store.close()
    reconciled = service.run_once(now=NOW).chunk
    assert reconciled is not None
    assert reconciled.state is ArchiveState.QUARANTINED_REPLAY_BASELINE_MISSING
    resumed = None
    while resumed is None or resumed.state is ArchiveState.QUARANTINED_REPLAY_BASELINE_MISSING:
        resumed = service.run_once(now=NOW).chunk
    assert resumed.state is ArchiveState.PURGE_ELIGIBLE
    assert manifest.metrics()["failed"] == 0


def test_waiting_prefix_reconciles_after_market_rollover_epoch(
    tmp_path: Path,
) -> None:
    service, manifest = _service(tmp_path)
    with sqlite3.connect(service.source_database) as connection:
        connection.execute("DELETE FROM kalshi_ws_orderbook_events WHERE id=2")
    waiting = service.run_once(now=NOW).chunk
    assert waiting is not None
    assert waiting.state is ArchiveState.WAITING_FOR_REPLAY_BASELINE

    store = RecorderStore(service.source_database)
    snapshot_time = NOW - timedelta(hours=7)
    rollover_tickers = (
        "KXETH15M-26AUG221230-15",
        TICKER.replace("221215", "221230"),
    )
    for sequence, ticker in enumerate(rollover_tickers, start=1):
        store.append_kalshi_ws_orderbook_event(
            KalshiOrderBookSnapshot(
                connection_id="connection-rollover",
                subscription_id=7,
                sequence=sequence,
                ticker=ticker,
                market_id=f"market-rollover-{sequence}",
                yes_bids=(),
                no_bids=(),
                source_timestamp=snapshot_time,
                socket_received_timestamp=snapshot_time + timedelta(seconds=sequence),
                parse_timestamp=snapshot_time + timedelta(seconds=sequence, microseconds=1),
            ),
            sync_status_after=(
                KalshiBookSyncStatus.SYNCHRONIZED
                if sequence == len(rollover_tickers)
                else KalshiBookSyncStatus.UNSYNCHRONIZED
            ),
        )
    store.append_kalshi_ws_orderbook_event(
        KalshiOrderBookDelta(
            connection_id="connection-rollover",
            subscription_id=7,
            sequence=3,
            ticker=rollover_tickers[1],
            market_id="market-rollover-2",
            side=KalshiBookSide.YES,
            price=Decimal("0.5000"),
            quantity_delta=Decimal("0.0001"),
            source_timestamp=snapshot_time + timedelta(seconds=3),
            socket_received_timestamp=snapshot_time + timedelta(seconds=3),
            parse_timestamp=snapshot_time + timedelta(seconds=3, microseconds=1),
        ),
        sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED,
    )
    store.close()

    reconciled = service.run_once(now=NOW).chunk
    assert reconciled is not None
    assert reconciled.state is ArchiveState.QUARANTINED_REPLAY_BASELINE_MISSING
    assert manifest.last_event_id() == reconciled.last_event_id

    resumed = None
    quarantined_suffixes = []
    while resumed is None or resumed.state is ArchiveState.QUARANTINED_REPLAY_BASELINE_MISSING:
        resumed = service.run_once(now=NOW).chunk
        assert resumed is not None
        if resumed.state is ArchiveState.QUARANTINED_REPLAY_BASELINE_MISSING:
            quarantined_suffixes.append(resumed)
    assert resumed.state is ArchiveState.PURGE_ELIGIBLE
    assert resumed.first_event_id > reconciled.last_event_id
    assert all(item.last_event_id < resumed.first_event_id for item in quarantined_suffixes)
    assert manifest.metrics()["failed"] == 0


def test_subscription_transition_without_authoritative_snapshot_stays_waiting(
    tmp_path: Path,
) -> None:
    service, manifest = _service(tmp_path)
    with sqlite3.connect(service.source_database) as connection:
        connection.execute("DELETE FROM kalshi_ws_orderbook_events WHERE id=2")
    waiting = service.run_once(now=NOW).chunk
    assert waiting is not None
    store = RecorderStore(service.source_database)
    observed = NOW - timedelta(hours=7)
    store.append_kalshi_ws_orderbook_event(
        KalshiOrderBookDelta(
            connection_id="connection-transition",
            subscription_id=8,
            sequence=1,
            ticker=TICKER,
            market_id="market-transition",
            side=KalshiBookSide.YES,
            price=Decimal("0.5000"),
            quantity_delta=Decimal("0.0001"),
            source_timestamp=observed,
            socket_received_timestamp=observed,
            parse_timestamp=observed + timedelta(microseconds=1),
        ),
        sync_status_after=KalshiBookSyncStatus.UNSYNCHRONIZED,
    )
    store.close()
    with pytest.raises(WsRetentionError, match="blocks later retention"):
        service.run_once(now=NOW)
    assert manifest.chunks()[0].state is ArchiveState.WAITING_FOR_REPLAY_BASELINE


def test_new_market_id_delta_without_snapshot_stays_waiting(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    with sqlite3.connect(service.source_database) as connection:
        connection.execute("DELETE FROM kalshi_ws_orderbook_events WHERE id=2")
    waiting = service.run_once(now=NOW).chunk
    assert waiting is not None
    store = RecorderStore(service.source_database)
    observed = NOW - timedelta(hours=7)
    store.append_kalshi_ws_orderbook_event(
        KalshiOrderBookDelta(
            connection_id="connection-1",
            subscription_id=2,
            sequence=32,
            ticker=TICKER,
            market_id="new-market-without-snapshot",
            side=KalshiBookSide.YES,
            price=Decimal("0.5000"),
            quantity_delta=Decimal("0.0001"),
            source_timestamp=observed,
            socket_received_timestamp=observed,
            parse_timestamp=observed + timedelta(microseconds=1),
        ),
        sync_status_after=KalshiBookSyncStatus.UNSYNCHRONIZED,
    )
    store.close()
    with pytest.raises(WsRetentionError, match="blocks later retention"):
        service.run_once(now=NOW)
    assert manifest.chunks()[0].state is ArchiveState.WAITING_FOR_REPLAY_BASELINE


def test_malformed_new_epoch_sequence_stays_waiting(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    with sqlite3.connect(service.source_database) as connection:
        connection.execute("DELETE FROM kalshi_ws_orderbook_events WHERE id=2")
    waiting = service.run_once(now=NOW).chunk
    assert waiting is not None
    store = RecorderStore(service.source_database)
    observed = NOW - timedelta(hours=7)
    store.append_kalshi_ws_orderbook_event(
        KalshiOrderBookSnapshot(
            connection_id="connection-malformed",
            subscription_id=9,
            sequence=2,
            ticker=TICKER.replace("221215", "221230"),
            market_id="malformed-market",
            yes_bids=(),
            no_bids=(),
            source_timestamp=observed,
            socket_received_timestamp=observed,
            parse_timestamp=observed + timedelta(microseconds=1),
        ),
        sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED,
    )
    store.close()
    with pytest.raises(WsRetentionError, match="blocks later retention"):
        service.run_once(now=NOW)
    assert manifest.chunks()[0].state is ArchiveState.WAITING_FOR_REPLAY_BASELINE


def test_unrelated_boundary_snapshot_does_not_quarantine_prefix(tmp_path: Path) -> None:
    service, _manifest = _service(tmp_path)
    with sqlite3.connect(service.source_database) as connection:
        connection.execute("DELETE FROM kalshi_ws_orderbook_events WHERE id=2")
    store = RecorderStore(service.source_database)
    snapshot_time = NOW - timedelta(hours=7)
    store.append_kalshi_ws_orderbook_event(
        KalshiOrderBookSnapshot(
            connection_id="connection-2",
            subscription_id=2,
            sequence=1,
            ticker="KXOTHER",
            market_id="other-market",
            yes_bids=(),
            no_bids=(),
            source_timestamp=snapshot_time,
            socket_received_timestamp=snapshot_time,
            parse_timestamp=snapshot_time + timedelta(microseconds=1),
        ),
        sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED,
    )
    store.close()
    result = service.run_once(now=NOW).chunk
    assert result is not None
    assert result.state is ArchiveState.WAITING_FOR_REPLAY_BASELINE


def test_purge_authorization_refuses_quarantine_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, manifest = _service(tmp_path)
    chunk = service.run_once(now=NOW).chunk
    assert chunk is not None
    monkeypatch.setattr(manifest, "quarantine_overlaps", lambda *_args: (chunk,))
    purge = WsPurgeService(service.source_database, service.archive_root, manifest)
    with pytest.raises(WsRetentionError, match="quarantined replay-baseline gap"):
        purge.run_once(now=NOW)


def test_cross_process_maintenance_lease_fails_fast(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    with manifest.maintenance_lease():
        with pytest.raises(WsMaintenanceBusy, match="maintenance pass is active"):
            service.run_once(now=NOW)
    assert service.run_once(now=NOW).chunk is not None


def test_expired_maintenance_lease_is_restart_recoverable(tmp_path: Path) -> None:
    service, manifest = _service(tmp_path)
    connection = sqlite3.connect(manifest.path)
    with connection:
        connection.execute(
            "INSERT INTO ws_retention_lease VALUES('archive-purge','crashed-owner',?)",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),),
        )
    connection.close()
    assert service.run_once(now=NOW).chunk is not None


def test_disk_quota_thresholds_fail_closed() -> None:
    quota = DiskQuota(
        warning_free_bytes=100,
        critical_free_bytes=50,
        fail_safe_free_bytes=25,
    )
    assert quota.classify(total_bytes=1000, free_bytes=500) is DiskThresholdState.NORMAL
    assert quota.classify(total_bytes=1000, free_bytes=290) is DiskThresholdState.WARNING
    assert quota.classify(total_bytes=1000, free_bytes=240) is DiskThresholdState.ARCHIVE_URGENT
    assert quota.classify(total_bytes=1000, free_bytes=140) is DiskThresholdState.CRITICAL
    assert quota.classify(total_bytes=1000, free_bytes=20) is DiskThresholdState.FAIL_SAFE


def test_compaction_benefit_gate_requires_bytes_and_percent(tmp_path: Path) -> None:
    gate = CompactionBenefitGate(100, Decimal("25"))
    assert gate.evaluate(database_bytes=1000, reclaimable_bytes=300) is True
    assert gate.evaluate(database_bytes=1000, reclaimable_bytes=99) is False
    assert gate.evaluate(database_bytes=1000, reclaimable_bytes=200) is False

    database = tmp_path / "pages.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE disposable(value BLOB)")
    connection.executemany(
        "INSERT INTO disposable VALUES(?)", ((b"x" * 1000,) for _ in range(2000))
    )
    connection.commit()
    connection.execute("DROP TABLE disposable")
    connection.commit()
    connection.close()
    decision = evaluate_database_compaction(database, CompactionBenefitGate(1, Decimal("0.01")))
    assert decision.reclaimable_bytes > 0
    assert decision.allowed is True


def test_purge_benefit_assessment_uses_disposable_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _populate(source)
    result = assess_purge_benefit(source, tmp_path / "post.sqlite3", ((1, 10),))
    assert result.deleted_rows == 10
    assert result.purge_physical_bytes > 0
    assert result.post_purge_compacted_bytes < result.baseline_compacted_bytes


def test_offline_compaction_requires_headroom_and_preserves_database(tmp_path: Path) -> None:
    source = tmp_path / "raw.sqlite3"
    _populate(source)
    with pytest.raises(WsRetentionError, match="headroom"):
        compact_database_offline(source, tmp_path / "too-large.sqlite3", minimum_free_bytes=10**18)
    destination = tmp_path / "compact.sqlite3"
    result = compact_database_offline(source, destination, minimum_free_bytes=0)
    assert result["compacted_bytes"] <= result["source_bytes"]
    source_connection = sqlite3.connect(source)
    compact_connection = sqlite3.connect(destination)
    try:
        assert (
            source_connection.execute("SELECT COUNT(*) FROM kalshi_ws_orderbook_events").fetchone()
            == compact_connection.execute(
                "SELECT COUNT(*) FROM kalshi_ws_orderbook_events"
            ).fetchone()
        )
    finally:
        source_connection.close()
        compact_connection.close()


def test_verified_compact_swap_retains_rollback(tmp_path: Path) -> None:
    source = tmp_path / "raw.sqlite3"
    _populate(source)
    compacted = tmp_path / "compact.sqlite3"
    compact_database_offline(source, compacted, minimum_free_bytes=0)
    rollback = tmp_path / "raw.rollback.sqlite3"
    result = swap_compacted_database(source, compacted, rollback)
    assert source.is_file() and rollback.is_file() and not compacted.exists()
    assert result.rollback_path == rollback
    connection = sqlite3.connect(source)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_failed_compact_swap_automatically_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "raw.sqlite3"
    _populate(source)
    compacted = tmp_path / "compact.sqlite3"
    compact_database_offline(source, compacted, minimum_free_bytes=0)
    rollback = tmp_path / "raw.rollback.sqlite3"
    original = Path.replace

    def fail_new_database(value: Path, target: Path) -> Path:
        if value == compacted:
            raise OSError("simulated atomic swap failure")
        return original(value, target)

    monkeypatch.setattr(Path, "replace", fail_new_database)
    with pytest.raises(OSError, match="simulated"):
        swap_compacted_database(source, compacted, rollback)
    assert source.is_file() and compacted.is_file() and not rollback.exists()
