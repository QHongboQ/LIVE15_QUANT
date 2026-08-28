from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from live15_quant.h2_l2_materialization import (
    H2_DELTA_SEQUENCE_UNAVAILABLE,
    H2_OVERLAP_FAILED,
    H2_OVERLAP_PARTIAL,
    H2_OVERLAP_VALIDATED,
    H0SnapshotReference,
    H2L2MaterializationError,
    H2SnapshotEvidence,
    L2EventWindow,
    build_snapshot_sequences,
    canonical_microstructure_availability,
    evaluate_h2_overlap,
    materialize_snapshot,
    summarize_h2_capabilities,
)
from live15_quant.historical_providers import (
    DEPTHFEED_KALSHI_L2,
    HistoricalL2Snapshot,
    ProviderProvenance,
    SnapshotLevel,
)

START = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
END = START + timedelta(minutes=15)
CUTOFF = END + timedelta(days=1)
SOURCE_HASH = "a" * 64


def _evidence(
    *,
    seconds: int = 30,
    event_id: str = "event-a",
    yes: tuple[SnapshotLevel, ...] | None = None,
    no: tuple[SnapshotLevel, ...] | None = None,
    gap_state: str = "NO_GAP",
    source_timestamp: datetime | None = None,
) -> H2SnapshotEvidence:
    decision = START + timedelta(seconds=seconds)
    snapshot = HistoricalL2Snapshot(
        ticker="KXBTC15M-TEST",
        series="KXBTC15M",
        base_asset="BTC",
        market_type="binary",
        received_timestamp=decision,
        yes=yes
        if yes is not None
        else (
            SnapshotLevel(Decimal("0.45"), Decimal("12")),
            SnapshotLevel(Decimal("0.40"), Decimal("8")),
        ),
        no=no
        if no is not None
        else (
            SnapshotLevel(Decimal("0.50"), Decimal("10")),
            SnapshotLevel(Decimal("0.45"), Decimal("6")),
        ),
        provider=ProviderProvenance(
            DEPTHFEED_KALSHI_L2,
            "H2_DEPTHFEED_RECORDED_L2",
            "historical_snapshots",
            decision,
        ),
    )
    return H2SnapshotEvidence(
        snapshot=snapshot,
        event_window=L2EventWindow(event_id, "KXBTC15M-TEST", START, END),
        source_timestamp=source_timestamp or decision,
        decision_timestamp=decision,
        source_artifact_hash=SOURCE_HASH,
        gap_state=gap_state,
        evidence_origin="SYNTHETIC_TEST_FIXTURE",
        experiment_cutoff=CUTOFF,
    )


def test_materializer_rejects_future_source_or_receive_timestamp() -> None:
    with pytest.raises(H2L2MaterializationError, match="SOURCE_AFTER_DECISION"):
        materialize_snapshot(_evidence(source_timestamp=START + timedelta(seconds=31)))


def test_materializer_is_deterministic_and_preserves_missing_levels() -> None:
    first = materialize_snapshot(_evidence())
    reordered = materialize_snapshot(
        _evidence(
            yes=(
                SnapshotLevel(Decimal("0.40"), Decimal("8")),
                SnapshotLevel(Decimal("0.45"), Decimal("12")),
            ),
            no=(),
        )
    )
    assert first.example_id == materialize_snapshot(_evidence()).example_id
    assert reordered.no_levels == ()
    assert reordered.features.yes_implied_ask is None
    assert reordered.features.yes_spread is None
    assert reordered.features.yes_best_bid == Decimal("0.45")


def test_sequence_builder_rejects_cross_event_and_gapped_rows_without_fill() -> None:
    first = materialize_snapshot(_evidence(seconds=30))
    second_event = materialize_snapshot(_evidence(seconds=60, event_id="event-b"))
    with pytest.raises(H2L2MaterializationError, match="CROSS_EVENT_SEQUENCE_FORBIDDEN"):
        build_snapshot_sequences((first, second_event), lookback=2, excluded_event_ids=())

    gapped = materialize_snapshot(_evidence(seconds=60, gap_state="GAP_DETECTED"))
    result = build_snapshot_sequences((first, gapped), lookback=2, excluded_event_ids=())
    assert result.sequences == ()
    assert result.exclusions[0].reason == "GAP_REJECTED"


def test_overlap_validation_is_explicit_and_h2_never_wins_conflict() -> None:
    h2 = materialize_snapshot(_evidence())
    matching_h0 = H0SnapshotReference(
        "live15_recorder_h0",
        "H0_LIVE_NATIVE",
        h2.ticker,
        h2.event_id,
        h2.decision_timestamp,
        h2.yes_levels,
        h2.no_levels,
        "b" * 64,
    )
    matching = evaluate_h2_overlap((h2,), (matching_h0,))
    assert matching.status == H2_OVERLAP_VALIDATED

    duplicate_h0 = evaluate_h2_overlap((h2,), (matching_h0, matching_h0))
    assert duplicate_h0.status == H2_OVERLAP_FAILED
    assert duplicate_h0.reasons == ("H0_DUPLICATE_OR_CONFLICT_QUARANTINED",)

    conflicting = materialize_snapshot(
        _evidence(yes=(SnapshotLevel(Decimal("0.44"), Decimal("12")),))
    )
    conflicting_h0 = H0SnapshotReference(
        "live15_recorder_h0",
        "H0_LIVE_NATIVE",
        conflicting.ticker,
        conflicting.event_id,
        conflicting.decision_timestamp,
        conflicting.yes_levels,
        conflicting.no_levels,
        "c" * 64,
    )
    failed = evaluate_h2_overlap((h2,), (conflicting_h0,))
    assert failed.status == H2_OVERLAP_FAILED
    assert failed.conflicts == (h2.example_id,)

    partial = evaluate_h2_overlap((h2,), ())
    assert partial.status == H2_OVERLAP_PARTIAL


def test_synthetic_snapshots_prove_code_pipeline_but_not_real_h2_readiness() -> None:
    example = materialize_snapshot(_evidence())
    result = build_snapshot_sequences((example,), lookback=1, excluded_event_ids=())
    summary = summarize_h2_capabilities((example,), result, overlap_result=None)
    assert summary["code_pipeline_status"] == "CODE_PIPELINE_READY"
    assert summary["real_h2_data_status"] == "REAL_H2_DATA_NOT_READY"
    assert summary["delta_sequence_status"] == H2_DELTA_SEQUENCE_UNAVAILABLE
    assert summary["snapshot_sequence_days"] == ()
    assert canonical_microstructure_availability(summary)["training_ready"]["available"] is False


def test_sequence_requires_holdout_exclusions_and_rejects_duplicate_snapshot() -> None:
    example = materialize_snapshot(_evidence())
    with pytest.raises(H2L2MaterializationError, match="HOLDOUT_IDENTITY_EXCLUSIONS_REQUIRED"):
        build_snapshot_sequences((example,), lookback=1)
    duplicate = build_snapshot_sequences((example, example), lookback=1, excluded_event_ids=())
    assert duplicate.sequences == ()
    assert duplicate.exclusions[0].reason == "DUPLICATE_SNAPSHOT_REJECTED"
