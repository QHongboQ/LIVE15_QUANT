from datetime import UTC, datetime, timedelta

import pytest

from live15_quant.sequence_readiness import (
    DEFAULT_HORIZONS_SECONDS,
    DEFAULT_SEQUENCE_LENGTHS,
    SequenceEvent,
    SequenceObservation,
    SequenceReadinessStatus,
    build_sequence_samples,
    classify_sequence_readiness,
)


def _event() -> SequenceEvent:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return SequenceEvent("event-1", "BTC", start, start + timedelta(minutes=15))


def _observations(count: int = 12) -> tuple[SequenceObservation, ...]:
    start = _event().open_time
    return tuple(
        SequenceObservation(
            event_id="event-1",
            timestamp=start + timedelta(minutes=index),
            source_timestamp=start + timedelta(minutes=index),
            received_timestamp=start + timedelta(minutes=index),
            value=float(index + 1),
            resolution="1m_candle",
        )
        for index in range(count)
    )


def test_contract_constants_are_frozen() -> None:
    assert DEFAULT_SEQUENCE_LENGTHS == (3, 5, 8, 10)
    assert DEFAULT_HORIZONS_SECONDS == (30, 60, 120, 180, 300)


def test_sequence_ids_are_deterministic_and_event_local() -> None:
    first = build_sequence_samples((_event(),), _observations(), horizons=(60,), lengths=(3,))
    second = build_sequence_samples((_event(),), _observations(), horizons=(60,), lengths=(3,))
    assert [row.sample_id for row in first.samples] == [row.sample_id for row in second.samples]
    assert all(row.event_id == "event-1" for row in first.samples)


def test_as_of_and_completed_observation_guards() -> None:
    observations = list(_observations())
    observations[3] = SequenceObservation(
        event_id="event-1",
        timestamp=observations[3].timestamp,
        source_timestamp=observations[3].timestamp + timedelta(seconds=1),
        received_timestamp=observations[3].received_timestamp,
        value=4.0,
        resolution="1m_candle",
    )
    result = build_sequence_samples((_event(),), tuple(observations), horizons=(60,), lengths=(3,))
    assert any(item.reason == "SOURCE_AFTER_DECISION" for item in result.excluded)
    assert all(row.decision_timestamp != observations[3].timestamp for row in result.samples)
    assert any(item.reason == "SOURCE_AFTER_DECISION" for item in result.excluded)


def test_no_future_nearest_or_cross_event_sequence() -> None:
    event = _event()
    other = SequenceEvent("event-2", "ETH", event.open_time, event.close_time)
    observations = _observations(4)
    result = build_sequence_samples((event, other), observations, horizons=(30,), lengths=(3,))
    assert all(row.event_id == "event-1" for row in result.samples)
    assert any(item.reason == "TARGET_NOT_EXACT" for item in result.excluded)


def test_readiness_statuses_keep_snapshot_and_delta_separate() -> None:
    partial = classify_sequence_readiness(
        independent_utc_days=90,
        independent_events=59056,
        sequence_count=100,
        candle_sequence_count=100,
        trade_sequence_count=0,
        snapshot_count=0,
        delta_count=0,
        holdout_accessed=False,
    )
    assert partial.sequence_status is SequenceReadinessStatus.PARTIAL
    assert partial.microstructure_snapshot_status == "MICROSTRUCTURE_SNAPSHOT_NOT_MATERIALIZED"
    assert partial.microstructure_delta_status == "MICROSTRUCTURE_DELTA_BLOCKED"


def test_holdout_access_is_always_blocked() -> None:
    with pytest.raises(ValueError, match="holdout"):
        classify_sequence_readiness(
            independent_utc_days=90,
            independent_events=59056,
            sequence_count=1,
            candle_sequence_count=1,
            trade_sequence_count=0,
            snapshot_count=0,
            delta_count=0,
            holdout_accessed=True,
        )
