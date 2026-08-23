from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from live15_quant.model_v3_structured import (
    INPUT_FEATURES,
    V3StructuredConfig,
    V3StructuredDevelopment,
    V3StructuredError,
    _folds,
    _horizon_status,
    build_structured_dataset,
    evaluate_rule_assisted_regime,
)


@dataclass(frozen=True)
class DatasetExample:
    asset: str
    ticker: str
    decision_timestamp: datetime
    window_start: datetime
    window_end: datetime
    bucket_seconds: int
    label_yes: int
    values: tuple[Decimal | None, ...]
    missing_reasons: tuple[str | None, ...]


@dataclass(frozen=True)
class CertifiedDataset:
    root: Path
    manifest: dict[str, object]
    feature_names: tuple[str, ...]
    splits: dict[str, tuple[DatasetExample, ...]]
    oos_assets: frozenset[str]
    train_only_assets: frozenset[str]

    @property
    def dataset_id(self) -> str:
        return str(self.manifest["dataset_id"])

    @property
    def deterministic_build_hash(self) -> str:
        return str(self.manifest["deterministic_build_hash"])


def _row(asset: str, ticker: str, start: datetime, seconds: int, label: int = 1) -> DatasetExample:
    names = INPUT_FEATURES
    values = tuple(
        Decimal("100") + Decimal(index) / Decimal("100") for index, _ in enumerate(names)
    )
    # Build a tiny self-contained schema whose indices are used by the builder.
    return DatasetExample(
        asset,
        ticker,
        start + timedelta(seconds=seconds),
        start,
        start + timedelta(minutes=15),
        900 - seconds,
        label,
        values,
        tuple(None for _ in values),
    )


def _dataset(events: int = 12) -> CertifiedDataset:
    start = datetime(2026, 8, 20, tzinfo=UTC)
    train: list[DatasetExample] = []
    validation = (_row("BTC", "validation-poison", start + timedelta(days=8), 30),)
    test = (_row("BTC", "test-poison", start + timedelta(days=9), 30),)
    for index in range(events):
        window = start + timedelta(minutes=15 * index)
        train.extend(
            _row("BTC", f"t{index}", window, seconds)
            for seconds in (30, 60, 120, 180, 240, 300, 360, 480, 600)
        )
    return CertifiedDataset(
        Path("."),
        {"dataset_id": "synthetic", "deterministic_build_hash": "hash"},
        INPUT_FEATURES,
        {"train": tuple(train), "validation": validation, "test": test},
        frozenset({"BTC"}),
        frozenset(),
    )


def test_structured_builder_reads_train_only_and_targets_are_strictly_future() -> None:
    structured = build_structured_dataset(_dataset())
    assert structured.atomic_ws_microstructure_status == "INSUFFICIENT_MICROSTRUCTURE_EVIDENCE"
    assert all(
        "poison" not in item.ticker
        for rows in structured.samples_by_horizon.values()
        for item in rows
    )
    assert all(
        item.target_timestamp > item.decision_timestamp
        for rows in structured.samples_by_horizon.values()
        for item in rows
    )


def test_structured_default_evidence_gate_is_not_silently_relaxed() -> None:
    structured = build_structured_dataset(_dataset(16))
    config = V3StructuredConfig()
    assert len(structured.calendar_days) < config.min_calendar_days
    assert "INSUFFICIENT" in structured.atomic_ws_microstructure_status


def test_missing_inputs_are_rejected_not_zero_filled() -> None:
    dataset = _dataset(1)
    broken = dataset.splits["train"][0]
    rows = list(dataset.splits["train"])
    rows[0] = DatasetExample(
        broken.asset,
        broken.ticker,
        broken.decision_timestamp,
        broken.window_start,
        broken.window_end,
        broken.bucket_seconds,
        broken.label_yes,
        (None, *broken.values[1:]),
        broken.missing_reasons,
    )
    result = build_structured_dataset(
        CertifiedDataset(
            dataset.root,
            dataset.manifest,
            dataset.feature_names,
            {**dataset.splits, "train": tuple(rows)},
            dataset.oos_assets,
            dataset.train_only_assets,
        )
    )
    assert result.skipped_missing_inputs >= 1


def test_non_positive_return_base_is_rejected_not_divided_or_filled() -> None:
    dataset = _dataset(1)
    source = dataset.splits["train"][0]
    rows = list(dataset.splits["train"])
    rows[0] = DatasetExample(
        source.asset,
        source.ticker,
        source.decision_timestamp,
        source.window_start,
        source.window_end,
        source.bucket_seconds,
        source.label_yes,
        (Decimal(0), *source.values[1:]),
        source.missing_reasons,
    )
    result = build_structured_dataset(
        CertifiedDataset(
            dataset.root,
            dataset.manifest,
            dataset.feature_names,
            {**dataset.splits, "train": tuple(rows)},
            dataset.oos_assets,
            dataset.train_only_assets,
        )
    )
    assert result.skipped_invalid_target_base == 1


