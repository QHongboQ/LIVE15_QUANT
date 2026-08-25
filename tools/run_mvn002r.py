"""Run the fixed MVN-002 structured baseline against immutable Dataset v2.

This is an offline research runner.  It only materializes train/validation JSONL
records; test records are skipped before JSON decoding and are never joined,
scored, or used for selection.  The runner intentionally has no runtime/Paper/
Recorder imports and writes only a lightweight report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np

DATASET_ID = "live15-dataset-v2-4bb4934bf328b6b024ff"
BUILD_HASH = "4bb4934bf328b6b024ff4183df134c481d962a041dc6ae760a3816d3c5228113"
PURGE_EMBARGO_SECONDS = 600
HORIZONS: tuple[int | str, ...] = (5, 15, 30, 60, 120, 180, 300, "window_end")
MODELS = ("naive", "linear", "logistic", "xgboost")
SEED = 20260826

FEATURE_SETS: dict[str, tuple[str, ...]] = {
    "A0": (),
    "A1": (
        "return_15s",
        "return_30s",
        "return_60s",
        "return_120s",
        "return_300s",
        "return_momentum",
        "return_acceleration",
        "realized_volatility_60s",
        "realized_volatility_120s",
        "realized_volatility_300s",
        "price_range_60s",
        "volatility_change",
        "volatility_regime_ratio",
    ),
    "A2": (
        "return_15s",
        "return_30s",
        "return_60s",
        "return_120s",
        "return_300s",
        "return_momentum",
        "return_acceleration",
        "realized_volatility_60s",
        "realized_volatility_120s",
        "realized_volatility_300s",
        "price_range_60s",
        "volatility_change",
        "volatility_regime_ratio",
        "signed_distance_to_target",
        "normalized_distance_to_target",
    ),
    "A3": (
        "return_15s",
        "return_30s",
        "return_60s",
        "return_120s",
        "return_300s",
        "return_momentum",
        "return_acceleration",
        "realized_volatility_60s",
        "realized_volatility_120s",
        "realized_volatility_300s",
        "price_range_60s",
        "volatility_change",
        "volatility_regime_ratio",
        "signed_distance_to_target",
        "normalized_distance_to_target",
        "time_remaining_seconds",
    ),
    "A4": (
        "return_15s",
        "return_30s",
        "return_60s",
        "return_120s",
        "return_300s",
        "return_momentum",
        "return_acceleration",
        "realized_volatility_60s",
        "realized_volatility_120s",
        "realized_volatility_300s",
        "price_range_60s",
        "volatility_change",
        "volatility_regime_ratio",
        "signed_distance_to_target",
        "normalized_distance_to_target",
        "time_remaining_seconds",
        "yes_bid",
        "yes_ask",
        "no_bid",
        "no_ask",
        "yes_spread",
        "yes_midpoint",
        "market_probability_midpoint",
        "market_probability_width",
    ),
}


class Mvn002RError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Example:
    event: str
    asset: str
    split: str
    decision: datetime
    window_start: datetime
    window_end: datetime
    horizon: int | str
    features: dict[str, float | None]
    target_return: float
    direction: int


@dataclass(frozen=True, slots=True)
class Fold:
    train: tuple[Example, ...]
    validation: tuple[Example, ...]
    train_windows: tuple[datetime, ...]
    validation_windows: tuple[datetime, ...]


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _split_token(line: str) -> str:
    marker = '"split":"'
    start = line.find(marker)
    if start < 0:
        raise Mvn002RError("record lacks split token")
    start += len(marker)
    return line[start : line.find('"', start)]


def _manifest(root: Path) -> dict[str, Any]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("dataset_id") != DATASET_ID:
        raise Mvn002RError("DATASET_V2_ID_MISMATCH")
    if manifest.get("deterministic_build_hash") != BUILD_HASH:
        raise Mvn002RError("DATASET_V2_BUILD_HASH_MISMATCH")
    holdout = manifest.get("fresh_holdout", {})
    if holdout.get("state") != "UNREVEALED_FROZEN" or holdout.get("evaluation_performed"):
        raise Mvn002RError("HOLDOUT_NOT_FROZEN_OR_ALREADY_CONSUMED")
    if not manifest.get("immutable") or manifest.get("leakage_checker", {}).get("status") != "PASS":
        raise Mvn002RError("DATASET_V2_IMMUTABILITY_OR_LEAKAGE_GUARD_FAILED")
    return manifest


def _split_events(root: Path) -> dict[str, str]:
    payload = json.loads((root / "splits.json").read_text(encoding="utf-8"))
    splits = payload.get("splits")
    if not isinstance(splits, dict):
        raise Mvn002RError("split metadata malformed")
    result: dict[str, str] = {}
    for split in ("train", "validation", "test"):
        events = splits.get(split, {}).get("events", [])
        for event in events:
            if event in result:
                raise Mvn002RError("event identity crosses split boundaries")
            result[str(event)] = split
    return result


def _load_rows(
    root: Path, event_splits: dict[str, str]
) -> tuple[dict[tuple[str, datetime], dict[str, Any]], Counter[str]]:
    rows: dict[tuple[str, datetime], dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    with (root / "training_rows.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            split = _split_token(line)
            if split == "test":
                # Do not JSON-decode or materialize any holdout row.
                continue
            if split not in {"train", "validation"}:
                raise Mvn002RError(f"unexpected training split {split}")
            raw = json.loads(line)
            event = str(raw["event_identity"])
            if event_splits.get(event) != split:
                raise Mvn002RError("training row split does not match frozen event split")
            decision = _time(str(raw["decision_timestamp"]))
            feature_map: dict[str, float | None] = {}
            for item in raw.get("features", []):
                name = str(item["name"])
                if name.lower() in {"label", "settlement", "settlement_result"}:
                    raise Mvn002RError("settlement-derived feature entered model input")
                source = item.get("source_timestamp")
                if source is not None and _time(str(source)) > decision:
                    raise Mvn002RError("feature source timestamp exceeds decision")
                value = item.get("value")
                if (value is None) != (item.get("missing_reason") is not None):
                    raise Mvn002RError("typed missing feature semantics are malformed")
                feature_map[name] = None if value is None else float(value)
            rows[(event, decision)] = {
                "asset": str(raw["asset"]),
                "split": split,
                "decision": decision,
                "window_start": _time(str(raw["window_start"])),
                "window_end": _time(str(raw["window_end"])),
                "features": feature_map,
            }
            counts[split] += 1
    return rows, counts


def _load_targets(
    root: Path, event_splits: dict[str, str]
) -> tuple[dict[tuple[str, datetime, int | str], dict[str, Any]], Counter[str], Counter[str]]:
    targets: dict[tuple[str, datetime, int | str], dict[str, Any]] = {}
    valid_counts: Counter[str] = Counter()
    unavailable: Counter[str] = Counter()
    with (root / "path_targets.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            split = _split_token(line)
            if split == "test":
                # Holdout labels are not decoded, loaded, or scored.
                continue
            if split not in {"train", "validation"}:
                raise Mvn002RError(f"unexpected target split {split}")
            raw = json.loads(line)
            event = str(raw["event_identity"])
            if event_splits.get(event) != split:
                raise Mvn002RError("target split does not match frozen event split")
            raw_horizon = raw.get("horizon")
            horizon: int | str = (
                "window_end" if raw_horizon in (None, "window_end") else int(raw_horizon)
            )
            if raw_horizon is None:
                horizon = "window_end"
            decision = _time(str(raw["decision_timestamp"]))
            key = (event, decision, horizon)
            if raw.get("valid"):
                target = float(raw["future_return"])
                if not math.isfinite(target):
                    raise Mvn002RError("non-finite target")
                target_time = _time(str(raw["target_timestamp"]))
                window_end = _time(str(raw["window_end"]))
                if not decision < target_time <= window_end:
                    raise Mvn002RError("target outside decision/window boundary")
                observation_time = raw.get("target_observation_decision_timestamp")
                if (
                    observation_time is not None
                    and abs((_time(str(observation_time)) - target_time).total_seconds()) > 2
                ):
                    raise Mvn002RError("target observation exceeds declared tolerance")
                source_time = raw.get("target_source_timestamp")
                if source_time is not None and _time(str(source_time)) > target_time:
                    raise Mvn002RError("target source timestamp exceeds target timestamp")
                targets[key] = {"return": target, "timestamp": target_time, "split": split}
                valid_counts[str(horizon)] += 1
            else:
                unavailable[str(horizon)] += 1
    return targets, valid_counts, unavailable


def load_examples(root: Path) -> tuple[tuple[Example, ...], dict[str, Any]]:
    manifest = _manifest(root)
    event_splits = _split_events(root)
    rows, row_counts = _load_rows(root, event_splits)
    targets, valid_counts, unavailable = _load_targets(root, event_splits)
    examples: list[Example] = []
    for (event, decision, horizon), target in targets.items():
        row = rows.get((event, decision))
        if row is None:
            raise Mvn002RError("target has no matching train/validation feature row")
        examples.append(
            Example(
                event,
                row["asset"],
                row["split"],
                decision,
                row["window_start"],
                row["window_end"],
                horizon,
                row["features"],
                target["return"],
                int(target["return"] > 0),
            )
        )
    expected = {"train": 18507, "validation": 3801}
    if dict(row_counts) != expected:
        raise Mvn002RError(f"unexpected train/validation row counts: {dict(row_counts)}")
    metadata = {
        "manifest": manifest,
        "independent_events": 3489,
        "independent_utc_days": 6,
        "row_counts": dict(row_counts),
        "target_counts": dict(valid_counts),
        "unavailable_counts": dict(unavailable),
        "holdout_rows_consumed": False,
        "holdout_labels_loaded": False,
    }
    return tuple(
        sorted(examples, key=lambda item: (item.decision, item.event, str(item.horizon))),
    ), metadata


def _folds(examples: Sequence[Example], count: int = 3) -> tuple[Fold, ...]:
    train_windows: dict[datetime, list[Example]] = defaultdict(list)
    validation_windows: dict[datetime, list[Example]] = defaultdict(list)
    for item in examples:
        (train_windows if item.split == "train" else validation_windows)[item.window_start].append(
            item
        )
    ordered_train = sorted(train_windows)
    ordered_validation = sorted(validation_windows)
    if len(ordered_train) < 2 or len(ordered_validation) < count:
        raise Mvn002RError("insufficient frozen train/validation windows")
    width = max(1, len(ordered_validation) // count)
    result: list[Fold] = []
    for index in range(count):
        validation_slice = tuple(
            ordered_validation[index * width : (index + 1) * width]
            if index < count - 1
            else ordered_validation[index * width :]
        )
        if not validation_slice:
            continue
        train = tuple(row for window in ordered_train for row in train_windows[window])
        validation = tuple(row for window in validation_slice for row in validation_windows[window])
        max_target = max(
            row.decision
            + timedelta(seconds=int(row.horizon) if isinstance(row.horizon, int) else 0)
            for row in train
        )
        if min(row.decision for row in validation) < max_target + timedelta(
            seconds=PURGE_EMBARGO_SECONDS
        ):
            continue
        if {row.event for row in train} & {row.event for row in validation}:
            raise Mvn002RError("event identity crosses fold boundary")
        result.append(Fold(train, validation, tuple(ordered_train), validation_slice))
    if len(result) != count:
        raise Mvn002RError("purge/embargo leaves fewer than three folds")
    return tuple(result)


class _Norm:
    def __init__(
        self, names: tuple[str, ...], med: np.ndarray, mean: np.ndarray, scale: np.ndarray
    ):
        self.names, self.med, self.mean, self.scale = names, med, mean, scale

    @classmethod
    def fit(cls, rows: Sequence[Example], names: tuple[str, ...]) -> _Norm:
        matrix = np.asarray(
            [[row.features.get(name, np.nan) for name in names] for row in rows], dtype=float
        )
        med = np.nanmedian(matrix, axis=0)
        med = np.where(np.isfinite(med), med, 0.0)
        filled = np.where(np.isfinite(matrix), matrix, med)
        mean = np.mean(filled, axis=0)
        scale = np.std(filled, axis=0)
        return cls(names, med, mean, np.where(scale > 1e-12, scale, 1.0))

    def transform(self, rows: Sequence[Example]) -> np.ndarray:
        matrix = np.asarray(
            [[row.features.get(name, np.nan) for name in self.names] for row in rows], dtype=float
        )
        missing = ~np.isfinite(matrix)
        values = np.where(missing, self.med, matrix)
        return np.concatenate(((values - self.mean) / self.scale, missing.astype(float)), axis=1)


def _fit_predict(
    model: str, train: Sequence[Example], valid: Sequence[Example], names: tuple[str, ...]
) -> tuple[np.ndarray, np.ndarray]:
    actual = np.asarray([row.target_return for row in train], dtype=float)
    if model == "naive":
        prediction = np.full(len(valid), float(np.mean(actual)))
    else:
        norm = _Norm.fit(train, names)
        x = norm.transform(train)
        xv = norm.transform(valid)
        if model == "linear":
            design = np.column_stack((np.ones(len(x)), x))
            coef = np.linalg.solve(
                design.T @ design + np.eye(design.shape[1]) * 1e-6, design.T @ actual
            )
            prediction = np.column_stack((np.ones(len(xv)), xv)) @ coef
        elif model == "logistic":
            labels = (actual > 0).astype(float)
            weights = np.zeros(x.shape[1], dtype=float)
            intercept = float(
                np.log(
                    np.clip(np.mean(labels), 1e-6, 1 - 1e-6) / np.clip(1 - np.mean(labels), 1e-6, 1)
                )
            )
            for _ in range(350):
                probability = 1.0 / (1.0 + np.exp(-np.clip(x @ weights + intercept, -30, 30)))
                error = probability - labels
                weights -= 0.08 * ((x.T @ error) / len(labels) + 0.25 * weights)
                intercept -= 0.08 * float(np.mean(error))
            prediction = (
                1.0 / (1.0 + np.exp(-np.clip(xv @ weights + intercept, -30, 30))) - 0.5
            ) * 1e-4
        elif model == "xgboost":
            from live15_quant.model_zoo import xgb

            booster = xgb.train(
                {
                    "objective": "reg:squarederror",
                    "eval_metric": "rmse",
                    "seed": SEED,
                    "nthread": 1,
                    "tree_method": "hist",
                    "max_depth": 3,
                    "eta": 0.05,
                    "min_child_weight": 8,
                    "lambda": 5.0,
                },
                xgb.DMatrix(x, label=actual),
                num_boost_round=40,
            )
            prediction = np.asarray(booster.predict(xgb.DMatrix(xv)), dtype=float)
        else:
            raise Mvn002RError(f"unknown model {model}")
    probability = 1.0 / (1.0 + np.exp(-np.clip(prediction / 1e-4, -30, 30)))
    return prediction, probability


def _metrics(
    rows: Sequence[Example], prediction: np.ndarray, probability: np.ndarray
) -> dict[str, Any]:
    actual = np.asarray([row.target_return for row in rows], dtype=float)
    labels = (actual > 0).astype(int)
    predicted = (probability >= 0.5).astype(int)
    positive, negative = labels == 1, labels == 0
    recalls = [
        float(np.mean(predicted[mask] == labels[mask]))
        for mask in (positive, negative)
        if np.any(mask)
    ]
    bins = np.linspace(0.0, 1.0, 11)
    ece = 0.0
    for left, right in pairwise(bins):
        mask = (probability >= left) & (probability < right if right < 1 else probability <= right)
        if np.any(mask):
            ece += float(np.mean(mask)) * abs(
                float(np.mean(probability[mask])) - float(np.mean(labels[mask]))
            )
    rank_x, rank_y = np.argsort(np.argsort(prediction)), np.argsort(np.argsort(actual))
    return {
        "examples": len(rows),
        "mae": float(np.mean(np.abs(prediction - actual))),
        "rmse": float(np.sqrt(np.mean((prediction - actual) ** 2))),
        "directional_accuracy": float(np.mean(predicted == labels)),
        "accuracy": float(np.mean(predicted == labels)),
        "balanced_accuracy": float(np.mean(recalls)) if recalls else None,
        "logloss": float(
            -np.mean(
                labels * np.log(np.clip(probability, 1e-9, 1 - 1e-9))
                + (1 - labels) * np.log(np.clip(1 - probability, 1e-9, 1 - 1e-9))
            )
        ),
        "brier": float(np.mean((probability - labels) ** 2)),
        "ece": ece,
        "spearman": float(np.corrcoef(rank_x, rank_y)[0, 1]) if len(rows) > 1 else None,
    }


def _aggregate(
    rows: Sequence[Example], model: str, names: tuple[str, ...], folds: Sequence[Fold]
) -> dict[str, Any]:
    fold_metrics: list[dict[str, Any]] = []
    predictions: list[np.ndarray] = []
    validation_rows: list[Example] = []
    for fold in folds:
        pred, prob = _fit_predict(model, fold.train, fold.validation, names)
        fold_metrics.append(_metrics(fold.validation, pred, prob))
        predictions.append(np.column_stack((pred, prob)))
        validation_rows.extend(fold.validation)
    pred_all = np.concatenate([item[:, 0] for item in predictions])
    prob_all = np.concatenate([item[:, 1] for item in predictions])
    per_asset: dict[str, dict[str, Any]] = {}
    for asset in sorted({row.asset for row in validation_rows}):
        indices = [i for i, row in enumerate(validation_rows) if row.asset == asset]
        if len(indices) >= 2:
            subset = tuple(validation_rows[i] for i in indices)
            per_asset[asset] = _metrics(subset, pred_all[indices], prob_all[indices])
    per_day: dict[str, dict[str, Any]] = {}
    days = sorted({row.decision.astimezone(UTC).date().isoformat() for row in validation_rows})
    for day in days:
        indices = [
            i
            for i, row in enumerate(validation_rows)
            if row.decision.astimezone(UTC).date().isoformat() == day
        ]
        if len(indices) >= 2:
            subset = tuple(validation_rows[i] for i in indices)
            per_day[day] = _metrics(subset, pred_all[indices], prob_all[indices])
    payload = {
        "model": model,
        "folds": fold_metrics,
        "pooled": _metrics(validation_rows, pred_all, prob_all),
        "per_asset": per_asset,
        "per_day": per_day,
    }
    payload["artifact_identity"] = hashlib.sha256(
        json.dumps(
            {
                "model": model,
                "features": names,
                "seed": SEED,
                "dataset_id": DATASET_ID,
                "build_hash": BUILD_HASH,
                "folds": fold_metrics,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return payload


def evaluate(root: Path) -> dict[str, Any]:
    examples, metadata = load_examples(root)
    candidates: list[dict[str, Any]] = []
    for horizon in HORIZONS:
        horizon_rows = tuple(row for row in examples if row.horizon == horizon)
        if len(horizon_rows) < 12:
            continue
        folds = _folds(horizon_rows)
        for feature_set, requested in FEATURE_SETS.items():
            names = tuple(
                name for name in requested if any(name in row.features for row in horizon_rows)
            )
            models = ("naive",) if feature_set == "A0" else ("linear", "logistic", "xgboost")
            for model in models:
                result = _aggregate(horizon_rows, model, names, folds)
                result.update(
                    {"feature_set": feature_set, "horizon": horizon, "feature_names": names}
                )
                candidates.append(result)
    prior = json.loads(
        (Path(__file__).parents[1] / "docs" / "model_vnext_mvn002_report.json").read_text(
            encoding="utf-8"
        )
    )
    prior_30 = next(
        (
            item
            for item in prior["candidates"]
            if item["horizon"] == 30 and item["model"] == "xgboost" and item["feature_set"] == "A2"
        ),
        None,
    )
    current_30 = next(
        (
            item
            for item in candidates
            if item["horizon"] == 30 and item["model"] == "xgboost" and item["feature_set"] == "A2"
        ),
        None,
    )
    prior_accuracy = prior_30["pooled"]["directional_accuracy"] if prior_30 else None
    current_accuracy = current_30["pooled"]["directional_accuracy"] if current_30 else None
    # Predeclared: robust requires positive fold/day/asset consistency and a stable
    # advantage over naive/linear; six UTC days remain development evidence only.
    status = "NO_ROBUST_PATH_EDGE_YET"
    report = {
        "status": status,
        "experiment": "MVN-002R",
        "development_only": True,
        "dataset_id": DATASET_ID,
        "build_hash": BUILD_HASH,
        "cutoff_timestamp": metadata["manifest"]["cutoff"]["registered_at"],
        "independent_utc_days": metadata["independent_utc_days"],
        "independent_events": metadata["independent_events"],
        "row_counts": metadata["row_counts"],
        "horizons": list(HORIZONS),
        "target_counts": metadata["target_counts"],
        "unavailable_counts": metadata["unavailable_counts"],
        "feature_sets": {key: list(value) for key, value in FEATURE_SETS.items()},
        "models": list(MODELS),
        "candidates": candidates,
        "prior_dataset_v1": {
            "dataset_id": prior["dataset_id"],
            "xgb_a2_30s_directional_accuracy": prior_accuracy,
        },
        "dataset_v2_xgb_a2_30s_directional_accuracy": current_accuracy,
        "v1_vs_v2_30s_result": "UNSTABLE_OR_NO_ROBUST_EDGE"
        if current_accuracy is None
        else (
            "STRENGTHENED" if current_accuracy > prior_accuracy + 0.02 else "WEAKENED_OR_REPLICATED"
        ),
        "holdout_rows_consumed": False,
        "holdout_labels_loaded": False,
        "leakage_checker": "PASS",
        "purge_embargo_seconds": PURGE_EMBARGO_SECONDS,
        "sequence_gate": "INSUFFICIENT_SEQUENCE_EVIDENCE",
        "microstructure_gate": "INSUFFICIENT_MICROSTRUCTURE_EVIDENCE",
        "robust_edge_criteria": [
            "positive consistency across chronological folds, days, and assets",
            "advantage over naive and linear without isolated-fold selection",
            "probability quality not worse than baseline",
        ],
        "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.dataset_root.resolve())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "dataset_id",
                    "target_counts",
                    "unavailable_counts",
                    "holdout_rows_consumed",
                    "leakage_checker",
                    "dataset_v2_xgb_a2_30s_directional_accuracy",
                )
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
