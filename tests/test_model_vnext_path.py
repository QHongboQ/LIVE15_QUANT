from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from live15_quant.model_vnext_path import (
    FEATURE_SETS,
    Mvn002Error,
    build_path_examples,
    chronological_folds,
)

FEATURES = (
    "underlying_price",
    *FEATURE_SETS["A4"],
)


@dataclass(frozen=True)
class Row:
    asset: str
    ticker: str
    decision_timestamp: datetime
    window_start: datetime
    window_end: datetime
    values: tuple[Decimal | None, ...]


@dataclass(frozen=True)
class Dataset:
    dataset_id: str
    deterministic_build_hash: str
    feature_names: tuple[str, ...]
    splits: dict[str, tuple[Row, ...]]


def _row(ticker: str, start: datetime, offset: int, value: str = "100") -> Row:
    values = tuple(
        Decimal(value) if name == "underlying_price" else Decimal("0.01") for name in FEATURES
    )
    return Row(
        "BTC",
        ticker,
        start + timedelta(seconds=offset),
        start,
        start + timedelta(seconds=900),
        values,
    )


def _dataset(*, windows: int = 10) -> Dataset:
    start = datetime(2026, 8, 20, tzinfo=UTC)
    rows: list[Row] = []
    offsets = (0, 5, 15, 30, 60, 120, 180, 300, 600, 840)
    for index in range(windows):
        window = start + timedelta(seconds=index * 900)
        rows.extend(
            _row(f"t{index}", window, offset, str(100 + index + offset / 1000))
            for offset in offsets
        )
    empty = tuple()
    return Dataset(
        "live15-dataset-v1-f81d7d1feebcbbaecff9",
        "build",
        FEATURES,
        {"train": tuple(rows), "validation": empty, "test": empty},
    )


def test_builder_keeps_all_declared_horizons_and_exact_future_only() -> None:
    examples = build_path_examples(_dataset(windows=2))
    five = [item for item in examples if item.horizon == 5 and item.missing_reason is None]
    thirty = [item for item in examples if item.horizon == 30 and item.missing_reason is None]
    terminal = [item for item in examples if item.horizon == "window_end"]
    assert five
    assert thirty
    assert all(
        item.target_timestamp and item.target_timestamp > item.decision_timestamp
        for item in five + thirty
    )
    assert terminal and all(
        item.missing_reason == "future_observation_unavailable" for item in terminal
    )


def test_missing_future_is_typed_not_interpolated() -> None:
    dataset = _dataset(windows=1)
    rows = tuple(
        item for item in dataset.splits["train"] if item.decision_timestamp.second not in {5, 15}
    )
    examples = build_path_examples(
        Dataset(
            dataset.dataset_id,
            dataset.deterministic_build_hash,
            dataset.feature_names,
            {"train": rows, "validation": (), "test": ()},
        )
    )
    assert any(
        item.horizon == 5 and item.missing_reason == "future_observation_unavailable"
        for item in examples
    )


def test_folds_keep_event_groups_and_purge_embargo() -> None:
    examples = build_path_examples(_dataset(windows=24))
    folds = chronological_folds(
        tuple(item for item in examples if item.horizon == 30 and item.missing_reason is None), 3
    )
    for fold in folds:
        assert {item.event_id for item in fold.train}.isdisjoint(
            {item.event_id for item in fold.validation}
        )
        assert max(
            item.target_timestamp for item in fold.train if item.target_timestamp
        ) + timedelta(seconds=600) <= min(item.decision_timestamp for item in fold.validation)


def test_unknown_dataset_identity_fails_closed() -> None:
    dataset = _dataset(windows=1)
    with pytest.raises(Mvn002Error):
        build_path_examples(
            Dataset(
                "wrong", dataset.deterministic_build_hash, dataset.feature_names, dataset.splits
            )
        )
