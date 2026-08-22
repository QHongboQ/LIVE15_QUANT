from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import live15_quant.storage as storage_module
from live15_quant.dataset import (
    ASOF_QUERY_VERSION,
    DATASET_VERSION,
    DatasetBuildConfig,
    DatasetBuilder,
    DatasetMode,
    FeatureStore,
    FeatureStoreError,
    dataset_diagnostics,
)
from live15_quant.features import FeatureEngine, FeatureInputs, SamplingPolicy
from live15_quant.kalshi_lifecycle import KalshiLifecycle
from live15_quant.models import (
    Asset,
    FreshnessState,
    MarketTick,
    UnderlyingObservation,
    UnderlyingProvider,
)
from live15_quant.normalization import (
    NormalizationPolicy,
    NormalizationScope,
    fit_normalization,
)
from live15_quant.records import SCHEMA_VERSION
from live15_quant.splits import (
    WalkForwardMode,
    WalkForwardPolicy,
    chronological_split,
    walk_forward_splits,
)
from live15_quant.storage import (
    ActiveRecorderAnalysisError,
    RecorderStorageError,
    RecorderStore,
)
from tests.test_kalshi_lifecycle import provider, quote, raw_market

BASE = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


def sampling() -> SamplingPolicy:
    return SamplingPolicy(
        (timedelta(minutes=10), timedelta(minutes=5)),
        quote_max_age=timedelta(seconds=30),
        underlying_max_age=timedelta(seconds=30),
    )


def add_event(store: RecorderStore, start: datetime, *, result: str) -> str:
    finalized = provider().parse_market(
        Asset.BTC,
        raw_market(Asset.BTC, start=start, status="finalized", result=result),
        start + timedelta(minutes=16),
    )
    assert finalized.settlement is not None
    observed = replace(
        finalized,
        lifecycle=KalshiLifecycle.OPEN,
        official_status="active",
        fetched_timestamp=start + timedelta(seconds=1),
        determination_result=None,
        settlement=None,
    )
    store.append_kalshi_market(observed)
    for index, decision in enumerate((start + timedelta(minutes=5), start + timedelta(minutes=10))):
        store.append_kalshi_quote(
            replace(
                quote(finalized.ticker, finalized.event_ticker, decision - timedelta(seconds=1)),
                yes_bid=Decimal("0.40") + Decimal(index) / Decimal(10),
                yes_ask=Decimal("0.42") + Decimal(index) / Decimal(10),
            )
        )
    for index, timestamp in enumerate(
        start - timedelta(minutes=5) + timedelta(seconds=15 * value) for value in range(61)
    ):
        price = Decimal("68100.00000001") + Decimal(index) / Decimal(100)
        store.append_coinbase(
            MarketTick(
                symbol="BTC-USD",
                price=price,
                bid=price - Decimal("0.01"),
                ask=price + Decimal("0.01"),
                received_at=timestamp,
                exchange_time=timestamp,
            )
        )
    store.append_kalshi_settlement(finalized.settlement)
    return finalized.ticker


def test_dataset_build_is_restartable_reproducible_and_decimal_safe(tmp_path) -> None:
    raw_path = tmp_path / "raw.sqlite3"
    feature_path = tmp_path / "features.sqlite3"
    with RecorderStore(raw_path) as source:
        ticker = add_event(source, BASE, result="yes")
        with FeatureStore(feature_path) as destination:
            builder = DatasetBuilder(source, destination)
            partial = builder.build(DatasetBuildConfig(sampling()), max_new_rows=1)
            assert not partial.complete
            assert partial.rows == 1
    with RecorderStore(raw_path) as source, FeatureStore(feature_path) as destination:
        resumed = DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))
        replayed = destination.replay(resumed.build_id)
        repeated = DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))

        assert resumed.complete
        assert resumed.rows == 2
        assert resumed.rows_written == 1
        assert repeated.build_id == resumed.build_id
        assert repeated.rows_written == 0
        assert destination.count_rows(resumed.build_id) == 2
        assert destination.integrity_check() == "ok"
        assert {row.ticker for row in replayed} == {ticker}
        assert replayed[0].target == Decimal("68159.82000001")
        assert replayed[0].features.by_name()["underlying_price"].value == Decimal("68100.40000001")
        assert max(len(row.source_tick_row_ids) for row in replayed) <= 23


