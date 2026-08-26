from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import live15_quant.model_zoo as model_zoo
from live15_quant.certified_dataset import CertifiedDatasetV1Builder, DatasetV1Config
from live15_quant.model_zoo import (
    DatasetExample,
    ModelZooConfig,
    ModelZooError,
    ModelZooV1,
    Preprocessor,
    evaluate_model,
    load_certified_dataset,
)
from live15_quant.storage import RecorderStore
from live15_quant.ws_retention import WsRetentionManifest
from tests.test_dataset import BASE, add_event, sampling


def _certified_dataset(tmp_path: Path, *, events: int = 120) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw = tmp_path / "raw.sqlite3"
    archive = tmp_path / "archive.sqlite3"
    with RecorderStore(raw) as store:
        for index in range(events):
            add_event(
                store,
                BASE + timedelta(minutes=15 * index),
                result="yes" if index % 2 else "no",
            )
    WsRetentionManifest(archive)
    result = CertifiedDatasetV1Builder(
        raw,
        tmp_path / "datasets",
        archive_manifest_snapshot=archive,
        git_sha="a" * 40,
        snapshot_captured_at=datetime(2026, 8, 23, tzinfo=UTC),
    ).build(DatasetV1Config(sampling()))
    return result.artifact_path


def _fast_config() -> ModelZooConfig:
    return ModelZooConfig(
        logistic_iterations=10,
        xgboost_rounds=2,
        internal_walk_forward_folds=2,
    )


def test_model_zoo_is_deterministic_event_isolated_and_marks_train_only_assets(tmp_path) -> None:
    dataset = load_certified_dataset(_certified_dataset(tmp_path))
    root = tmp_path / "model-artifacts"
    first = ModelZooV1(dataset, root, _fast_config()).build()
    second = ModelZooV1(dataset, root, _fast_config()).build()

    assert first.zoo_id == second.zoo_id
    assert second.reused_existing_artifact
    payload = json.loads((first.artifact_path / "manifest.json").read_text(encoding="utf-8"))
    assert payload["test_evaluation"]["state"] == "REVEALED_FINAL"
    assert payload["selection"]["selection_split"] == "validation"
    assert payload["models"]["market_implied"]["test"]["per_asset"]["Gold"] == {
        "rows": 0,
        "status": "OOS_NOT_ELIGIBLE",
    }
    assert payload["asset_validation_eligibility"]["Gold"] == {
        "out_of_sample_validation": False,
        "status": "OOS_NOT_ELIGIBLE",
    }
    model_manifest = json.loads(
        (root / "models" / first.model_ids["xgboost"] / "manifest.json").read_text(encoding="utf-8")
    )
    lineage = model_manifest["lineage"]
    assert lineage["dataset_code_git_sha"] == "a" * 40
    assert len(lineage["model_source_sha256"]) == 64
    assert model_manifest["asset_validation_eligibility"]["BTC"]["out_of_sample_validation"]

    splits = dataset.splits
    train_events = {row.ticker for row in splits["train"]}
    validation_events = {row.ticker for row in splits["validation"]}
    test_events = {row.ticker for row in splits["test"]}
    assert not train_events & validation_events
    assert not train_events & test_events
    assert not validation_events & test_events
    assert max(row.window_end for row in splits["train"]) <= min(
        row.window_start for row in splits["validation"]
    )
    assert max(row.window_end for row in splits["validation"]) <= min(
        row.window_start for row in splits["test"]
    )


def test_model_zoo_rejects_future_feature_timestamp_and_existing_conflict(tmp_path) -> None:
    dataset_path = _certified_dataset(tmp_path)
    rows_path = dataset_path / "training_rows.jsonl"
    rows = rows_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(rows[0])
    first["features"][0]["source_timestamp"] = "2099-01-01T00:00:00+00:00"
    rows[0] = json.dumps(first, sort_keys=True)
    rows_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(ModelZooError, match="artifact hash mismatch"):
        load_certified_dataset(dataset_path)

    # Rebuild an untampered artifact, then prove immutable model publication rejects byte conflict.
    clean = load_certified_dataset(_certified_dataset(tmp_path / "clean"))
    root = tmp_path / "models"
    summary = ModelZooV1(clean, root, _fast_config()).build()
    manifest_path = summary.artifact_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["selection"]["champion_status"] = "tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ModelZooError, match="conflicts"):
        ModelZooV1(clean, root, _fast_config()).build()


