from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tools.run_mvn002r import (
    BUILD_HASH,
    DATASET_ID,
    FEATURE_SETS,
    HORIZONS,
    PURGE_EMBARGO_SECONDS,
    Example,
    Mvn002RError,
    _manifest,
    _metrics,
    _split_token,
)


def test_fixed_dataset_identity_and_holdout_guard(monkeypatch) -> None:
    manifest = {
        "dataset_id": DATASET_ID,
        "deterministic_build_hash": BUILD_HASH,
        "immutable": True,
        "leakage_checker": {"status": "PASS"},
        "fresh_holdout": {"state": "UNREVEALED_FROZEN", "evaluation_performed": False},
    }
    monkeypatch.setattr("pathlib.Path.read_text", lambda *_args, **_kwargs: json.dumps(manifest))
    assert _manifest(__import__("pathlib").Path("D:/synthetic-v2"))["dataset_id"] == DATASET_ID
    manifest["fresh_holdout"]["evaluation_performed"] = True
    with pytest.raises(Mvn002RError, match="HOLDOUT"):
        _manifest(__import__("pathlib").Path("D:/synthetic-v2"))


def test_test_split_is_detectable_without_decoding() -> None:
    assert _split_token('{"split":"test","future_return":"secret"}') == "test"


def test_fixed_contract_is_not_expanded() -> None:
    assert HORIZONS == (5, 15, 30, 60, 120, 180, 300, "window_end")
    assert PURGE_EMBARGO_SECONDS == 600
    assert set(FEATURE_SETS) == {"A0", "A1", "A2", "A3", "A4"}
    assert "top_depth_imbalance" not in FEATURE_SETS["A4"]


def test_metrics_are_deterministic_and_include_probability_quality() -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    rows = tuple(
        Example(
            f"e{i}",
            "BTC",
            "train",
            now + timedelta(minutes=i),
            now,
            now + timedelta(minutes=15),
            30,
            {},
            value,
            int(value > 0),
        )
        for i, value in enumerate((-0.01, 0.01, 0.02, -0.02))
    )
    first = _metrics(
        rows,
        __import__("numpy").array((-0.01, 0.01, 0.01, -0.01)),
        __import__("numpy").array((0.2, 0.8, 0.7, 0.3)),
    )
    second = _metrics(
        rows,
        __import__("numpy").array((-0.01, 0.01, 0.01, -0.01)),
        __import__("numpy").array((0.2, 0.8, 0.7, 0.3)),
    )
    assert first == second
    assert {"mae", "rmse", "directional_accuracy", "logloss", "brier", "ece"} <= first.keys()


def test_committed_report_has_v2_lineage_and_no_holdout_consumption() -> None:
    report = json.loads(
        __import__("pathlib")
        .Path("docs/model_vnext_mvn002r_report.json")
        .read_text(encoding="utf-8")
    )
    assert report["dataset_id"] == DATASET_ID
    assert report["build_hash"] == BUILD_HASH
    assert report["holdout_rows_consumed"] is False
    assert report["holdout_labels_loaded"] is False
    assert report["leakage_checker"] == "PASS"
    assert report["sequence_gate"] == "INSUFFICIENT_SEQUENCE_EVIDENCE"
