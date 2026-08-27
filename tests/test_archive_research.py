from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from live15_quant.config import Settings
from live15_quant.kalshi_ws import (
    KalshiBookSide,
    KalshiBookSyncStatus,
    KalshiCommandAcknowledged,
    KalshiOrderBookDelta,
    KalshiOrderBookSnapshot,
)
from live15_quant.models import OrderBookLevel
from live15_quant.research_data_authority import ResearchDataAuthority
from live15_quant.storage import RecorderStore
from live15_quant.ws_retention import ArchiveState, WsArchiveService, WsRetentionManifest

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
TICKER = "KXBTC15M-26AUG221215-15"


def _populate(
    path: Path,
    *,
    count: int = 8,
    future_received_sequence: int | None = None,
    reconnect_at_event: int | None = None,
) -> None:
    store = RecorderStore(path)
    received = NOW - timedelta(hours=8)
    store.append_kalshi_ws_orderbook_event(
        KalshiCommandAcknowledged(
            connection_id="connection-1",
            request_id=1,
            subscription_id=2,
            sequence=1,
            market_tickers=(TICKER,),
            socket_received_timestamp=received,
            parse_timestamp=received + timedelta(microseconds=1),
        ),
        sync_status_after=KalshiBookSyncStatus.UNSYNCHRONIZED,
    )
    store.append_kalshi_ws_orderbook_event(
        KalshiOrderBookSnapshot(
            connection_id="connection-1",
            subscription_id=2,
            sequence=2,
            ticker=TICKER,
            market_id="market-1",
            yes_bids=(OrderBookLevel(Decimal("0.50"), Decimal("10")),),
            no_bids=(OrderBookLevel(Decimal("0.49"), Decimal("11")),),
            source_timestamp=received,
            socket_received_timestamp=received + timedelta(microseconds=2),
            parse_timestamp=received + timedelta(microseconds=3),
        ),
        sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED,
    )
    for sequence in range(3, count + 1):
        observed = received + timedelta(seconds=sequence)
        if reconnect_at_event == sequence:
            store.append_kalshi_ws_orderbook_event(
                KalshiCommandAcknowledged(
                    connection_id="connection-2",
                    request_id=2,
                    subscription_id=3,
                    sequence=1,
                    market_tickers=(TICKER,),
                    socket_received_timestamp=observed,
                    parse_timestamp=observed + timedelta(microseconds=1),
                ),
                sync_status_after=KalshiBookSyncStatus.UNSYNCHRONIZED,
            )
            continue
        if reconnect_at_event is not None and sequence == reconnect_at_event + 1:
            store.append_kalshi_ws_orderbook_event(
                KalshiOrderBookSnapshot(
                    connection_id="connection-2",
                    subscription_id=3,
                    sequence=2,
                    ticker=TICKER,
                    market_id="market-2",
                    yes_bids=(OrderBookLevel(Decimal("0.20"), Decimal("10")),),
                    no_bids=(OrderBookLevel(Decimal("0.19"), Decimal("11")),),
                    source_timestamp=observed,
                    socket_received_timestamp=observed,
                    parse_timestamp=observed + timedelta(microseconds=1),
                ),
                sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED,
            )
            continue
        reconnected = bool(reconnect_at_event and sequence > reconnect_at_event)
        connection_id = "connection-2" if reconnected else "connection-1"
        subscription_id = 3 if reconnected else 2
        replay_sequence = (
            sequence - reconnect_at_event + 1
            if reconnect_at_event and sequence > reconnect_at_event
            else sequence
        )
        socket_received = (
            observed + timedelta(seconds=30) if future_received_sequence == sequence else observed
        )
        store.append_kalshi_ws_orderbook_event(
            KalshiOrderBookDelta(
                connection_id=connection_id,
                subscription_id=subscription_id,
                sequence=replay_sequence,
                ticker=TICKER,
                market_id="market-2" if connection_id == "connection-2" else "market-1",
                side=KalshiBookSide.YES,
                price=Decimal("0.50"),
                quantity_delta=Decimal("1"),
                source_timestamp=observed,
                socket_received_timestamp=socket_received,
                parse_timestamp=socket_received + timedelta(microseconds=1),
            ),
            sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED,
        )
    store.close()


def _archive(
    tmp_path: Path,
    *,
    count: int = 8,
    chunk_records: int = 8,
    future_received_sequence: int | None = None,
    reconnect_at_event: int | None = None,
):
    database = tmp_path / "raw.sqlite3"
    root = tmp_path / "archive"
    manifest_path = tmp_path / "manifest.sqlite3"
    _populate(
        database,
        count=count,
        future_received_sequence=future_received_sequence,
        reconnect_at_event=reconnect_at_event,
    )
    manifest = WsRetentionManifest(manifest_path)
    service = WsArchiveService(
        database,
        root,
        manifest,
        hot_retention=timedelta(hours=6),
        chunk_records=chunk_records,
    )
    chunks = []
    while result := service.run_once(now=NOW).chunk:
        assert result.state is ArchiveState.PURGE_ELIGIBLE
        chunks.append(result)
    assert chunks
    return database, root, manifest_path, tuple(chunks)