def test_impossible_target_outside_event_is_not_constructed() -> None:
    dataset = _dataset(1)
    result = build_structured_dataset(dataset)
    assert all(
        item.target_timestamp < item.window_end
        for rows in result.samples_by_horizon.values()
        for item in rows
    )


def test_duplicate_timestamp_within_event_fails_loudly() -> None:
    dataset = _dataset(1)
    original = dataset.splits["train"][0]
    duplicate = DatasetExample(
        original.asset,
        original.ticker,
        original.decision_timestamp,
        original.window_start,
        original.window_end,
        original.bucket_seconds,
        1 - original.label_yes,
        original.values,
        original.missing_reasons,
    )
    duplicated = CertifiedDataset(
        dataset.root,
        dataset.manifest,
        dataset.feature_names,
        {**dataset.splits, "train": (*dataset.splits["train"], duplicate)},
        dataset.oos_assets,
        dataset.train_only_assets,
    )
    with pytest.raises(V3StructuredError, match="duplicate Dataset v1 decision timestamp"):
        build_structured_dataset(duplicated)


def test_chronological_folds_keep_all_assets_in_one_market_window_together() -> None:
    dataset = _dataset(12)
    rows = list(dataset.splits["train"])
    # Add a second asset for each identical 15-minute window.  Event identity
    # remains asset-specific, but their decision/target clocks overlap.
    rows.extend(
        DatasetExample(
            "ETH",
            f"eth-{row.ticker}",
            row.decision_timestamp,
            row.window_start,
            row.window_end,
            row.bucket_seconds,
            row.label_yes,
            row.values,
            row.missing_reasons,
        )
        for row in dataset.splits["train"]
    )
    structured = build_structured_dataset(
        CertifiedDataset(
            dataset.root,
            dataset.manifest,
            dataset.feature_names,
            {**dataset.splits, "train": tuple(rows)},
            dataset.oos_assets,
            dataset.train_only_assets,
        )
    )
    for train, validation in _folds(structured.samples_by_horizon[30], 3):
        assert {item.window_start for item in train}.isdisjoint(
            {item.window_start for item in validation}
        )
        assert max(item.target_timestamp for item in train) < min(
            item.decision_timestamp for item in validation
        )


def test_rule_assisted_regime_keeps_missing_inputs_typed_unavailable() -> None:
    dataset = _dataset(1)
    source = dataset.splits["train"][0]
    rows = list(dataset.splits["train"])
    rows[0] = DatasetExample(
        source.asset,
        source.ticker,
        source.decision_timestamp,
        source.window_start,
        source.window_end,
        source.bucket_seconds,
        source.label_yes,
        (*source.values[:6], None, *source.values[7:]),
        source.missing_reasons,
    )
    result = evaluate_rule_assisted_regime(
        CertifiedDataset(
            dataset.root,
            dataset.manifest,
            dataset.feature_names,
            {**dataset.splits, "train": tuple(rows)},
            dataset.oos_assets,
            dataset.train_only_assets,
        )
    )
    assert result["data_unavailable_rows"] == 1
    assert result["label_counts"]["DATA_UNAVAILABLE"] == 1


def test_short_horizon_is_reported_not_silently_dropped_when_a_fold_is_small() -> None:
    config = V3StructuredConfig(min_examples_per_fold=40)
    assert (
        _horizon_status([{"examples": 80}, {"examples": 20}], config, diagnostic_only=False)
        == "REJECTED_HORIZON_INSUFFICIENT_FOLD_EVIDENCE"
    )


def test_structured_artifact_is_train_only_and_deterministically_reused(tmp_path: Path) -> None:
    config = V3StructuredConfig(
        xgboost_rounds=1,
        min_independent_events=1,
        min_examples_per_horizon=1,
        min_examples_per_fold=1,
        min_calendar_days=1,
    )
    first = V3StructuredDevelopment(_dataset(12), tmp_path, config).build()
    second = V3StructuredDevelopment(_dataset(12), tmp_path, config).build()
    assert second.reused_existing_artifact is True
    assert first.artifact_id == second.artifact_id
    report = json.loads((first.artifact_path / "development_report.json").read_text())
    assert report["final_test"]["state"] == "REVEALED_FINAL"
    assert report["final_test"]["rows_consumed"] is False
    assert report["microstructure_evaluations"]["promotion_eligible"] is False
    assert report["candidate"]["status"] == "NO_V3_FORWARD_CANDIDATE"
    assert report["lineage"]["model_files_sha256"]
