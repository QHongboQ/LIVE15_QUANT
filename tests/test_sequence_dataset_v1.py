from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from live15_quant.models import Asset
from live15_quant.sequence_dataset_v1 import (
    SequenceDataset,
    SequenceDatasetConfig,
    SequenceDatasetError,
    SequenceDatasetV1Builder,
    SequenceEvidenceStatus,
    SequenceFrame,
    SequenceSnapshotCoverage,
)


def _frame(event: str, start: datetime, index: int) -> SequenceFrame:
    midpoint = Decimal("0.50") + Decimal(index) / Decimal("1000")
    return SequenceFrame(
        Asset.BTC,
        event,
        f"{event}-ticker",
        start + timedelta(seconds=index * 10),
        start,
        start + timedelta(minutes=15),
        midpoint - Decimal("0.01"),
        midpoint + Decimal("0.01"),
        Decimal("0.49"),
        Decimal("0.51"),
        Decimal("10"),
        Decimal("11"),
        Decimal("100000"),
    )


def _config() -> SequenceDatasetConfig:
    return SequenceDatasetConfig(
        lookback_frames=3,
        horizons=(timedelta(seconds=10), timedelta(seconds=30), timedelta(seconds=60)),
        target_tolerance=timedelta(0),
        min_events=3,
        min_examples=1,
        min_distinct_days=1,
    )


def _coverage() -> SequenceSnapshotCoverage:
    return SequenceSnapshotCoverage("snapshot", "a" * 64, Decimal("1"), Decimal("1"))


def test_sequence_builder_uses_only_past_frames_and_same_event_future_targets() -> None:
    start = datetime(2026, 8, 23, tzinfo=UTC)
    frames = [
        _frame(f"event-{event}", start + timedelta(minutes=20 * event), index)
        for event in range(3)
        for index in range(20)
    ]
    result = SequenceDatasetV1Builder(_config()).build(frames, coverage=_coverage())
    assert isinstance(result, SequenceDataset)
    for example in result.examples:
        assert (
            max(frame.received_timestamp for frame in example.frames) <= example.decision_timestamp
        )
        assert all(
            target.target_timestamp > example.decision_timestamp for target in example.targets
        )
        assert all(frame.event_id == example.event_id for frame in example.frames)
        assert all(
            target.target_timestamp < example.frames[-1].window_end for target in example.targets
        )


def test_sequence_split_is_grouped_and_strictly_chronological() -> None:
    start = datetime(2026, 8, 23, tzinfo=UTC)
    frames = [
        _frame(f"event-{event}", start + timedelta(minutes=20 * event), index)
        for event in range(6)
        for index in range(20)
    ]
    result = SequenceDatasetV1Builder(_config()).build(frames, coverage=_coverage())
    assert isinstance(result, SequenceDataset)
    events = {name: {item.event_id for item in rows} for name, rows in result.splits.items()}
    assert not (events["train"] & events["validation"])
    assert not (events["train"] & events["test"])
    assert not (events["validation"] & events["test"])
    assert max(item.decision_timestamp for item in result.splits["train"]) < min(
        item.decision_timestamp for item in result.splits["validation"]
    )
    assert (
        result.manifest(git_sha="a" * 40)["deterministic_build_hash"]
        == result.manifest(git_sha="a" * 40)["deterministic_build_hash"]
    )


def test_sequence_evidence_fails_closed_without_independent_history() -> None:
    result = SequenceDatasetV1Builder(_config()).build(
        [_frame("event", datetime(2026, 8, 23, tzinfo=UTC), index) for index in range(20)],
        coverage=_coverage(),
    )
    assert result.status is SequenceEvidenceStatus.INSUFFICIENT_SEQUENCE_EVIDENCE
    assert "insufficient_independent_events" in result.reasons


def test_sequence_evidence_requires_offline_coverage_proof() -> None:
    start = datetime(2026, 8, 23, tzinfo=UTC)
    frames = [
        _frame(f"event-{event}", start + timedelta(minutes=20 * event), index)
        for event in range(3)
        for index in range(20)
    ]
    coverage = SequenceSnapshotCoverage("snapshot", "a" * 64, Decimal("0.98"), Decimal("0.98"))
    result = SequenceDatasetV1Builder(_config()).build(frames, coverage=coverage)
    assert result.status is SequenceEvidenceStatus.INSUFFICIENT_SEQUENCE_EVIDENCE
    assert "insufficient_gap_free_coverage" in result.reasons


def test_sequence_frame_rejects_unsynchronized_or_invalid_book() -> None:
    start = datetime(2026, 8, 23, tzinfo=UTC)
    with pytest.raises(SequenceDatasetError, match="unsynchronized"):
        SequenceFrame(
            Asset.BTC,
            "event",
            "ticker",
            start,
            start,
            start + timedelta(minutes=15),
            Decimal("0.5"),
            Decimal("0.6"),
            Decimal("0.4"),
            Decimal("0.5"),
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
            synchronized=False,
        )
    with pytest.raises(SequenceDatasetError, match="positive YES midpoint"):
        SequenceFrame(
            Asset.BTC,
            "event",
            "ticker",
            start,
            start,
            start + timedelta(minutes=15),
            Decimal("0"),
            Decimal("0"),
            Decimal("0.4"),
            Decimal("0.5"),
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
        )


def test_sequence_snapshot_coverage_requires_real_manifest_hash() -> None:
    with pytest.raises(SequenceDatasetError, match="SHA-256"):
        SequenceSnapshotCoverage("snapshot", "not-a-hash", Decimal("1"), Decimal("1"))