def test_verified_archive_materializes_causal_provenance_bearing_book_states(
    tmp_path: Path,
) -> None:
    from live15_quant.archive_research import ArchiveResearchQuery, ArchiveResearchSourceAdapter

    _database, root, manifest_path, (chunk,) = _archive(tmp_path)
    result = ArchiveResearchSourceAdapter(root, manifest_path).materialize(
        ArchiveResearchQuery(
            chunk.first_event_id,
            chunk.last_event_id,
            as_of_timestamp=NOW,
            maximum_chunks=1,
        )
    )

    assert result.available is True
    assert result.source_identity.startswith("archive-research-")
    assert result.chunk_ids == (chunk.chunk_id,)
    assert [item.event_id for item in result.materializations] == list(
        range(chunk.first_event_id + 1, chunk.last_event_id + 1)
    )
    latest = result.materializations[-1]
    assert latest.archive_chunk_id == chunk.chunk_id
    assert latest.sequence == chunk.last_sequence
    assert latest.source_id == "live15_verified_archive"
    assert latest.materialization_timestamp >= NOW
    assert latest.as_of_timestamp == NOW
    assert latest.replay_state_hash == chunk.archive_replay_hash
    assert latest.yes_bid_depth == Decimal("16")
    assert latest.research_observation().source_id == "live15_verified_archive"


def test_checksum_mismatch_is_unavailable(tmp_path: Path) -> None:
    from live15_quant.archive_research import ArchiveResearchQuery, ArchiveResearchSourceAdapter

    _database, root, manifest_path, (chunk,) = _archive(tmp_path)
    adapter = ArchiveResearchSourceAdapter(root, manifest_path)
    query = ArchiveResearchQuery(chunk.first_event_id, chunk.last_event_id, NOW, maximum_chunks=1)
    archive_path = root / chunk.relative_path
    archive_path.write_bytes(b"broken")
    assert adapter.materialize(query).reason == "ARCHIVE_FILE_CHECKSUM_MISMATCH"


def test_as_of_excludes_future_delta_and_output_is_deterministic(tmp_path: Path) -> None:
    from live15_quant.archive_research import ArchiveResearchQuery, ArchiveResearchSourceAdapter

    _database, root, manifest_path, (chunk,) = _archive(tmp_path)
    cutoff = NOW - timedelta(hours=8) + timedelta(seconds=4)
    query = ArchiveResearchQuery(
        chunk.first_event_id, chunk.last_event_id, cutoff, maximum_chunks=1
    )
    adapter = ArchiveResearchSourceAdapter(root, manifest_path)
    first = adapter.materialize(query)
    second = adapter.materialize(query)

    assert first.source_identity == second.source_identity
    assert first.chunk_ids == second.chunk_ids
    assert [item.replay_state_hash for item in first.materializations] == [
        item.replay_state_hash for item in second.materializations
    ]
    assert [item.sequence for item in first.materializations] == [2, 3, 4]
    assert first.materializations[-1].yes_bid_depth == Decimal("12")


def test_quarantined_range_and_bounded_range_fail_closed(tmp_path: Path) -> None:
    from live15_quant.archive_research import ArchiveResearchQuery, ArchiveResearchSourceAdapter

    _database, root, manifest_path, (first, second) = _archive(tmp_path, count=16, chunk_records=8)
    manifest = WsRetentionManifest(manifest_path)
    manifest.advance(second.chunk_id, ArchiveState.FAILED, now=NOW)
    manifest.quarantine_failed_chunk(second.chunk_id, now=NOW)
    adapter = ArchiveResearchSourceAdapter(root, manifest_path)
    crossed = adapter.materialize(
        ArchiveResearchQuery(first.first_event_id, second.last_event_id, NOW, maximum_chunks=2)
    )
    bounded = adapter.materialize(
        ArchiveResearchQuery(first.first_event_id, first.last_event_id, NOW, maximum_chunks=0)
    )

    assert crossed.available is False
    assert crossed.reason == "ARCHIVE_CHUNK_NOT_RESEARCH_ELIGIBLE"
    assert bounded.reason == "ARCHIVE_QUERY_MAXIMUM_CHUNKS_INVALID"
    assert (
        adapter.materialize(
            ArchiveResearchQuery(first.first_event_id, second.last_event_id, NOW, maximum_chunks=1)
        ).reason
        == "ARCHIVE_QUERY_MAXIMUM_CHUNKS_EXCEEDED"
    )
    assert (
        adapter.materialize(
            ArchiveResearchQuery(first.first_event_id, first.last_event_id, NOW, maximum_chunks=5)
        ).reason
        == "ARCHIVE_QUERY_MAXIMUM_CHUNKS_HARD_LIMIT"
    )