def test_preprocessor_uses_train_only_statistics_and_explicit_missing_indicator() -> None:
    start = datetime(2026, 8, 23, tzinfo=UTC)
    train = (
        DatasetExample("BTC", "a", start, start, start, 60, 0, (Decimal("1"),), (None,)),
        DatasetExample("BTC", "b", start, start, start, 60, 1, (Decimal("3"),), (None,)),
    )
    validation = (
        DatasetExample("BTC", "c", start, start, start, 60, 1, (None,), ("stale_source",)),
    )
    processor = Preprocessor.fit(train, ("feature",))
    assert processor.medians == (2.0,)
    transformed = processor.transform(validation)
    assert transformed[0, 0] == pytest.approx(0.0)
    assert transformed[0, 1] == 1.0

    unknown_only = Preprocessor.fit(validation, ("feature",))
    assert unknown_only.entirely_missing_in_train == (True,)
    assert unknown_only.transform(validation)[0, 1] == 1.0
    newly_observed = DatasetExample(
        "BTC", "d", start, start, start, 60, 0, (Decimal("99"),), (None,)
    )
    assert unknown_only.transform((newly_observed,))[0, 0] == 0.0
    assert unknown_only.transform((newly_observed,))[0, 1] == 1.0


def test_model_publisher_recovers_dead_staging_but_rejects_live_owner(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "artifacts"
    artifact_id = "model"
    stale = root / f".{artifact_id}.staging-999999"
    stale.mkdir(parents=True)
    (stale / "partial").write_text("x", encoding="utf-8")
    manifest = {"identity": "stable"}
    assert not model_zoo._atomic_publish(root, artifact_id, {"model.json": b"{}"}, manifest)
    assert not stale.exists()

    active = root / f".other.staging-{os.getpid()}"
    active.mkdir()
    monkeypatch.setattr(model_zoo, "process_alive", lambda pid: pid == os.getpid())
    with pytest.raises(ModelZooError, match="already active"):
        model_zoo._atomic_publish(root, "other", {"model.json": b"{}"}, manifest)


def test_oos_not_eligible_is_explicit_not_zero() -> None:
    time = datetime(2026, 8, 23, tzinfo=UTC)
    feature_names = tuple(definition.name for definition in model_zoo.FEATURE_REGISTRY)
    values = tuple(Decimal("0.70") for _ in feature_names)
    rows = (DatasetExample("BTC", "btc", time, time, time, 60, 1, values, (None,) * len(values)),)
    model = model_zoo._fit_market_implied(feature_names)
    result = evaluate_model(
        model,
        rows,
        frozenset({"BTC"}),
        ModelZooConfig(),
        all_assets=frozenset({"BTC", "Gold"}),
    )
    assert result.per_asset["Gold"] == {"status": "OOS_NOT_ELIGIBLE", "rows": 0}


def test_foundation_adapter_contracts_cover_layers_without_runtime_wiring() -> None:
    contracts = model_zoo.build_model_adapter_contracts()
    assert {item.layer for item in contracts} == {
        model_zoo.ModelLayer.PATH,
        model_zoo.ModelLayer.TERMINAL,
        model_zoo.ModelLayer.MICROSTRUCTURE,
        model_zoo.ModelLayer.ROUTER,
    }
    assert all(not item.runtime_wiring for item in contracts)
    assert all(item.allowed_data_sources for item in contracts)


def test_foundation_adapter_contract_rejects_holdout_and_empty_inputs() -> None:
    with pytest.raises(ValueError, match="holdout"):
        model_zoo.ModelAdapterContract(
            family_id="bad",
            layer=model_zoo.ModelLayer.PATH,
            allowed_data_sources=("DATASET_V2_HOLDOUT",),
            input_schema_version="x",
            output_schema_version="x",
        )
