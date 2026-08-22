from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from live15_quant.config import Settings
from live15_quant.gaps import DataGap, GapReason, GapSource
from live15_quant.market_sessions import MarketDataState
from live15_quant.models import Asset, FreshnessState, UnderlyingObservation, UnderlyingProvider
from live15_quant.readiness import (
    ReadinessStatus,
    SnapshotTimeoutError,
    _gap_report,
    _historical_gap_streams,
    _live_source_ready_by_asset,
    _live_source_state_by_asset,
    _quality,
    _ratio_percent,
    _readiness_status,
    _windowed_coverage,
    _worker_progress_report,
    snapshot_database,
)
from live15_quant.storage import RecorderStore

NOW = datetime(2026, 8, 21, tzinfo=UTC)


def test_gap_report_excludes_normal_closed_session_from_reliability() -> None:
    friday_close = NOW.replace(hour=21)
    saturday = friday_close + timedelta(hours=7)
    closed = DataGap(
        source=GapSource.KALSHI_REST,
        asset=Asset.GOLD,
        instrument="KXGOLD15M",
        gap_start=friday_close,
        gap_end=None,
        detected_at=saturday,
        threshold_seconds=Decimal("15"),
        reason=GapReason.SOURCE_OUTAGE,
        recovered=False,
    )

    report = _gap_report((closed,), {}, snapshot_at=saturday)

    assert report["active_gap_count"] == 0
    assert report["gap_count"] == 0


def test_readiness_never_redetects_gaps_from_retention_pruned_ws_hot_rows() -> None:
    streams = _historical_gap_streams(Settings())
    assert streams
    assert all(stream.source is not GapSource.KALSHI_WS for stream in streams)
    assert any(stream.source is GapSource.KALSHI_REST for stream in streams)


