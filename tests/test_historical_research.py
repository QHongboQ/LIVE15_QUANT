from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from live15_quant.historical_research import (
    HistoricalLeakageError,
    HistoricalSample,
    HistoricalSource,
    HistoricalTier,
    WalkForwardConfig,
    build_manifest,
    build_walk_forward_folds,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def source() -> HistoricalSource:
    return HistoricalSource(
        source_id="local-recorder",
        tier=HistoricalTier.H0,
        data_type="recorder_trainable_rows",
        earliest=BASE,
        latest=BASE + timedelta(days=10),
        frequency="event-decision",
        as_of_quality="source_and_receive_asof",
        intended_use="path_and_regime_research",
        limitations=("not fresh validation",),
        row_count=10,
        event_count=5,
        assets=("BTC",),
    )


def sample(event: str, day: int, *, decision_offset: int = 60) -> HistoricalSample:
    start = BASE + timedelta(days=day)
    decision = start + timedelta(seconds=decision_offset)
    return HistoricalSample(
        sample_id=f"{event}-{day}",
        event_id=event,
        asset="BTC",
        source_id="local-recorder",
        provenance_tier=HistoricalTier.H0,
        window_start=start,
        window_end=start + timedelta(minutes=15),
        decision_timestamp=decision,
        source_timestamp=decision - timedelta(seconds=1),
        received_timestamp=decision - timedelta(milliseconds=1),
        target_timestamp=decision + timedelta(seconds=30),
        feature_names=("return_30s",),
    )


def test_historical_manifest_identity_is_deterministic() -> None:
    samples = tuple(sample(f"event-{day}", day) for day in range(5))
    first = build_manifest(
        sources=(source(),),
        samples=samples,
        code_sha="abc123",
        config={"window": "recent-14d", "purge_seconds": 600},
    )
    second = build_manifest(
        sources=(source(),),
        samples=samples,
        code_sha="abc123",
        config={"purge_seconds": 600, "window": "recent-14d"},
    )

    assert first.dataset_id == second.dataset_id
    assert first.build_hash == second.build_hash
    assert first.dataset_id.startswith("historical-research-")
    assert first.dataset_v2_touched is False
    assert first.holdout_accessed is False


def test_historical_sample_rejects_future_source_or_received_timestamp() -> None:
    with pytest.raises(HistoricalLeakageError, match="source_timestamp"):
        HistoricalSample(
            sample_id="bad-source",
            event_id="event",
            asset="BTC",
            source_id="local-recorder",
            provenance_tier=HistoricalTier.H0,
            window_start=BASE,
            window_end=BASE + timedelta(minutes=15),
            decision_timestamp=BASE + timedelta(seconds=10),
            source_timestamp=BASE + timedelta(seconds=11),
            received_timestamp=BASE + timedelta(seconds=1),
            feature_names=("return_30s",),
        )


def test_walk_forward_is_chronological_whole_event_and_purged() -> None:
    late = sample("event-2b", 2)
    late_start = BASE + timedelta(days=2, hours=23, minutes=55)
    late = replace(
        late,
        window_start=late_start,
        window_end=late_start + timedelta(minutes=15),
        decision_timestamp=late_start + timedelta(seconds=60),
        source_timestamp=late_start + timedelta(seconds=59),
        received_timestamp=late_start + timedelta(seconds=59),
        target_timestamp=late_start + timedelta(seconds=90),
    )
    samples = (*tuple(sample(f"event-{day}", day) for day in range(8)), late)
    folds = build_walk_forward_folds(
        samples,
        WalkForwardConfig(train_days=3, validation_days=2, step_days=1, purge_embargo_seconds=600),
    )

    assert len(folds) == 4
    first = folds[0]
    assert first.train_event_ids == ("event-0", "event-1", "event-2")
    assert first.validation_event_ids == ("event-3", "event-4")
    assert set(first.train_event_ids).isdisjoint(first.validation_event_ids)
    assert first.purged_event_ids == ("event-2b",)
    assert first.train_samples == 3
    assert first.validation_samples == 2


def test_missing_samples_are_excluded_with_reason_and_no_zero_fill() -> None:
    missing = HistoricalSample(
        sample_id="missing",
        event_id="event-missing",
        asset="BTC",
        source_id="local-recorder",
        provenance_tier=HistoricalTier.H0,
        window_start=BASE,
        window_end=BASE + timedelta(minutes=15),
        decision_timestamp=BASE + timedelta(seconds=60),
        source_timestamp=None,
        received_timestamp=None,
        available=False,
        missing_reason="source_unavailable",
    )

    assert missing.excluded is True
    assert missing.missing_reason == "source_unavailable"


def test_settlement_fields_are_not_accepted_as_features() -> None:
    with pytest.raises(HistoricalLeakageError, match="settlement"):
        sample_obj = sample("event", 0)
        HistoricalSample(
            sample_id=sample_obj.sample_id,
            event_id=sample_obj.event_id,
            asset=sample_obj.asset,
            source_id=sample_obj.source_id,
            provenance_tier=sample_obj.provenance_tier,
            window_start=sample_obj.window_start,
            window_end=sample_obj.window_end,
            decision_timestamp=sample_obj.decision_timestamp,
            source_timestamp=sample_obj.source_timestamp,
            received_timestamp=sample_obj.received_timestamp,
            target_timestamp=sample_obj.target_timestamp,
            feature_names=("settlement_result",),
        )
