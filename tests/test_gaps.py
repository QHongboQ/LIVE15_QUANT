from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import live15_quant.storage as storage_module
from live15_quant.config import Settings
from live15_quant.dataset import DatasetBuildConfig, DatasetBuilder, FeatureStore
from live15_quant.gaps import (
    DataGap,
    GapReason,
    GapSource,
    GapStream,
    InferenceReadinessStatus,
    configured_streams,
    detect_gaps,
    effective_data_gaps,
    inference_readiness,
)
from live15_quant.market_sessions import MarketDataState
from live15_quant.models import (
    Asset,
    FreshnessState,
    MarketTick,
    RecorderEventSeverity,
    RecorderEventType,
    UnderlyingObservation,
    UnderlyingProvider,
)
from live15_quant.records import SCHEMA_VERSION
from live15_quant.storage import DataGapConflictError, RecorderStore
from tests.test_dataset import BASE, add_event, sampling


def gap(
    start: datetime,
    end: datetime | None,
    *,
    source: GapSource = GapSource.COINBASE,
    reason: GapReason = GapReason.OBSERVATION_INTERVAL,
    recovered: bool = True,
) -> DataGap:
    return DataGap(
        source=source,
        asset=Asset.BTC,
        instrument="BTC-USD" if source is GapSource.COINBASE else Asset.BTC.value,
        gap_start=start,
        gap_end=end,
        detected_at=(end or start + timedelta(seconds=15)) + timedelta(seconds=1),
        threshold_seconds=Decimal("15"),
        reason=reason,
        recovered=recovered,
        recorder_session_id="session-1",
    )


def test_gap_storage_is_idempotent_conflict_safe_and_deterministic(tmp_path) -> None:
    first = gap(BASE, BASE + timedelta(seconds=40))
    second = gap(BASE + timedelta(minutes=1), BASE + timedelta(minutes=2))
    with RecorderStore(tmp_path / "raw.sqlite3") as store:
        assert store.append_data_gap(second)
        assert store.append_data_gap(first)
        assert not store.append_data_gap(
            replace(first, detected_at=first.detected_at + timedelta(hours=1))
        )
        with pytest.raises(DataGapConflictError):
            store.append_data_gap(replace(first, reason=GapReason.SOURCE_OUTAGE))
        replayed = store.replay_data_gaps()

    assert replayed == (first, second)
    assert replayed[0].duration_seconds == Decimal("40")


def test_active_gap_is_append_only_idempotent_and_closed_by_recovery(tmp_path) -> None:
    active = gap(BASE, None, recovered=False)
    recovered = gap(BASE, BASE + timedelta(seconds=40))
    with RecorderStore(tmp_path / "raw.sqlite3") as store:
        assert store.append_data_gap(active)
        assert not store.append_data_gap(
            replace(active, detected_at=active.detected_at + timedelta(seconds=1))
        )
        assert store.active_data_gaps() == (active,)
        assert store.append_data_gap(recovered)
        assert store.active_data_gaps() == ()
        assert store.replay_data_gaps() == (active, recovered)
        with pytest.raises(DataGapConflictError):
            store.append_data_gap(replace(recovered, gap_end=BASE + timedelta(seconds=41)))
    assert effective_data_gaps((active, recovered)) == (recovered,)


def test_active_gap_overlap_fails_live_inference_closed() -> None:
    checked = BASE + timedelta(minutes=5)
    active = gap(checked - timedelta(minutes=1), None, recovered=False)
    result = inference_readiness(
        checked_at=checked,
        required_since=checked - timedelta(minutes=5),
        latest_received=checked - timedelta(seconds=1),
        max_age=timedelta(seconds=15),
        active_gaps=(active,),
    )
    assert result.status is InferenceReadinessStatus.DATA_UNAVAILABLE
    assert result.reasons == ("source_gap_overlap",)


def test_gap_overlap_respects_manifest_boundary_without_future_close_leakage(tmp_path) -> None:
    active = gap(BASE, None, recovered=False)
    recovered = gap(BASE, BASE + timedelta(seconds=40))
    with RecorderStore(tmp_path / "raw.sqlite3") as store:
        store.append_data_gap(active)
        open_boundary = int(
            store._connection.execute("SELECT MAX(id) FROM data_gaps").fetchone()[0]
        )
        store.append_data_gap(recovered)
        closed_boundary = int(
            store._connection.execute("SELECT MAX(id) FROM data_gaps").fetchone()[0]
        )
        query_start = BASE + timedelta(seconds=50)
        query_end = BASE + timedelta(seconds=60)
        as_open = store.replay_data_gaps(start=query_start, end=query_end, max_row_id=open_boundary)
        as_closed = store.replay_data_gaps(
            start=query_start, end=query_end, max_row_id=closed_boundary
        )

    assert as_open == (active,)
    assert as_closed == ()


