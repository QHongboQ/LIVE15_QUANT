from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from live15_quant.canonical_evidence import (
    CoverageScope,
    EvidenceReconciliationError,
    EvidenceRecord,
    PreflightStatus,
    build_canonical_evidence_snapshot,
    evaluate_canonical_readiness,
    training_preflight,
    validate_sampling_policy,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
CUTOFF = START + timedelta(days=90)


def _record(
    source_id: str,
    tier: str,
    *,
    scope: CoverageScope = CoverageScope.FULL_SOURCE,
    days: int = 90,
    events: int = 100,
    path_days: int = 0,
    snapshot_days: int = 0,
    delta_days: int = 0,
    source_days: int | None = None,
    sampling_policy: str = "",
    capped: bool = False,
    holdout_accessed: bool = False,
) -> EvidenceRecord:
    return EvidenceRecord(
        source_id=source_id,
        provenance_tier=tier,
        coverage_scope=scope,
        earliest_timestamp=START,
        latest_timestamp=CUTOFF,
        independent_utc_days=days,
        independent_events=events,
        assets=("BTC", "ETH"),
        per_day_counts={START.date().isoformat(): events},
        per_asset_counts={"BTC": events // 2, "ETH": events - events // 2},
        row_count=events,
        artifact_id=f"artifact-{source_id}",
        cutoff=CUTOFF,
        sampling_policy=sampling_policy,
        capped=capped,
        cap_size=events if capped else None,
        full_source=scope is CoverageScope.FULL_SOURCE,
        data_quality_status="HEALTHY",
        gap_quarantine_state="NONE",
        sequence_availability={"available": path_days > 0, "days": path_days},
        microstructure_availability={
            "snapshot": {"available": snapshot_days > 0, "days": snapshot_days},
            "delta": {"available": delta_days > 0, "days": delta_days},
        },
        target_availability={"available": events > 0},
        source_independent_utc_days=source_days,
        holdout_accessed=holdout_accessed,
    )


def test_sampled_artifact_cannot_override_global_source_days() -> None:
    snapshot = build_canonical_evidence_snapshot(
        experiment_id="recon-test",
        experiment_cutoff=CUTOFF,
        records=(
            _record("h1-global", "H1_KALSHI_OFFICIAL_HISTORY", path_days=90),
            _record(
                "h1-sample",
                "H1_KALSHI_OFFICIAL_HISTORY",
                scope=CoverageScope.STRATIFIED_SAMPLE,
                days=1,
                events=350,
                path_days=1,
                source_days=90,
                sampling_policy="UTC day x asset x bounded events",
                capped=True,
            ),
        ),
    )
    assert snapshot.readiness_days()["h1_path_days"] == 90
    assert snapshot.readiness_days()["combined_path_days"] == 90


def test_first_n_temporal_sampling_is_rejected() -> None:
    with pytest.raises(EvidenceReconciliationError, match="first-N"):
        validate_sampling_policy("first N markets in API order")


def test_unexplained_temporal_collapse_requires_reconciliation() -> None:
    snapshot = build_canonical_evidence_snapshot(
        experiment_id="collapse-test",
        experiment_cutoff=CUTOFF,
        records=(
            _record(
                "h1-collapsed",
                "H1_KALSHI_OFFICIAL_HISTORY",
                scope=CoverageScope.SAMPLED_SUBSET,
                days=1,
                events=350,
                path_days=1,
                source_days=90,
                capped=True,
            ),
        ),
    )
    assert "EVIDENCE_RECONCILIATION_REQUIRED" in snapshot.inconsistency_states
    assert evaluate_canonical_readiness(snapshot).status is PreflightStatus.BLOCKED


def test_asset_and_event_collapse_are_explicit_inconsistencies() -> None:
    base = _record(
        "h1-collapse",
        "H1_KALSHI_OFFICIAL_HISTORY",
        scope=CoverageScope.SAMPLED_SUBSET,
        days=1,
        events=10,
        source_days=90,
        capped=True,
    )
    record = replace(
        base,
        source_independent_events=1000,
        source_assets=tuple(f"A{index}" for index in range(10)),
    )
    snapshot = build_canonical_evidence_snapshot(
        experiment_id="asset-collapse-test", experiment_cutoff=CUTOFF, records=(record,)
    )
    assert "ASSET_COVERAGE_COLLAPSE" in snapshot.inconsistency_states
    assert "SOURCE_ARTIFACT_COUNT_MISMATCH" in snapshot.inconsistency_states


def test_h0_h1_h2_are_reported_separately_and_h0_is_priority() -> None:
    snapshot = build_canonical_evidence_snapshot(
        experiment_id="tier-test",
        experiment_cutoff=CUTOFF,
        records=(
            _record("h0", "H0_LIVE_NATIVE", days=6, path_days=2, snapshot_days=5, delta_days=5),
            _record("h1", "H1_KALSHI_OFFICIAL_HISTORY", days=90, path_days=7),
            _record("h2", "H2_DEPTHFEED_RECORDED_L2", days=0, snapshot_days=0),
        ),
    )
    days = snapshot.readiness_days()
    assert days["h0_path_days"] == 2
    assert days["h1_path_days"] == 7
    assert days["h2_path_days"] == 0
    assert days["h0_snapshot_days"] == 5
    assert snapshot.h0_priority_source == "H0_LIVE_NATIVE"


def test_capability_days_do_not_inherit_general_coverage_days() -> None:
    h0 = replace(
        _record("h0", "H0_LIVE_NATIVE", days=6, path_days=0),
        coverage_days=tuple(f"2026-01-{index:02d}" for index in range(1, 7)),
    )
    h1 = replace(
        _record("h1", "H1_KALSHI_OFFICIAL_HISTORY", days=7, path_days=7),
        coverage_days=tuple(f"2026-02-{index:02d}" for index in range(1, 8)),
    )
    snapshot = build_canonical_evidence_snapshot(
        experiment_id="capability-day-isolation",
        experiment_cutoff=CUTOFF,
        records=(h0, h1),
    )
    assert snapshot.readiness_days()["h0_path_days"] == 0
    result = training_preflight(snapshot, model_family="path_expert")
    assert result.status is PreflightStatus.PARTIAL
    assert result.reasons == ("H0_PRIORITY_VALIDATION_REQUIRED",)


def test_snapshot_is_deterministic_and_cutoff_is_enforced() -> None:
    records = (_record("h0", "H0_LIVE_NATIVE", days=6),)
    first = build_canonical_evidence_snapshot(
        experiment_id="stable", experiment_cutoff=CUTOFF, records=records
    )
    second = build_canonical_evidence_snapshot(
        experiment_id="stable", experiment_cutoff=CUTOFF, records=records
    )
    assert first.snapshot_id == second.snapshot_id
    with pytest.raises(ValueError, match="cutoff"):
        build_canonical_evidence_snapshot(
            experiment_id="late", experiment_cutoff=START, records=records
        )


def test_holdout_access_blocks_training_preflight() -> None:
    snapshot = build_canonical_evidence_snapshot(
        experiment_id="holdout-test",
        experiment_cutoff=CUTOFF,
        records=(_record("dataset-v2", "FROZEN_DATASET", holdout_accessed=True),),
    )
    result = training_preflight(snapshot, model_family="path_expert")
    assert result.status is PreflightStatus.BLOCKED
    assert "HOLDOUT" in " ".join(result.reasons)


def test_training_preflight_returns_provenance_aware_contract() -> None:
    snapshot = build_canonical_evidence_snapshot(
        experiment_id="train-test",
        experiment_cutoff=CUTOFF,
        records=(
            _record("h0", "H0_LIVE_NATIVE", days=6, path_days=2),
            _record("h1", "H1_KALSHI_OFFICIAL_HISTORY", days=90, path_days=7),
        ),
    )
    result = training_preflight(snapshot, model_family="path_expert")
    assert result.status is PreflightStatus.READY
    assert {item.provenance_tier for item in result.training_sources} == {
        "H0_LIVE_NATIVE",
        "H1_KALSHI_OFFICIAL_HISTORY",
    }
    with pytest.raises(TypeError, match="CanonicalEvidenceSnapshot"):
        training_preflight({"h0_path_days": 2}, model_family="path_expert")  # type: ignore[arg-type]
