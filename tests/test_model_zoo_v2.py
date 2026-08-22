from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from live15_quant.model_zoo import ModelZooConfig, ModelZooError, ModelZooV1, load_certified_dataset
from live15_quant.model_zoo_v2 import (
    DEFAULT_THRESHOLDS,
    ModelZooV2,
    ModelZooV2Config,
    _development_folds,
    _promotion_status,
    _Spec,
)
from tests.test_model_zoo import _certified_dataset


def _fast_config() -> ModelZooV2Config:
    return ModelZooV2Config(
        folds=2,
        logistic_iterations=10,
        xgboost_rounds=2,
        min_trade_count=1,
        min_profit_factor=Decimal("0"),
        max_drawdown=Decimal("999"),
        min_positive_assets=1,
    )


def _v1_artifact(dataset, root: Path) -> Path:
    return (
        ModelZooV1(
            dataset,
            root,
            ModelZooConfig(logistic_iterations=10, xgboost_rounds=2, internal_walk_forward_folds=2),
        )
        .build()
        .artifact_path
    )


def test_v2_is_deterministic_and_does_not_emit_dataset_test_metrics(tmp_path: Path) -> None:
    dataset = load_certified_dataset(_certified_dataset(tmp_path, events=120))
    v1 = _v1_artifact(dataset, tmp_path / "v1")
    first = ModelZooV2(dataset, tmp_path / "v2", v1, _fast_config()).build()
    second = ModelZooV2(dataset, tmp_path / "v2", v1, _fast_config()).build()

    assert first.zoo_id == second.zoo_id
    assert second.reused_existing_artifact
    manifest = json.loads((first.artifact_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["final_test"] == {
        "state": "REVEALED_FINAL",
        "policy": "DATASET_V1_FINAL_TEST_REVEALED_NOT_USED_FOR_V2_DEVELOPMENT",
        "v2_test_rows_consumed_for_development": False,
        "v2_test_metrics_emitted": False,
        "dataset_artifact_hash_verified": True,
    }
    assert [item for item in manifest["config"]["thresholds"]] == [
        str(item) for item in DEFAULT_THRESHOLDS
    ]
    assert manifest["certified_data_contract"]["feature_schema"]["order"] == list(
        dataset.feature_names
    )
    assert (
        "limited asset-feature interactions"
        in manifest["candidate_definitions"]["xgboost_asset_identity"]["definition"]
    )
    assert manifest["asset_aware_configuration"]["asset_identity_order"] == sorted(
        dataset.oos_assets
    )
    assert len(manifest["candidates"]["xgboost_pooled_identity"]["folds"]) == 2
    assert (
        manifest["candidates"]["xgboost_pooled_identity"]["folds"][0]["development_evaluation"][
            "thresholds"
        ]["0.03"]["trade_count"]
        >= 0
    )
    assert "test" not in json.dumps(manifest["candidates"]).lower()
    assert manifest["status"] in {"FORWARD_CANDIDATE", "NO_FORWARD_CANDIDATE"}


def test_v2_build_never_accesses_validation_or_test_rows(tmp_path: Path) -> None:
    dataset = load_certified_dataset(_certified_dataset(tmp_path, events=120))
    v1 = _v1_artifact(dataset, tmp_path / "v1")

    class TrainOnlySplits(dict[str, object]):
        def __getitem__(self, key: str):
            if key in {"validation", "test"}:
                raise AssertionError(f"v2 must not access Dataset v1 {key} rows")
            return super().__getitem__(key)

    train_only = replace(dataset, splits=TrainOnlySplits({"train": dataset.splits["train"]}))
    summary = ModelZooV2(train_only, tmp_path / "v2", v1, _fast_config()).build()
    assert summary.status in {"FORWARD_CANDIDATE", "NO_FORWARD_CANDIDATE"}


def test_v2_folds_are_grouped_chronological_and_calibration_is_past_only(tmp_path: Path) -> None:
    dataset = load_certified_dataset(_certified_dataset(tmp_path, events=120))
    rows = tuple(row for row in dataset.splits["train"] if row.asset in dataset.oos_assets)
    folds = _development_folds(rows, 2)
    for train, validation in folds:
        assert {row.ticker for row in train}.isdisjoint({row.ticker for row in validation})
        assert max(row.window_end for row in train) <= min(row.window_start for row in validation)


def test_v2_rejects_v1_artifact_without_revealed_final_test(tmp_path: Path) -> None:
    dataset = load_certified_dataset(_certified_dataset(tmp_path, events=120))
    v1 = _v1_artifact(dataset, tmp_path / "v1")
    manifest_path = v1 / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["test_evaluation"]["state"] = "UNSEEN"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ModelZooError, match="revealed"):
        ModelZooV2(dataset, tmp_path / "v2", v1, _fast_config()).build()


def test_v2_promotion_rejects_single_asset_or_few_trade_driver() -> None:
    baseline = {"probability_metrics": {"brier_score": 0.10}}
    trade = {
        "candidate_probability_metrics": {"brier_score": 0.10},
        "trade_count": 100,
        "net_pnl_estimate": "5.00",
        "profit_factor": "2.00",
        "max_drawdown": "1.00",
        "positive_net_pnl_folds": 2,
        "per_asset": {
            "BTC": {"trades": 95, "net_pnl_estimate": "4.99"},
            "ETH": {"trades": 5, "net_pnl_estimate": "0.01"},
        },
    }
    gate = _promotion_status(
        _Spec("candidate", "xgboost", "identity"), trade, baseline, ModelZooV2Config(folds=2)
    )
    assert gate["status"] == "REJECTED"
    assert "development_economics_not_supported_by_multiple_assets" in gate["reasons"]


def test_v2_promotion_requires_repeated_positive_chronological_folds() -> None:
    baseline = {"probability_metrics": {"brier_score": 0.10}}
    trade = {
        "candidate_probability_metrics": {"brier_score": 0.10},
        "trade_count": 100,
        "net_pnl_estimate": "5.00",
        "profit_factor": "2.00",
        "max_drawdown": "1.00",
        "positive_net_pnl_folds": 1,
        "per_asset": {
            "BTC": {"trades": 50, "net_pnl_estimate": "2.50"},
            "ETH": {"trades": 50, "net_pnl_estimate": "2.50"},
        },
    }
    gate = _promotion_status(
        _Spec("candidate", "xgboost", "identity"), trade, baseline, ModelZooV2Config(folds=2)
    )
    assert gate["status"] == "REJECTED"
    assert "development_economics_not_repeated_across_fixed_chronological_folds" in gate["reasons"]