def test_schema_v8_to_v9_gap_migration_is_atomic(tmp_path) -> None:
    path = tmp_path / "v8.sqlite3"
    with RecorderStore(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE data_gaps")
    connection.execute("UPDATE recorder_metadata SET value='8' WHERE key='schema_version'")
    connection.commit()
    connection.close()

    with RecorderStore(path) as migrated:
        version = migrated._connection.execute(
            "SELECT value FROM recorder_metadata WHERE key='schema_version'"
        ).fetchone()[0]
        assert version == str(SCHEMA_VERSION)
        assert migrated.count("data_gaps") == 0
        assert migrated.integrity_check() == "ok"
        assert migrated._connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_v8_to_v9_gap_migration_rolls_back_atomically(tmp_path, monkeypatch) -> None:
    path = tmp_path / "v8-rollback.sqlite3"
    with RecorderStore(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE data_gaps")
    connection.execute("UPDATE recorder_metadata SET value='8' WHERE key='schema_version'")
    connection.commit()
    connection.close()

    monkeypatch.setattr(storage_module, "_DATA_GAP_OVERLAP_INDEX_SQL", "INVALID SQL")
    with pytest.raises(sqlite3.Error):
        RecorderStore(path)

    connection = sqlite3.connect(path)
    try:
        version = connection.execute(
            "SELECT value FROM recorder_metadata WHERE key='schema_version'"
        ).fetchone()[0]
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='data_gaps'"
        ).fetchone()
    finally:
        connection.close()
    assert version == "8"
    assert table is None


def test_gap_overlap_query_uses_bounded_index(tmp_path) -> None:
    with RecorderStore(tmp_path / "raw.sqlite3") as store:
        plan = " ".join(
            str(row[3])
            for row in store._connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM data_gaps "
                "WHERE asset=? AND source=? AND recovered=1 "
                "AND gap_end>? AND gap_start<?",
                (Asset.BTC.value, GapSource.COINBASE.value, BASE.isoformat(), BASE.isoformat()),
            )
        )
    assert "idx_data_gap_overlap" in plan
    assert "USE TEMP B-TREE" not in plan


def test_detector_uses_received_timestamps_and_classifies_restart(tmp_path) -> None:
    with RecorderStore(tmp_path / "raw.sqlite3") as store:
        for received in (BASE, BASE + timedelta(seconds=45)):
            store.append_coinbase(
                MarketTick(
                    symbol="BTC-USD",
                    price=Decimal("1"),
                    bid=Decimal("0.9"),
                    ask=Decimal("1.1"),
                    received_at=received,
                    exchange_time=received,
                )
            )
        store.append_recorder_event(
            observed_timestamp=BASE + timedelta(seconds=30),
            severity=RecorderEventSeverity.INFO,
            event_type=RecorderEventType.RECORDER_RECOVERED,
            asset=None,
            source="recorder",
            error_type=None,
            message="recovered",
        )
        stream = next(
            item
            for item in configured_streams(Settings())
            if item.source is GapSource.COINBASE and item.asset is Asset.BTC
        )
        detected = detect_gaps(
            store._connection,
            (stream,),
            start=BASE,
            end=BASE + timedelta(minutes=1),
            detected_at=BASE + timedelta(minutes=1),
            immutable_snapshot=True,
        )

    assert len(detected) == 1
    assert detected[0].reason is GapReason.RESTART
    assert detected[0].gap_start == BASE
    assert detected[0].gap_end == BASE + timedelta(seconds=45)


def test_detector_refuses_active_database_analytics(tmp_path) -> None:
    with RecorderStore(tmp_path / "raw.sqlite3") as store:
        with pytest.raises(ValueError, match="immutable database snapshot"):
            detect_gaps(
                store._connection,
                (),
                start=BASE,
                end=BASE + timedelta(minutes=1),
                detected_at=BASE + timedelta(minutes=1),
                immutable_snapshot=False,
            )


def test_detector_does_not_apply_another_source_failure_to_coinbase(tmp_path) -> None:
    with RecorderStore(tmp_path / "raw.sqlite3") as store:
        for received in (BASE, BASE + timedelta(seconds=45), BASE + timedelta(seconds=90)):
            store.append_coinbase(
                MarketTick(
                    symbol="BTC-USD",
                    price=Decimal("1"),
                    bid=Decimal("0.9"),
                    ask=Decimal("1.1"),
                    received_at=received,
                    exchange_time=received,
                )
            )
        for observed, source in (
            (BASE + timedelta(seconds=30), "kalshi_quote:BTC"),
            (BASE + timedelta(seconds=75), "coinbase"),
        ):
            store.append_recorder_event(
                observed_timestamp=observed,
                severity=RecorderEventSeverity.WARNING,
                event_type=RecorderEventType.SOURCE_UNAVAILABLE,
                asset=Asset.BTC,
                source=source,
                error_type="TimeoutError",
                message="temporary source failure",
            )
        stream = next(
            item
            for item in configured_streams(Settings())
            if item.source is GapSource.COINBASE and item.asset is Asset.BTC
        )
        detected = detect_gaps(
            store._connection,
            (stream,),
            start=BASE,
            end=BASE + timedelta(minutes=2),
            detected_at=BASE + timedelta(minutes=2),
            immutable_snapshot=True,
        )

    assert [item.reason for item in detected] == [
        GapReason.OBSERVATION_INTERVAL,
        GapReason.SOURCE_OUTAGE,
    ]