def test_dataset_builder_batches_commits_and_rolls_back_incomplete_batch(
    tmp_path, monkeypatch
) -> None:
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        add_event(source, BASE, result="yes")
        original = destination.append
        calls = 0

        def fail_second(row, *, commit=True):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected build failure")
            return original(row, commit=commit)

        monkeypatch.setattr(destination, "append", fail_second)
        with pytest.raises(RuntimeError, match="injected build failure"):
            DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))

        persisted = destination._connection.execute(
            "SELECT COUNT(*) FROM training_examples"
        ).fetchone()[0]
        assert persisted == 0


def test_dataset_builder_resumes_after_crash_following_committed_batch(
    tmp_path, monkeypatch
) -> None:
    """A crash preserves completed 256-row batches and retries only the tail."""

    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        for index in range(129):
            add_event(
                source,
                BASE + timedelta(minutes=15 * index),
                result=("yes" if index % 2 else "no"),
            )
        original = destination.append
        calls = 0

        def fail_after_committed_batch(row, *, commit=True):
            nonlocal calls
            calls += 1
            if calls == 257:
                raise RuntimeError("injected post-batch crash")
            return original(row, commit=commit)

        monkeypatch.setattr(destination, "append", fail_after_committed_batch)
        builder = DatasetBuilder(source, destination)
        with pytest.raises(RuntimeError, match="post-batch crash"):
            builder.build(DatasetBuildConfig(sampling()))

        persisted_after_crash = destination._connection.execute(
            "SELECT COUNT(*) FROM training_examples"
        ).fetchone()[0]
        assert persisted_after_crash == 256

        monkeypatch.setattr(destination, "append", original)
        resumed = builder.build(DatasetBuildConfig(sampling()))
        repeated = builder.build(DatasetBuildConfig(sampling()))

        assert resumed.complete
        assert resumed.rows == 258
        assert resumed.rows_written == 2
        assert repeated.rows_written == 0
        assert destination.count_rows(resumed.build_id) == 258
        assert destination.integrity_check() == "ok"


def test_multiple_decisions_pooled_and_per_asset_builds(tmp_path) -> None:
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        add_event(source, BASE, result="no")
        pooled = DatasetBuilder(source, destination).build(
            DatasetBuildConfig(sampling(), DatasetMode.POOLED)
        )
        per_asset = DatasetBuilder(source, destination).build(
            DatasetBuildConfig(sampling(), DatasetMode.PER_ASSET, (Asset.BTC,))
        )

        assert pooled.rows == per_asset.rows == 2
        assert pooled.events == per_asset.events == 1
        assert pooled.build_id != per_asset.build_id
        assert pooled.diagnostics["label_balance"] == {"yes": 0, "no": 2}
        assert pooled.diagnostics["rows_per_decision_bucket_seconds"] == {
            "300": 1,
            "600": 1,
        }
        assert destination.coverage_by_asset(pooled.build_id)[Asset.BTC] == (1, 2)
        assert destination.coverage_by_asset(pooled.build_id)[Asset.ETH] == (0, 0)
        assert source.settlement_counts_by_asset()[Asset.BTC] == 1
        settlement_snapshot = source.training_source_snapshot()["kalshi_settlements"]
        assert isinstance(settlement_snapshot, dict)
        assert settlement_snapshot["counts_by_asset"]["BTC"] == 1


def test_current_schema_missing_summary_requires_explicit_offline_repair(tmp_path) -> None:
    """Normal startup fails boundedly instead of backfilling with a full-table GROUP BY."""

    path = tmp_path / "draft-v8.sqlite3"
    with RecorderStore(path) as source:
        add_event(source, BASE, result="yes")
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE kalshi_settlement_counts")
    connection.commit()
    connection.close()

    with pytest.raises(RecorderStorageError, match="offline repair"):
        RecorderStore(path)

    connection = sqlite3.connect(path)
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kalshi_settlement_counts'"
        ).fetchone()
        is None
    )
    connection.close()