def test_readiness_quality_ranges_use_asset_provider_indexes(tmp_path: Path) -> None:
    with RecorderStore(tmp_path / "plans.sqlite3") as store:
        plans = {
            "quote": store._connection.execute(
                "EXPLAIN QUERY PLAN SELECT source_timestamp FROM kalshi_prediction_quotes "
                "WHERE asset=? AND received_timestamp>=? AND received_timestamp<=? "
                "ORDER BY received_timestamp,id",
                (Asset.BTC.value, NOW.isoformat(), NOW.isoformat()),
            ).fetchall(),
            "coinbase": store._connection.execute(
                "EXPLAIN QUERY PLAN SELECT exchange_timestamp FROM coinbase_ticks "
                "WHERE product=? AND received_timestamp>=? AND received_timestamp<=? "
                "ORDER BY received_timestamp,id",
                ("BTC-USD", NOW.isoformat(), NOW.isoformat()),
            ).fetchall(),
            "pyth": store._connection.execute(
                "EXPLAIN QUERY PLAN SELECT source_timestamp FROM underlying_observations "
                "WHERE asset=? AND provider=? AND received_timestamp>=? "
                "AND received_timestamp<=? ORDER BY received_timestamp,id",
                (
                    Asset.GOLD.value,
                    UnderlyingProvider.PYTH_HERMES.value,
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            ).fetchall(),
        }

    text = {name: " ".join(str(row[3]) for row in rows) for name, rows in plans.items()}
    assert "idx_kalshi_native_quote_asset_cursor" in text["quote"]
    assert "idx_coinbase_product_replay" in text["coinbase"]
    assert "idx_underlying_asset_provider_replay" in text["pyth"]
    assert all("USE TEMP B-TREE" not in plan for plan in text.values())


def test_trainable_event_coverage_uses_evaluated_finalized_events() -> None:
    assert _ratio_percent(365, 857) == 42.590432
    assert _ratio_percent(0, 0) is None


def test_snapshot_database_is_consistent_and_does_not_modify_source(tmp_path: Path) -> None:
    source = tmp_path / "raw.sqlite3"
    destination = tmp_path / "snapshot.sqlite3"
    with RecorderStore(source) as store:
        store.append_underlying(
            UnderlyingObservation(
                asset=Asset.GOLD,
                provider=UnderlyingProvider.PYTH_HERMES,
                symbol="Metal.XAU/USD",
                feed_id="a" * 64,
                price=Decimal("3388.1"),
                source_timestamp=NOW,
                received_timestamp=NOW + timedelta(milliseconds=10),
                confidence=None,
                provenance="official",
                freshness=FreshnessState.FRESH,
            )
        )
    before = source.read_bytes()
    snapshot_database(source, destination)
    assert source.read_bytes() == before
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM underlying_observations").fetchone()[0] == 1


def test_snapshot_database_deadline_cleans_partial_destination(tmp_path: Path) -> None:
    source = tmp_path / "raw.sqlite3"
    destination = tmp_path / "partial.sqlite3"
    with RecorderStore(source):
        pass
    with pytest.raises(SnapshotTimeoutError):
        snapshot_database(source, destination, max_seconds=1e-12, pages_per_step=1)
    assert not destination.exists()


def test_windowed_coverage_preserves_real_gap_and_boundary_outage() -> None:
    windows = _windowed_coverage(
        iter(
            (
                NOW - timedelta(minutes=59),
                NOW - timedelta(minutes=58, seconds=50),
                NOW - timedelta(minutes=30),
                NOW - timedelta(seconds=5),
            )
        ),
        snapshot_at=NOW,
        bucket_seconds=10,
        stale_seconds=15,
    )
    one_hour = windows["1h"]
    assert one_hour.observations == 4
    assert one_hour.coverage_percent > 0
    assert one_hour.stale_free_coverage_percent < 5
    assert one_hour.max_continuous_gap_seconds == 1795


def test_market_closure_does_not_reduce_source_reliability_coverage() -> None:
    saturday = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    windows = _windowed_coverage(
        iter(()),
        snapshot_at=saturday,
        bucket_seconds=5,
        stale_seconds=15,
        market_asset=Asset.GOLD,
    )
    assert windows["1h"].observations == 0
    assert windows["1h"].coverage_percent == 100
    assert windows["1h"].stale_free_coverage_percent == 100
    assert windows["1h"].max_continuous_gap_seconds == 0


def test_quality_reports_gaps_duplicates_order_and_clock_skew_without_repairing() -> None:
    rows = [
        (NOW, NOW + timedelta(milliseconds=10), "one"),
        (NOW, NOW + timedelta(seconds=2), "one"),
        (NOW - timedelta(seconds=1), NOW + timedelta(seconds=3), "two"),
        (NOW + timedelta(seconds=10), NOW + timedelta(seconds=4), "three"),
    ]
    quality = _quality(rows)
    assert quality.observations == 4
    assert quality.duplicate_rate == 0.25
    assert quality.out_of_order_observations == 1
    assert quality.negative_latency_observations == 1
    assert quality.severe_clock_skew_observations == 1
    assert quality.gap_max_seconds == 1.99
    assert quality.gaps_over_15_seconds == 0
    assert quality.stale_duration_seconds == 0


def test_readiness_distinguishes_live_source_from_historical_feature_coverage() -> None:
    quality = _quality([(NOW, NOW + timedelta(milliseconds=10), "one")])
    assert (
        _readiness_status(
            quality=quality,
            live_ready=True,
            finalized=3,
            trainable=2,
            training_rows=10,
            historical_underlying_rows=4,
        )
        is ReadinessStatus.PARTIAL
    )


def test_readiness_market_closed_is_partial_without_hiding_historical_quality() -> None:
    healthy_history = _quality([(NOW, NOW + timedelta(milliseconds=10), "one")])
    assert (
        _readiness_status(
            quality=healthy_history,
            live_ready=False,
            finalized=3,
            trainable=2,
            training_rows=10,
            historical_underlying_rows=10,
            market_closed=True,
        )
        is ReadinessStatus.PARTIAL
    )
    gapped_history = _quality(
        [
            (NOW, NOW, "one"),
            (NOW + timedelta(seconds=61), NOW + timedelta(seconds=61), "two"),
        ]
    )
    assert (
        _readiness_status(
            quality=gapped_history,
            live_ready=False,
            finalized=3,
            trainable=2,
            training_rows=10,
            historical_underlying_rows=10,
            market_closed=True,
        )
        is ReadinessStatus.QUALITY_WARNING
    )


def test_live_readiness_requires_asof_fresh_source_and_receive_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "raw.sqlite3"
    with RecorderStore(path) as store:
        store.append_underlying(
            UnderlyingObservation(
                asset=Asset.GOLD,
                provider=UnderlyingProvider.PYTH_HERMES,
                symbol="Metal.XAU/USD",
                feed_id="a" * 64,
                price=Decimal("3388.1"),
                source_timestamp=NOW - timedelta(seconds=4),
                received_timestamp=NOW,
                confidence=None,
                provenance="official",
                freshness=FreshnessState.FRESH,
            )
        )
        store.append_underlying(
            UnderlyingObservation(
                asset=Asset.BNB,
                provider=UnderlyingProvider.PYTH_HERMES,
                symbol="Crypto.BNB/USD",
                feed_id="b" * 64,
                price=Decimal("800"),
                source_timestamp=NOW - timedelta(seconds=30),
                received_timestamp=NOW - timedelta(seconds=20),
                confidence=None,
                provenance="official",
                freshness=FreshnessState.STALE,
            )
        )
        ready = _live_source_ready_by_asset(
            store._connection,
            snapshot_at=NOW + timedelta(seconds=1),
            max_age_seconds=15,
        )

    assert ready[Asset.GOLD] is True
    assert ready[Asset.BNB] is False


def test_live_readiness_preserves_market_closed_state(tmp_path: Path) -> None:
    saturday = datetime(2026, 8, 22, 12, tzinfo=UTC)
    with RecorderStore(tmp_path / "closed-readiness.sqlite3") as store:
        store.append_underlying(
            UnderlyingObservation(
                asset=Asset.GOLD,
                provider=UnderlyingProvider.PYTH_HERMES,
                symbol="Metal.XAU/USD",
                feed_id="a" * 64,
                price=Decimal("3388.1"),
                source_timestamp=NOW.replace(hour=20, minute=59),
                received_timestamp=NOW.replace(hour=20, minute=59),
                confidence=None,
                provenance="official",
                freshness=FreshnessState.FRESH,
            )
        )
        states = _live_source_state_by_asset(
            store._connection,
            snapshot_at=saturday,
            max_age_seconds=15,
        )

    assert states[Asset.GOLD] is MarketDataState.MARKET_CLOSED


def test_worker_progress_report_is_missing_safe_and_secret_free(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    assert _worker_progress_report(path)["available"] is False
    path.write_text(
        '{"observed_at":"2026-08-22T00:00:00+00:00",'
        '"event_loop_lag_seconds":0.2,"worker_progress_age_seconds":{"coinbase":1.2},'
        '"stale_workers":[],"api_key":"must-not-escape"}',
        encoding="utf-8",
    )
    report = _worker_progress_report(path)
    assert report["available"] is True
    assert report["worker_progress_age_seconds"] == {"coinbase": 1.2}
    assert "api_key" not in report
