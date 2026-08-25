"""MVN-002 structured multi-horizon path baseline.

This module is deliberately offline-only.  It reads the immutable Dataset v1
artifact's *train* partition and builds targets only from explicitly observed
future rows in the same event.  Missing horizons are retained as typed
``future_observation_unavailable`` records; no interpolation, forward fill, or
settlement label is used.  The module has no runtime, Paper, Recorder, or
execution imports.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from live15_quant.model_vnext_contract import (
    DATASET_V1_ID,
    PATH_HORIZONS_SECONDS,
    TERMINAL_WINDOW_END_HORIZON,
    ContractSide,
    DecisionTimeContract,
    LeakageChecker,
    PathObservation,
    PathTargetSpec,
    TargetUnavailableError,
    build_path_target,
    required_purge_embargo_seconds,
)

MVN002_VERSION = "1.0.0"
DEVELOPMENT_STATUS = "DEVELOPMENT_ONLY"
NO_EDGE_STATUS = "NO_ROBUST_PATH_EDGE_YET"
SEQUENCE_GATE = "INSUFFICIENT_SEQUENCE_EVIDENCE"
PURGE_EMBARGO_SECONDS = required_purge_embargo_seconds(
    max_lookback_seconds=300, max_horizon_seconds=300
)

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


class Mvn002Error(RuntimeError):
    """An MVN-002 lineage, target, or model invariant failed."""


@dataclass(frozen=True, slots=True)
class PathExample:
    event_id: str
    asset: str
    ticker: str
    decision_timestamp: datetime
    target_timestamp: datetime | None
    window_start: datetime
    window_end: datetime
    horizon: int | str
    features: Mapping[str, float | None]
    underlying_price: float
    future_underlying_price: float | None
    missing_reason: str | None = None

    @property
    def target_return(self) -> float | None:
        if self.future_underlying_price is None or self.underlying_price <= 0:
            return None
        return self.future_underlying_price / self.underlying_price - 1.0

    @property
    def direction(self) -> int | None:
        value = self.target_return
        return None if value is None else int(value > 0)


@dataclass(frozen=True, slots=True)
class Fold:
    train: tuple[PathExample, ...]
    validation: tuple[PathExample, ...]
    train_windows: tuple[datetime, ...]
    validation_windows: tuple[datetime, ...]


@dataclass(frozen=True, slots=True)
class ModelMetrics:
    examples: int
    mae: float | None
    rmse: float | None
    directional_accuracy: float | None
    spearman: float | None
    accuracy: float | None
    balanced_accuracy: float | None
    logloss: float | None
    brier: float | None


@dataclass(frozen=True, slots=True)
class CandidateResult:
    model: str
    feature_set: str
    horizon: int | str
    folds: tuple[ModelMetrics, ...]
    pooled: ModelMetrics | None
    per_asset: Mapping[str, ModelMetrics]
    per_day: Mapping[str, ModelMetrics]
    artifact_identity: str


@dataclass(frozen=True, slots=True)
class Mvn002Report:
    status: str
    dataset_id: str
    build_hash: str
    independent_utc_days: tuple[str, ...]
    independent_events: int
    usable_target_events: int
    horizons: tuple[int | str, ...]
    target_counts: Mapping[str, int]
    unavailable_counts: Mapping[str, int]
    feature_sets: Mapping[str, tuple[str, ...]]
    models: tuple[str, ...]
    candidates: tuple[CandidateResult, ...]
    final_test_rows_consumed: bool
    sequence_gate: str
    evidence_note: str
    code_sha: str

    def payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["target_counts"] = dict(self.target_counts)
        payload["unavailable_counts"] = dict(self.unavailable_counts)
        return payload


def _event_id(row: Any) -> str:
    return f"{row.ticker}:{row.window_start.isoformat()}:{row.window_end.isoformat()}"


def _feature_map(row: Any, feature_names: Sequence[str]) -> dict[str, float | None]:
    return {
        name: (None if value is None else float(value))
        for name, value in zip(feature_names, row.values, strict=True)
    }


def _row_price(row: Any, feature_names: Sequence[str]) -> float | None:
    try:
        value = row.values[feature_names.index("underlying_price")]
    except (ValueError, IndexError):
        return None
    return None if value is None else float(value)


def _validate_certified_train_only(dataset: Any) -> None:
    if dataset.dataset_id != DATASET_V1_ID:
        raise Mvn002Error(f"unexpected Dataset v1 identity: {dataset.dataset_id}")
    if set(dataset.splits) != {"train", "validation", "test"}:
        raise Mvn002Error("certified dataset split contract is incomplete")
    LeakageChecker().check_final_test(
        DATASET_V1_ID, purpose="MVN-002 candidate selection", rows_consumed=False
    )
    LeakageChecker().check_feature_names(dataset.feature_names)
    LeakageChecker().check_splits(dataset.splits)
    LeakageChecker().check_normalization("train")


def build_path_examples(dataset: Any) -> tuple[PathExample, ...]:
    """Build every declared horizon, preserving unavailable target reasons."""

    _validate_certified_train_only(dataset)
    names = tuple(dataset.feature_names)
    if "underlying_price" not in names:
        raise Mvn002Error("certified schema lacks underlying_price")
    grouped: dict[str, list[Any]] = defaultdict(list)
    # This is the only Dataset v1 partition read by the candidate path.
    for row in dataset.splits["train"]:
        grouped[_event_id(row)].append(row)
    result: list[PathExample] = []
    for event_id, rows in sorted(grouped.items()):
        ordered = sorted(rows, key=lambda row: row.decision_timestamp)
        for row in ordered:
            base = _row_price(row, names)
            features = _feature_map(row, names)
            for horizon in (*PATH_HORIZONS_SECONDS, TERMINAL_WINDOW_END_HORIZON):
                spec = PathTargetSpec(horizon)
                target_time = (
                    row.window_end
                    if horizon == TERMINAL_WINDOW_END_HORIZON
                    else row.decision_timestamp + timedelta(seconds=horizon)
                )
                reason: str | None = None
                target_row = None
                if base is None or not math.isfinite(base) or base <= 0:
                    reason = "invalid_target_base"
                else:
                    candidates = tuple(
                        candidate
                        for candidate in ordered
                        if row.decision_timestamp < candidate.decision_timestamp <= row.window_end
                        and abs((candidate.decision_timestamp - target_time).total_seconds()) <= 2
                    )
                    target_row = min(
                        candidates,
                        key=lambda candidate: (
                            abs((candidate.decision_timestamp - target_time).total_seconds()),
                            candidate.decision_timestamp,
                        ),
                        default=None,
                    )
                    if target_row is None:
                        # Contract lookup is exact/tolerance based.  Never use
                        # the nearest row merely because it is convenient.
                        reason = "future_observation_unavailable"
                    else:
                        future = _row_price(target_row, names)
                        if future is None or not math.isfinite(future) or future <= 0:
                            reason = "future_observation_invalid"
                        else:
                            try:
                                build_path_target(
                                    _contract_for_row(row, base),
                                    spec,
                                    base_value=_decimal(base),
                                    observations=(
                                        PathObservation(
                                            target_row.decision_timestamp, _decimal(future)
                                        ),
                                    ),
                                )
                            except TargetUnavailableError:
                                reason = "future_observation_unavailable"
                result.append(
                    PathExample(
                        event_id,
                        row.asset,
                        row.ticker,
                        row.decision_timestamp,
                        None if reason else target_row.decision_timestamp,
                        row.window_start,
                        row.window_end,
                        horizon,
                        features,
                        float(base or 0.0),
                        None if reason else _row_price(target_row, names),
                        reason,
                    )
                )
    return tuple(
        sorted(result, key=lambda item: (item.decision_timestamp, item.event_id, str(item.horizon)))
    )


def _decimal(value: float):
    from decimal import Decimal

    return Decimal(str(value))


def _contract_for_row(row: Any, base: float):
    return DecisionTimeContract(
        _event_id(row),
        row.ticker,
        row.window_start,
        row.window_end,
        row.decision_timestamp,
        _decimal(base),
        ContractSide.YES,
        (300,),
    )


def chronological_folds(examples: Iterable[PathExample], count: int = 3) -> tuple[Fold, ...]:
    usable = [item for item in examples if item.missing_reason is None]
    windows: dict[datetime, list[PathExample]] = defaultdict(list)
    for item in usable:
        windows[item.window_start].append(item)
    ordered = sorted(windows)
    if len(ordered) < count + 2:
        raise Mvn002Error("insufficient chronological windows for MVN-002 folds")
    first_train_end = max(2, len(ordered) // 3)
    width = max(1, (len(ordered) - first_train_end) // count)
    folds: list[Fold] = []
    cursor = first_train_end
    for _index in range(count):
        train_end = cursor
        train_windows = tuple(ordered[:train_end])
        max_train_target = max(
            item.target_timestamp
            for window in train_windows
            for item in windows[window]
            if item.target_timestamp
        )
        while cursor < len(ordered):
            validation_start = ordered[cursor]
            validation_rows = windows[validation_start]
            if min(
                item.decision_timestamp for item in validation_rows
            ) >= max_train_target + timedelta(seconds=PURGE_EMBARGO_SECONDS):
                break
            cursor += 1
        validation_end = min(len(ordered), cursor + width)
        validation_windows = tuple(ordered[cursor:validation_end])
        if not validation_windows:
            continue
        train = tuple(item for window in train_windows for item in windows[window])
        validation = tuple(item for window in validation_windows for item in windows[window])
        if max(item.target_timestamp for item in train if item.target_timestamp) + timedelta(
            seconds=PURGE_EMBARGO_SECONDS
        ) > min(item.decision_timestamp for item in validation):
            continue
        if {item.event_id for item in train} & {item.event_id for item in validation}:
            raise Mvn002Error("event identity crosses MVN-002 folds")
        folds.append(Fold(train, validation, train_windows, validation_windows))
        cursor = validation_end
    if len(folds) != count:
        raise Mvn002Error("purge/embargo leaves fewer than the requested chronological folds")
    return tuple(folds)


class _Normalizer:
    def __init__(
        self, names: tuple[str, ...], medians: np.ndarray, means: np.ndarray, scales: np.ndarray
    ):
        self.names, self.medians, self.means, self.scales = names, medians, means, scales

    @classmethod
    def fit(cls, rows: Sequence[PathExample], names: tuple[str, ...]) -> _Normalizer:
        matrix = np.asarray(
            [[item.features.get(name, np.nan) for name in names] for item in rows], dtype=float
        )
        med = np.nanmedian(matrix, axis=0)
        med = np.where(np.isfinite(med), med, 0.0)
        filled = np.where(np.isfinite(matrix), matrix, med)
        mean = np.mean(filled, axis=0)
        scale = np.std(filled, axis=0)
        scale = np.where(scale > 1e-12, scale, 1.0)
        return cls(names, med, mean, scale)

    def transform(self, rows: Sequence[PathExample]) -> np.ndarray:
        matrix = np.asarray(
            [[item.features.get(name, np.nan) for name in self.names] for item in rows], dtype=float
        )
        missing = ~np.isfinite(matrix)
        values = np.where(missing, self.medians, matrix)
        values = (values - self.means) / self.scales
        return np.concatenate((values, missing.astype(float)), axis=1)


def _fit_linear(
    rows: Sequence[PathExample], names: tuple[str, ...]
) -> tuple[_Normalizer, np.ndarray]:
    normalizer = _Normalizer.fit(rows, names)
    matrix = normalizer.transform(rows)
    target = np.asarray([item.target_return for item in rows], dtype=float)
    design = np.column_stack((np.ones(len(rows)), matrix))
    coef = np.linalg.solve(design.T @ design + np.eye(design.shape[1]) * 1e-6, design.T @ target)
    return normalizer, coef


def _fit_logistic(
    rows: Sequence[PathExample], names: tuple[str, ...], seed: int
) -> tuple[_Normalizer, np.ndarray, float]:
    del seed
    normalizer = _Normalizer.fit(rows, names)
    matrix = normalizer.transform(rows)
    labels = np.asarray([item.direction for item in rows], dtype=float)
    weights = np.zeros(matrix.shape[1], dtype=float)
    intercept = float(
        np.log(np.clip(np.mean(labels), 1e-6, 1 - 1e-6) / np.clip(1 - np.mean(labels), 1e-6, 1))
    )
    for _ in range(350):
        probability = 1.0 / (1.0 + np.exp(-np.clip(matrix @ weights + intercept, -30, 30)))
        error = probability - labels
        weights -= 0.08 * ((matrix.T @ error) / len(labels) + 0.25 * weights)
        intercept -= 0.08 * float(np.mean(error))
    return normalizer, weights, intercept


def _fit_xgb(
    rows: Sequence[PathExample], names: tuple[str, ...], seed: int
) -> tuple[_Normalizer, Any]:
    from live15_quant.model_zoo import xgb

    normalizer = _Normalizer.fit(rows, names)
    matrix = normalizer.transform(rows)
    target = np.asarray([item.target_return for item in rows], dtype=float)
    booster = xgb.train(
        {
            "objective": "reg:squarederror",
            "eval_metric": "rmse",
            "seed": seed,
            "nthread": 1,
            "tree_method": "hist",
            "max_depth": 3,
            "eta": 0.05,
            "min_child_weight": 8,
            "lambda": 5.0,
        },
        xgb.DMatrix(matrix, label=target),
        num_boost_round=40,
    )
    return normalizer, booster


def load_train_only_dataset(path: Path) -> Any:
    """Load Dataset v1 metadata and train rows without materializing test rows."""

    from live15_quant.model_zoo import CertifiedDataset, _parse_dataset_row

    root = path.resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("dataset_id") != DATASET_V1_ID:
        raise Mvn002Error("unexpected Dataset v1 identity")
    splits = json.loads((root / "splits.json").read_text(encoding="utf-8"))
    split_root = splits.get("splits")
    if not isinstance(split_root, dict) or not isinstance(split_root.get("train"), dict):
        raise Mvn002Error("Dataset v1 split metadata is malformed")
    train_tickers = set(split_root["train"].get("events", ()))
    schema = manifest.get("feature_schema")
    names = tuple(schema.get("order", ())) if isinstance(schema, dict) else ()
    rows: list[Any] = []
    with (root / "training_rows.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            raw = json.loads(line)
            if raw.get("split") != "train":
                continue
            row = _parse_dataset_row(raw, names)
            if row.ticker not in train_tickers:
                raise Mvn002Error("train row is not in the certified train event set")
            rows.append(row)
    return CertifiedDataset(
        root,
        manifest,
        names,
        {"train": tuple(rows), "validation": (), "test": ()},
        frozenset(),
        frozenset(),
    )


def _predict(
    model: tuple[str, Any], rows: Sequence[PathExample], names: tuple[str, ...]
) -> tuple[np.ndarray, np.ndarray]:
    family, state = model
    if family == "naive":
        mean = float(state)
        prediction = np.full(len(rows), mean)
        probability = 1.0 / (1.0 + np.exp(-np.clip(prediction / 1e-4, -30, 30)))
    elif family == "linear":
        normalizer, coef = state
        prediction = np.column_stack((np.ones(len(rows)), normalizer.transform(rows))) @ coef
        probability = 1.0 / (1.0 + np.exp(-np.clip(prediction / 1e-4, -30, 30)))
    elif family == "logistic":
        normalizer, weights, intercept = state
        probability = 1.0 / (
            1.0 + np.exp(-np.clip(normalizer.transform(rows) @ weights + intercept, -30, 30))
        )
        prediction = (probability - 0.5) * 1e-4
    elif family == "xgboost":
        normalizer, booster = state
        prediction = np.asarray(
            booster.predict(
                __import__("live15_quant.model_zoo", fromlist=["xgb"]).xgb.DMatrix(
                    normalizer.transform(rows)
                )
            ),
            dtype=float,
        )
        probability = 1.0 / (1.0 + np.exp(-np.clip(prediction / 1e-4, -30, 30)))
    else:
        raise Mvn002Error(f"unknown model family {family}")
    return prediction, probability


def _metrics(
    rows: Sequence[PathExample], prediction: np.ndarray, probability: np.ndarray
) -> ModelMetrics:
    actual = np.asarray([item.target_return for item in rows], dtype=float)
    labels = (actual > 0).astype(int)
    predicted_labels = (probability >= 0.5).astype(int)
    mae = float(np.mean(np.abs(prediction - actual)))
    rmse = float(np.sqrt(np.mean((prediction - actual) ** 2)))
    accuracy = float(np.mean(predicted_labels == labels))
    positives = labels == 1
    negatives = ~positives
    recalls = [
        float(np.mean(predicted_labels[mask] == labels[mask]))
        for mask in (positives, negatives)
        if np.any(mask)
    ]
    balanced = float(np.mean(recalls)) if recalls else None
    logloss = float(
        -np.mean(
            labels * np.log(np.clip(probability, 1e-9, 1 - 1e-9))
            + (1 - labels) * np.log(np.clip(1 - probability, 1e-9, 1 - 1e-9))
        )
    )
    brier = float(np.mean((probability - labels) ** 2))
    rank_x = np.argsort(np.argsort(prediction))
    rank_y = np.argsort(np.argsort(actual))
    spearman = float(np.corrcoef(rank_x, rank_y)[0, 1]) if len(rows) > 1 else None
    return ModelMetrics(
        len(rows), mae, rmse, accuracy, spearman, accuracy, balanced, logloss, brier
    )


def _fit_model(
    model_name: str, train: Sequence[PathExample], names: tuple[str, ...], seed: int
) -> tuple[str, Any]:
    if model_name == "naive":
        return ("naive", float(np.mean([item.target_return for item in train])))
    elif model_name == "linear":
        return ("linear", _fit_linear(train, names))
    elif model_name == "logistic":
        return ("logistic", _fit_logistic(train, names, seed))
    elif model_name == "xgboost":
        return ("xgboost", _fit_xgb(train, names, seed))
    else:
        raise Mvn002Error(f"unknown model {model_name}")


def _train_one(
    model_name: str,
    train: Sequence[PathExample],
    validation: Sequence[PathExample],
    names: tuple[str, ...],
    seed: int,
) -> tuple[ModelMetrics, tuple[str, Any]]:
    state = _fit_model(model_name, train, names, seed)
    return _metrics(validation, *_predict(state, validation, names)), state


def _identity(
    model: str,
    feature_set: str,
    horizon: int | str,
    names: tuple[str, ...],
    seed: int,
    dataset_id: str,
    build_hash: str,
    folds: Sequence[ModelMetrics],
) -> str:
    payload = json.dumps(
        {
            "model": model,
            "feature_set": feature_set,
            "horizon": horizon,
            "features": names,
            "seed": seed,
            "dataset_id": dataset_id,
            "build_hash": build_hash,
            "folds": [asdict(item) for item in folds],
        },
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def evaluate(dataset: Any, *, folds: int = 3, seed: int = 20260826) -> Mvn002Report:
    """Run fixed, train-only development folds; never reads Dataset v1 test rows."""

    examples = build_path_examples(dataset)
    valid = tuple(item for item in examples if item.missing_reason is None)
    target_counts = Counter(str(item.horizon) for item in valid)
    unavailable_counts = Counter(str(item.horizon) for item in examples if item.missing_reason)
    days = tuple(
        sorted({item.decision_timestamp.astimezone(UTC).date().isoformat() for item in valid})
    )
    events = len({item.event_id for item in valid})
    candidates: list[CandidateResult] = []
    for horizon in (*PATH_HORIZONS_SECONDS, TERMINAL_WINDOW_END_HORIZON):
        horizon_rows = tuple(item for item in valid if item.horizon == horizon)
        if len(horizon_rows) < 12:
            continue
        try:
            horizon_folds = chronological_folds(horizon_rows, folds)
        except Mvn002Error:
            continue
        for feature_set, feature_names in FEATURE_SETS.items():
            selected = tuple(name for name in feature_names if name in dataset.feature_names)
            model_names = ("naive",) if feature_set == "A0" else ("linear", "logistic", "xgboost")
            for model_name in model_names:
                fold_metrics_list: list[ModelMetrics] = []
                for fold in horizon_folds:
                    fold_metric, _ = _train_one(
                        model_name, fold.train, fold.validation, selected, seed
                    )
                    fold_metrics_list.append(fold_metric)
                fold_metrics = tuple(fold_metrics_list)
                all_train = tuple(item for fold in horizon_folds for item in fold.train)
                all_validation = tuple(item for fold in horizon_folds for item in fold.validation)
                pooled, pooled_state = _train_one(
                    model_name, all_train, all_validation, selected, seed
                )
                per_asset: dict[str, ModelMetrics] = {}
                for asset in sorted({item.asset for item in all_validation}):
                    rows = tuple(item for item in all_validation if item.asset == asset)
                    if len(rows) >= 2:
                        per_asset[asset] = _metrics(rows, *_predict(pooled_state, rows, selected))
                per_day: dict[str, ModelMetrics] = {}
                for day in sorted(
                    {
                        item.decision_timestamp.astimezone(UTC).date().isoformat()
                        for item in all_validation
                    }
                ):
                    rows = tuple(
                        item
                        for item in all_validation
                        if item.decision_timestamp.astimezone(UTC).date().isoformat() == day
                    )
                    if len(rows) >= 2:
                        per_day[day] = _metrics(rows, *_predict(pooled_state, rows, selected))
                candidates.append(
                    CandidateResult(
                        model_name,
                        feature_set,
                        horizon,
                        fold_metrics,
                        pooled,
                        per_asset,
                        per_day,
                        _identity(
                            model_name,
                            feature_set,
                            horizon,
                            selected,
                            seed,
                            dataset.dataset_id,
                            dataset.deterministic_build_hash,
                            fold_metrics,
                        ),
                    )
                )
    manifest_events = int(
        dataset.manifest.get("diagnostics", {}).get("overall", {}).get("events_count", events)
    )
    status = NO_EDGE_STATUS
    return Mvn002Report(
        status,
        dataset.dataset_id,
        dataset.deterministic_build_hash,
        days,
        manifest_events,
        events,
        (*PATH_HORIZONS_SECONDS, TERMINAL_WINDOW_END_HORIZON),
        target_counts,
        unavailable_counts,
        FEATURE_SETS,
        ("naive", "linear", "logistic", "xgboost"),
        tuple(candidates),
        False,
        SEQUENCE_GATE,
        (
            "Development folds trained from sparse exact future rows; "
            "no robust edge or promotion claim"
        ),
        hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    )


def write_report(report: Mvn002Report, path: Path) -> None:
    """Persist a deterministic lightweight research manifest/report."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.payload(), sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8"
    )