def test_missing_baseline_and_missing_file_fail_closed(tmp_path: Path) -> None:
    from live15_quant.archive_research import ArchiveResearchQuery, ArchiveResearchSourceAdapter

    _database, root, manifest_path, (_first, second) = _archive(tmp_path, count=16, chunk_records=8)
    connection = sqlite3.connect(manifest_path)
    try:
        connection.execute(
            "DELETE FROM ws_retention_chunks WHERE last_event_id<?", (second.first_event_id,)
        )
        connection.commit()
    finally:
        connection.close()
    adapter = ArchiveResearchSourceAdapter(root, manifest_path)
    query = ArchiveResearchQuery(second.first_event_id, second.last_event_id, NOW, maximum_chunks=1)

    assert adapter.materialize(query).reason == "ARCHIVE_REPLAY_INVALID"
    (root / second.relative_path).unlink()
    assert adapter.materialize(query).reason == "ARCHIVE_FILE_UNAVAILABLE"


def test_as_of_requires_both_source_and_received_timestamps(tmp_path: Path) -> None:
    from live15_quant.archive_research import ArchiveResearchQuery, ArchiveResearchSourceAdapter

    _database, root, manifest_path, (chunk,) = _archive(
        tmp_path, count=3, future_received_sequence=3
    )
    cutoff = NOW - timedelta(hours=8) + timedelta(seconds=4)
    result = ArchiveResearchSourceAdapter(root, manifest_path).materialize(
        ArchiveResearchQuery(chunk.first_event_id, chunk.last_event_id, cutoff, maximum_chunks=1)
    )

    assert [item.sequence for item in result.materializations] == [2]
    assert result.materializations[-1].yes_bid_depth == Decimal("10")


def test_baseline_source_timestamp_after_as_of_is_unavailable(tmp_path: Path) -> None:
    from live15_quant.archive_research import ArchiveResearchQuery, ArchiveResearchSourceAdapter

    _database, root, manifest_path, (_first, second) = _archive(tmp_path, count=16, chunk_records=8)
    connection = sqlite3.connect(manifest_path)
    try:
        connection.execute(
            "UPDATE ws_retention_chunks SET last_source_timestamp=? WHERE last_event_id<?",
            ((NOW + timedelta(days=1)).isoformat(), second.first_event_id),
        )
        connection.commit()
    finally:
        connection.close()

    assert (
        ArchiveResearchSourceAdapter(root, manifest_path)
        .materialize(
            ArchiveResearchQuery(second.first_event_id, second.last_event_id, NOW, maximum_chunks=1)
        )
        .reason
        == "ARCHIVE_REPLAY_BASELINE_AFTER_AS_OF"
    )


def test_reconnect_snapshot_does_not_reuse_another_stream_baseline(tmp_path: Path) -> None:
    from live15_quant.archive_research import ArchiveResearchQuery, ArchiveResearchSourceAdapter

    _database, root, manifest_path, (_first, second) = _archive(
        tmp_path, count=16, chunk_records=8, reconnect_at_event=9
    )
    result = ArchiveResearchSourceAdapter(root, manifest_path).materialize(
        ArchiveResearchQuery(second.first_event_id, second.last_event_id, NOW, maximum_chunks=1)
    )

    assert result.available is True
    assert result.materializations[0].connection_id == "connection-2"
    assert result.materializations[0].sequence == 2


def test_authority_builds_snapshot_and_canonical_evidence_from_explicit_archive_query(
    tmp_path: Path,
) -> None:
    from live15_quant.archive_research import ArchiveResearchQuery
    from live15_quant.canonical_evidence import build_canonical_evidence_snapshot

    database, root, manifest_path, (chunk,) = _archive(tmp_path)
    settings = Settings(
        recorder_data_path=database,
        current_trainable_path=tmp_path / "current.sqlite3",
        ws_archive_root=root,
        ws_archive_manifest_path=manifest_path,
        feature_store_path=tmp_path / "features.sqlite3",
        paper_data_path=tmp_path / "paper.sqlite3",
        recorder_health_path=tmp_path / "health.json",
        recorder_control_path=tmp_path / "control.json",
        recorder_pid_path=tmp_path / "recorder.pid",
        readiness_report_path=tmp_path / "readiness.json",
    )
    query = ArchiveResearchQuery(chunk.first_event_id, chunk.last_event_id, NOW, maximum_chunks=1)
    authority = ResearchDataAuthority(settings, project_root=tmp_path)
    universe, selection = authority.archive_research_snapshot(query, code_git_sha="a" * 40)
    evidence = build_canonical_evidence_snapshot(
        experiment_id="archive-adapter-test",
        experiment_cutoff=NOW,
        records=(selection.canonical_evidence_record(),),
    )

    assert universe.selected_source_ids == ("live15_verified_archive",)
    assert universe.holdout_accessed is False
    assert selection.available is True
    assert universe.source_manifests[0].coverage_status == {
        "materialization": "EXPLICIT_BOUNDED_READ_ONLY",
        "metadata": "VERIFIED_METADATA",
        "replay": "REPLAY_VERIFIED",
        "selection": "AVAILABLE",
    }
    assert all("dataset" not in item.source_id for item in universe.source_manifests)
    assert evidence.records[0].artifact_id == selection.source_identity
    assert evidence.records[0].provenance_tier == "H0_LIVE_NATIVE"