def test_dataset_builder_uses_pyth_underlying_for_non_coinbase_asset(tmp_path) -> None:
    start = BASE
    finalized = provider().parse_market(
        Asset.GOLD,
        raw_market(Asset.GOLD, start=start, status="finalized", result="yes"),
        start + timedelta(minutes=16),
    )
    assert finalized.settlement is not None
    observed = replace(
        finalized,
        lifecycle=KalshiLifecycle.OPEN,
        official_status="active",
        fetched_timestamp=start + timedelta(seconds=1),
        determination_result=None,
        settlement=None,
    )
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        source.append_kalshi_market(observed)
        decision = start + timedelta(minutes=5)
        source.append_kalshi_quote(
            replace(
                quote(
                    "KXBTC15M-26AUG201000-00",
                    "KXBTC15M-26AUG201000",
                    decision - timedelta(seconds=1),
                ),
                asset=Asset.GOLD,
                series=finalized.series,
                ticker=finalized.ticker,
                event_ticker=finalized.event_ticker,
            )
        )
        for index in range(22):
            timestamp = decision - timedelta(seconds=315 - index * 15)
            source.append_underlying(
                UnderlyingObservation(
                    asset=Asset.GOLD,
                    provider=UnderlyingProvider.PYTH_HERMES,
                    symbol="Metal.XAU/USD",
                    feed_id="a" * 64,
                    price=Decimal("3388") + Decimal(index) / Decimal(100),
                    source_timestamp=timestamp,
                    received_timestamp=timestamp,
                    confidence=Decimal("0.01"),
                    provenance="official",
                    freshness=FreshnessState.FRESH,
                )
            )
        source.append_kalshi_settlement(finalized.settlement)
        config = DatasetBuildConfig(
            SamplingPolicy(
                (timedelta(minutes=10),),
                quote_max_age=timedelta(seconds=30),
                underlying_max_age=timedelta(seconds=30),
            ),
            assets=(Asset.GOLD,),
        )
        summary = DatasetBuilder(source, destination).build(config)
        row = destination.replay(summary.build_id)[0]

    assert row.features.by_name()["underlying_price"].value == Decimal("3388.21")
    assert row.source_tick_row_ids == ()
    assert len(row.source_underlying_row_ids) == 22


def test_dataset_builder_quarantines_closed_market_underlying_at_row_level(tmp_path) -> None:
    start = datetime(2026, 8, 22, 4, 0, tzinfo=UTC)  # Saturday metals closure.
    finalized = provider().parse_market(
        Asset.GOLD,
        raw_market(Asset.GOLD, start=start, status="finalized", result="yes"),
        start + timedelta(minutes=16),
    )
    assert finalized.settlement is not None
    observed = replace(
        finalized,
        lifecycle=KalshiLifecycle.OPEN,
        official_status="active",
        fetched_timestamp=start + timedelta(seconds=1),
        determination_result=None,
        settlement=None,
    )
    decision = start + timedelta(minutes=5)
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        source.append_kalshi_market(observed)
        btc_ticker = finalized.ticker.replace(finalized.series, "KXBTC15M", 1)
        btc_event_ticker = finalized.event_ticker.replace(finalized.series, "KXBTC15M", 1)
        source.append_kalshi_quote(
            replace(
                quote(btc_ticker, btc_event_ticker, decision - timedelta(seconds=1)),
                asset=Asset.GOLD,
                series=finalized.series,
                ticker=finalized.ticker,
                event_ticker=finalized.event_ticker,
            )
        )
        source.append_underlying(
            UnderlyingObservation(
                asset=Asset.GOLD,
                provider=UnderlyingProvider.PYTH_HERMES,
                symbol="Metal.XAU/USD",
                feed_id="a" * 64,
                price=Decimal("3388.12345678"),
                source_timestamp=decision - timedelta(seconds=1),
                received_timestamp=decision - timedelta(seconds=1),
                confidence=Decimal("0.01"),
                provenance="official",
                freshness=FreshnessState.FRESH,
            )
        )
        source.append_kalshi_settlement(finalized.settlement)
        summary = DatasetBuilder(source, destination).build(
            DatasetBuildConfig(
                SamplingPolicy(
                    (timedelta(minutes=10),),
                    quote_max_age=timedelta(seconds=30),
                    underlying_max_age=timedelta(seconds=30),
                ),
                assets=(Asset.GOLD,),
            )
        )

    assert summary.rows == 0
    assert summary.skipped_decisions == 1
    assert summary.diagnostics["trainability_rejections"] == {"market_closed": 1}


def test_empty_dataset_diagnostics_report_rates_as_not_applicable() -> None:
    diagnostics = dataset_diagnostics(())

    assert diagnostics["events_count"] == 0
    assert diagnostics["rows_count"] == 0
    assert set(diagnostics["missing_feature_rates"].values()) == {None}
    assert set(diagnostics["stale_feature_rates"].values()) == {None}