def test_partial_event_gap_quarantines_only_affected_decision(tmp_path) -> None:
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        add_event(source, BASE, result="yes")
        first_decision = BASE + timedelta(minutes=5)
        opened = gap(
            first_decision - timedelta(seconds=10),
            None,
            source=GapSource.KALSHI_REST,
            recovered=False,
        )
        source.append_data_gaps(
            (
                opened,
                replace(
                    opened,
                    gap_end=first_decision + timedelta(seconds=1),
                    recovered=True,
                ),
            )
        )
        summary = DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))
        rows = destination.replay(summary.build_id)

    assert len(rows) == 1
    assert rows[0].decision_timestamp == BASE + timedelta(minutes=10)
    assert summary.diagnostics["trainability_rejections"] == {"source_gap_overlap": 1}


def test_whole_event_runtime_stall_gap_is_non_trainable(tmp_path) -> None:
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        add_event(source, BASE, result="no")
        source.append_data_gap(
            gap(
                BASE,
                BASE + timedelta(minutes=15),
                reason=GapReason.RUNTIME_STALL,
            )
        )
        summary = DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))

    assert summary.rows == 0
    assert summary.diagnostics["trainability_rejections"] == {"runtime_stall_gap": 2}


def test_restart_gap_reason_is_preserved_in_dataset_quarantine(tmp_path) -> None:
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        add_event(source, BASE, result="yes")
        source.append_data_gap(
            gap(
                BASE + timedelta(minutes=4, seconds=10),
                BASE + timedelta(minutes=4, seconds=20),
                reason=GapReason.RESTART,
            )
        )
        summary = DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))

    assert summary.rows == 1
    assert summary.diagnostics["trainability_rejections"] == {"restart_gap": 1}


def test_live_inference_fails_closed_for_gap_stale_disconnect_and_unsynced_book() -> None:
    checked = BASE + timedelta(minutes=5)
    result = inference_readiness(
        checked_at=checked,
        required_since=checked - timedelta(minutes=5),
        latest_received=checked - timedelta(seconds=40),
        max_age=timedelta(seconds=15),
        active_gaps=(gap(checked - timedelta(minutes=1), checked + timedelta(seconds=1)),),
        source_connected=False,
        synchronized_orderbook=False,
        lookback_complete=False,
    )

    assert result.status is InferenceReadinessStatus.DATA_UNAVAILABLE
    assert set(result.reasons) == {
        "stale_source",
        "insufficient_lookback",
        "source_gap_overlap",
        "source_disconnected",
        "orderbook_unsynchronized",
    }


def test_live_inference_passes_only_with_complete_current_lookback() -> None:
    checked = datetime(2026, 8, 22, tzinfo=UTC)
    result = inference_readiness(
        checked_at=checked,
        required_since=checked - timedelta(minutes=5),
        latest_received=checked - timedelta(minutes=5),
        max_age=timedelta(minutes=6),
    )
    assert result.status is InferenceReadinessStatus.PASS
    assert result.reasons == ()


def test_market_closed_live_inference_fails_closed_without_source_failure() -> None:
    checked = datetime(2026, 8, 22, 4, 0, tzinfo=UTC)
    result = inference_readiness(
        checked_at=checked,
        required_since=checked - timedelta(minutes=5),
        latest_received=datetime(2026, 8, 21, 20, 59, tzinfo=UTC),
        max_age=timedelta(seconds=15),
        underlying_state=MarketDataState.MARKET_CLOSED,
    )
    assert result.status is InferenceReadinessStatus.DATA_UNAVAILABLE
    assert "market_closed" in result.reasons
    assert "source_disconnected" not in result.reasons


def test_historical_gap_detection_excludes_normal_market_closure(tmp_path) -> None:
    before_close = datetime(2026, 8, 21, 20, 59, 55, tzinfo=UTC)
    after_reopen = datetime(2026, 8, 23, 22, 0, 5, tzinfo=UTC)
    with RecorderStore(tmp_path / "closed.sqlite3") as store:
        for received in (before_close, after_reopen):
            store.append_underlying(
                UnderlyingObservation(
                    asset=Asset.GOLD,
                    provider=UnderlyingProvider.PYTH_HERMES,
                    symbol="Metal.XAU/USD",
                    feed_id="a" * 64,
                    price=Decimal("3388"),
                    source_timestamp=received,
                    received_timestamp=received,
                    confidence=None,
                    provenance="official-test",
                    freshness=FreshnessState.FRESH,
                )
            )
        gaps = detect_gaps(
            store._connection,
            (GapStream(GapSource.PYTH, Asset.GOLD, "Metal.XAU/USD", Decimal("15")),),
            start=before_close,
            end=after_reopen,
            detected_at=after_reopen,
            immutable_snapshot=True,
        )
    assert gaps == ()
