from __future__ import annotations

import json
from pathlib import Path

import pytest

from live15_quant.model_readiness import (
    DataReadinessEvidence,
    ReadinessStatus,
    evaluate_microstructure_readiness,
    evaluate_path_readiness,
    load_model_zoo_manifest,
)

ROOT = Path(__file__).parents[1]


def test_manifest_schema_is_machine_readable_and_has_required_families() -> None:
    manifest = load_model_zoo_manifest(ROOT / "docs" / "model_zoo_foundation.json")
    assert manifest["foundation_version"] == "1.0.0"
    families = {item["family_id"] for item in manifest["families"]}
    assert {"path_expert_foundation", "terminal_baseline", "microstructure_expert"} <= families
    for item in manifest["families"]:
        assert item["approved_data_sources"]
        assert "blocked_by" in item
        assert "role" in item


def test_holdout_access_blocks_every_readiness_decision() -> None:
    evidence = DataReadinessEvidence(
        independent_utc_days=90,
        independent_events=59_056,
        approved_historical_representation=True,
        detail_coverage_complete=False,
        h0_orderbook=True,
        h2_snapshots=True,
        h2_ticks=False,
        holdout_accessed=True,
    )
    assert evaluate_path_readiness(evidence).status is ReadinessStatus.BLOCKED
    assert evaluate_microstructure_readiness(evidence).status is ReadinessStatus.BLOCKED


def test_path_foundation_is_ready_but_full_training_is_partial_on_bounded_detail() -> None:
    evidence = DataReadinessEvidence(
        independent_utc_days=90,
        independent_events=59_056,
        approved_historical_representation=True,
        detail_coverage_complete=False,
        h0_orderbook=False,
        h2_snapshots=False,
        h2_ticks=False,
    )
    decision = evaluate_path_readiness(evidence)
    assert decision.status is ReadinessStatus.READY
    assert decision.decision == "APPROVED_FOR_FOUNDATION"
    assert decision.full_training_status is ReadinessStatus.PARTIAL


def test_microstructure_snapshot_only_evidence_is_partial() -> None:
    evidence = DataReadinessEvidence(
        independent_utc_days=90,
        independent_events=59_056,
        approved_historical_representation=True,
        detail_coverage_complete=False,
        h0_orderbook=True,
        h2_snapshots=True,
        h2_ticks=False,
    )
    decision = evaluate_microstructure_readiness(evidence)
    assert decision.status is ReadinessStatus.PARTIAL
    assert "ticks" in decision.blocked_by[0].lower()


def test_manifest_rejects_holdout_as_an_allowed_source(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "foundation_version": "1.0.0",
                "families": [
                    {
                        "family_id": "bad",
                        "upstream_name": "x",
                        "role": "path",
                        "status": "READY",
                        "approved_data_sources": ["DATASET_V2_HOLDOUT"],
                        "blocked_by": [],
                        "notes": "bad",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="holdout"):
        load_model_zoo_manifest(path)