def test_event_group_chronological_and_walk_forward_splits(tmp_path) -> None:
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        for index in range(5):
            add_event(
                source,
                BASE + timedelta(minutes=15 * index),
                result="yes" if index % 2 == 0 else "no",
            )
        summary = DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))
        rows = destination.replay(summary.build_id)

    split = chronological_split(rows, train_events=2, validation_events=1)
    expanding = walk_forward_splits(
        rows,
        WalkForwardPolicy(WalkForwardMode.EXPANDING, train_events=2, validation_events=1),
    )
    rolling = walk_forward_splits(
        rows,
        WalkForwardPolicy(WalkForwardMode.ROLLING, train_events=2, validation_events=1),
    )

    assert tuple(map(len, (split.train, split.validation, split.test))) == (4, 2, 4)
    assert len(expanding) == len(rolling) == 3
    assert tuple(len(fold.train) for fold in expanding) == (4, 6, 8)
    assert tuple(len(fold.train) for fold in rolling) == (4, 4, 4)
    for fold in (*expanding, *rolling):
        assert {row.ticker for row in fold.train}.isdisjoint(row.ticker for row in fold.validation)


def test_dataset_version_changes_are_part_of_reproducible_manifest(tmp_path) -> None:
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        add_event(source, BASE, result="yes")
        first = DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))
        first_snapshot = source.training_source_snapshot()
        persisted_first_snapshot = destination.build_source_snapshot(first.build_id)
        source.append_coinbase(
            MarketTick(
                symbol="BTC-USD",
                price=Decimal("999.000000000000000001"),
                bid=Decimal("998.99"),
                ask=Decimal("999.01"),
                received_at=BASE + timedelta(minutes=14),
                exchange_time=BASE + timedelta(minutes=14),
            )
        )
        second_snapshot = source.training_source_snapshot()
        second = DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))

    assert DATASET_VERSION == "1.2.0"
    assert first_snapshot["recorder_schema_version"] == SCHEMA_VERSION
    assert all(persisted_first_snapshot[key] == value for key, value in first_snapshot.items())
    assert persisted_first_snapshot["dataset_query_metadata"]["version"] == ASOF_QUERY_VERSION
    assert "content_sha256" in persisted_first_snapshot["data_gaps"]
    assert first_snapshot != second_snapshot
    assert len(first.build_id) == 64
    assert second.build_id != first.build_id


def test_captured_source_boundaries_exclude_rows_appended_during_build(
    tmp_path, monkeypatch
) -> None:
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        ticker = add_event(source, BASE, result="yes")
        original_snapshot = source.training_source_snapshot

        def snapshot_then_append() -> dict[str, object]:
            snapshot = original_snapshot()
            decision = BASE + timedelta(minutes=10)
            source.append_kalshi_quote(
                replace(
                    quote(ticker, ticker.rsplit("-", 1)[0], decision),
                    yes_bid=Decimal("0.99"),
                    yes_ask=Decimal("1.00"),
                )
            )
            source.append_coinbase(
                MarketTick(
                    symbol="BTC-USD",
                    price=Decimal("999999.00000001"),
                    bid=Decimal("999998.99"),
                    ask=Decimal("999999.01"),
                    received_at=decision,
                    exchange_time=decision,
                )
            )
            return snapshot

        monkeypatch.setattr(source, "training_source_snapshot", snapshot_then_append)
        summary = DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))
        row = {item.decision_timestamp: item for item in destination.replay(summary.build_id)}[
            BASE + timedelta(minutes=10)
        ]

    assert row.features.by_name()["yes_bid"].value == Decimal("0.50")
    assert row.features.by_name()["underlying_price"].value == Decimal("68100.60000001")


def test_future_target_revision_is_rejected_before_dataset_construction(tmp_path) -> None:
    finalized = provider().parse_market(
        Asset.BTC,
        raw_market(Asset.BTC, start=BASE, status="finalized", result="yes"),
        BASE + timedelta(minutes=16),
    )
    observed = replace(
        finalized,
        lifecycle=KalshiLifecycle.OPEN,
        official_status="active",
        fetched_timestamp=BASE + timedelta(seconds=1),
        determination_result=None,
        settlement=None,
    )
    revised = replace(
        observed,
        target=observed.target + Decimal("1"),
        fetched_timestamp=BASE + timedelta(minutes=10, seconds=1),
    )
    with RecorderStore(tmp_path / "raw.sqlite3") as source:
        source.append_kalshi_market(observed)
        with pytest.raises(RecorderStorageError, match="conflicting official market metadata"):
            source.append_kalshi_market(revised)


def test_feature_store_refuses_raw_database_role(tmp_path) -> None:
    path = tmp_path / "raw.sqlite3"
    with RecorderStore(path):
        pass

    with pytest.raises(FeatureStoreError, match="cannot share"):
        FeatureStore(path)


def test_raw_store_refuses_feature_store_database_role(tmp_path) -> None:
    path = tmp_path / "features.sqlite3"
    with FeatureStore(path):
        pass

    with pytest.raises(RecorderStorageError, match="cannot share"):
        RecorderStore(path)


def test_per_asset_mode_requires_exactly_one_asset() -> None:
    assert set(DatasetBuildConfig(sampling()).assets) == set(Asset)
    with pytest.raises(ValueError, match="exactly one asset"):
        DatasetBuildConfig(sampling(), DatasetMode.PER_ASSET, (Asset.BTC, Asset.ETH))


def test_global_and_per_asset_normalization_interfaces(tmp_path) -> None:
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        add_event(source, BASE, result="yes")
        summary = DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))
        rows = destination.replay(summary.build_id)

    global_profile = fit_normalization(
        rows,
        NormalizationPolicy(NormalizationScope.GLOBAL, ("underlying_price", "last_trade")),
    )
    asset_profile = fit_normalization(
        rows,
        NormalizationPolicy(NormalizationScope.PER_ASSET, ("underlying_price",)),
    )

    assert {item.group for item in global_profile.statistics} == {"global"}
    assert {item.group for item in asset_profile.statistics} == {"BTC"}
    assert global_profile.transform(rows[0])["underlying_price"] is not None
    assert asset_profile.transform(rows[0])["underlying_price"] is not None


def test_builder_skips_only_expected_missing_decision_time_metadata(tmp_path) -> None:
    finalized = provider().parse_market(
        Asset.BTC,
        raw_market(Asset.BTC, start=BASE, status="finalized", result="yes"),
        BASE + timedelta(minutes=16),
    )
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        source.append_kalshi_market(finalized)
        summary = DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))

    assert summary.complete
    assert summary.rows == 0
    assert summary.skipped_decisions == 2
    assert summary.diagnostics["evaluated_finalized_events"] == 1
    assert summary.diagnostics["events_without_training_rows"] == 1
    assert summary.diagnostics["trainability_rejections"] == {"missing_decision_time_metadata": 2}

    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        repeated = DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))
    assert repeated.skipped_decisions == 2
    assert repeated.diagnostics == summary.diagnostics


def test_builder_fails_loudly_on_training_metadata_corruption(tmp_path) -> None:
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        ticker = add_event(source, BASE, result="yes")
        source._connection.execute(
            "UPDATE kalshi_market_lifecycle SET target='1.0' WHERE ticker=?", (ticker,)
        )
        source._connection.commit()

        with pytest.raises(RecorderStorageError, match="metadata does not match"):
            DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))


def test_feature_store_replay_detects_content_corruption(tmp_path) -> None:
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        add_event(source, BASE, result="yes")
        summary = DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))
        destination._connection.execute(
            "UPDATE training_examples SET target='1.0' WHERE build_id=?", (summary.build_id,)
        )
        destination._connection.commit()

        with pytest.raises(FeatureStoreError, match="content hash mismatch"):
            destination.replay(summary.build_id)


def test_offline_engine_matches_raw_join_build_and_feature_store_replay(tmp_path) -> None:
    with (
        RecorderStore(tmp_path / "raw.sqlite3") as source,
        FeatureStore(tmp_path / "features.sqlite3") as destination,
    ):
        ticker = add_event(source, BASE, result="yes")
        decision = BASE + timedelta(minutes=10)
        joined = source.join_training_label(ticker, decision)
        raw_ticks = tuple(
            source.replay_coinbase_range(
                "BTC-USD",
                start=decision - timedelta(minutes=5, seconds=30),
                end=decision,
            )
        )
        offline = FeatureEngine(sampling()).compute(
            FeatureInputs(joined.market, joined.observations, raw_ticks, decision)
        )

        summary = DatasetBuilder(source, destination).build(DatasetBuildConfig(sampling()))
        replayed = {row.decision_timestamp: row for row in destination.replay(summary.build_id)}[
            decision
        ]

    assert replayed.features == offline
    assert replayed.target == joined.market.target
    assert replayed.label == joined.label.result


def test_dataset_analysis_refuses_active_writer_database(tmp_path, monkeypatch) -> None:
    path = tmp_path / "raw.sqlite3"
    (tmp_path / "recorder.pid").write_text("12345\n", encoding="ascii")
    monkeypatch.setattr(storage_module, "process_alive", lambda pid: pid == 12345)
    with RecorderStore(path) as source:
        with pytest.raises(ActiveRecorderAnalysisError, match="read-only snapshot"):
            source.training_source_snapshot()
