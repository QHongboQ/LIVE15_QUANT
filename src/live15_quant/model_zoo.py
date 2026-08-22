"""Leak-safe, deterministic offline Model Zoo v1 for certified Dataset artifacts.

This module deliberately consumes only an immutable Dataset v1 JSONL artifact.  It
does not open the recorder database, connect to any venue, or create trading actions.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import fmean
from typing import Any

import numpy as np

from live15_quant.certified_dataset import LABEL_SCHEMA_VERSION, ModelDatasetLineage
from live15_quant.execution import ExecutionAction
from live15_quant.feature_registry import FEATURE_REGISTRY, FEATURE_SCHEMA_VERSION
from live15_quant.fees import KalshiTakerFeeModel
from live15_quant.recorder_control import process_alive

_XGBOOST_DLL_DIRECTORY: Any | None = None


def _load_xgboost() -> Any:
    """Load the pinned native wheel with the venv-local Windows OpenMP runtime."""

    # The pinned intel-openmp wheel installs vcomp140.dll in the venv root.
    # Register it only for this offline model process; no system-wide DLL installation is used.
    global _XGBOOST_DLL_DIRECTORY
    if os.name == "nt" and hasattr(os, "add_dll_directory") and _XGBOOST_DLL_DIRECTORY is None:
        _XGBOOST_DLL_DIRECTORY = os.add_dll_directory(sys.prefix)
    import xgboost

    return xgboost


xgb = _load_xgboost()

MODEL_ZOO_VERSION = "1.0.0"
MODEL_ARTIFACT_VERSION = "1.0.0"
CALIBRATION_POLICY_VERSION = "validation-platt-or-identity-v1"
EDGE_POLICY_VERSION = "fixed-executable-ask-edge-v1"
DEFAULT_SEED = 20260823
OOS_NOT_ELIGIBLE = "OOS_NOT_ELIGIBLE"


class ModelZooError(RuntimeError):
    """A certified dataset or model artifact invariant was violated."""


@dataclass(frozen=True, slots=True)
class ModelZooConfig:
    """Fixed, intentionally small Model Zoo v1 configuration."""

    seed: int = DEFAULT_SEED
    reliability_bins: int = 10
    fixed_edge_threshold: Decimal = Decimal("0.05")
    logistic_iterations: int = 1_500
    logistic_learning_rate: float = 0.08
    logistic_l2: float = 0.25
    xgboost_rounds: int = 80
    xgboost_max_depth: int = 3
    xgboost_learning_rate: float = 0.05
    internal_walk_forward_folds: int = 3

    def __post_init__(self) -> None:
        if self.seed < 0 or self.reliability_bins < 2 or self.internal_walk_forward_folds < 1:
            raise ValueError("invalid deterministic Model Zoo configuration")
        if not Decimal(0) < self.fixed_edge_threshold < Decimal(1):
            raise ValueError("edge threshold must be between zero and one")
        if min(self.logistic_iterations, self.xgboost_rounds, self.xgboost_max_depth) <= 0:
            raise ValueError("model iteration/depth values must be positive")

    def payload(self) -> dict[str, object]:
        value = asdict(self)
        value["fixed_edge_threshold"] = str(self.fixed_edge_threshold)
        return value


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class Preprocessor:
    """Train-only median imputation plus explicit missing indicators and scaling."""

    feature_names: tuple[str, ...]
    medians: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    entirely_missing_in_train: tuple[bool, ...]

    @classmethod
    def fit(cls, rows: tuple[DatasetExample, ...], feature_names: tuple[str, ...]) -> Preprocessor:
        if not rows:
            raise ModelZooError("preprocessing requires non-empty training rows")
        columns: list[list[float]] = [[] for _ in feature_names]
        for row in rows:
            for index, value in enumerate(row.values):
                if value is not None:
                    columns[index].append(float(value))
        medians: list[float] = []
        means: list[float] = []
        scales: list[float] = []
        entirely_missing: list[bool] = []
        for column in columns:
            if not column:
                # The raw Dataset value remains null. The numeric model matrix uses an explicit
                # neutral placeholder solely because its paired missing indicator is always one.
                # This avoids silently treating absent evidence as an observed zero.
                medians.append(0.0)
                means.append(0.0)
                scales.append(1.0)
                entirely_missing.append(True)
                continue
            ordered = sorted(column)
            middle = len(ordered) // 2
            median = (
                ordered[middle]
                if len(ordered) % 2
                else (ordered[middle - 1] + ordered[middle]) / 2.0
            )
            imputed = column
            mean = fmean(imputed)
            variance = fmean((value - mean) ** 2 for value in imputed)
            medians.append(median)
            means.append(mean)
            scales.append(math.sqrt(variance) if variance > 1e-24 else 1.0)
            entirely_missing.append(False)
        return cls(
            feature_names,
            tuple(medians),
            tuple(means),
            tuple(scales),
            tuple(entirely_missing),
        )

    def transform(self, rows: tuple[DatasetExample, ...]) -> np.ndarray:
        matrix = np.empty((len(rows), len(self.feature_names) * 2), dtype=np.float64)
        for row_index, row in enumerate(rows):
            if len(row.values) != len(self.feature_names):
                raise ModelZooError("row feature order differs from certified schema")
            for index, value in enumerate(row.values):
                # A feature absent from all train rows is not allowed to become an
                # out-of-distribution signal merely because it appears later.
                missing = value is None or self.entirely_missing_in_train[index]
                numeric = self.medians[index] if missing else float(value)
                matrix[row_index, index] = (numeric - self.means[index]) / self.scales[index]
                matrix[row_index, len(self.feature_names) + index] = 1.0 if missing else 0.0
        return matrix

    def payload(self) -> dict[str, object]:
        return {
            "policy": "train-only-median-imputation-plus-missing-indicators-and-zscore-v1",
            "feature_names": list(self.feature_names),
            "medians": list(self.medians),
            "means": list(self.means),
            "scales": list(self.scales),
            "entirely_missing_in_train": list(self.entirely_missing_in_train),
            "all_missing_policy": "numeric placeholder only with explicit missing indicator",
            "output_features": [
                *self.feature_names,
                *(f"{name}__missing" for name in self.feature_names),
            ],
        }


@dataclass(frozen=True, slots=True)
class PlattCalibrator:
    coefficient: float
    intercept: float

    @classmethod
    def fit(cls, probabilities: np.ndarray, labels: np.ndarray) -> PlattCalibrator:
        if len(probabilities) != len(labels) or len(probabilities) < 20:
            raise ModelZooError("Platt calibration requires at least twenty validation rows")
        if len(set(int(value) for value in labels)) != 2:
            raise ModelZooError("Platt calibration requires both validation labels")
        x = np.asarray([_logit(float(value)) for value in probabilities], dtype=np.float64)
        y = labels.astype(np.float64)
        coefficient = 1.0
        intercept = 0.0
        for _ in range(1_500):
            predicted = _sigmoid_array(coefficient * x + intercept)
            error = predicted - y
            gradient_coefficient = float(np.mean(error * x)) + 0.001 * coefficient
            gradient_intercept = float(np.mean(error))
            coefficient -= 0.05 * gradient_coefficient
            intercept -= 0.05 * gradient_intercept
        return cls(coefficient, intercept)

    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        values = np.asarray([_logit(float(value)) for value in probabilities], dtype=np.float64)
        return _sigmoid_array(self.coefficient * values + self.intercept)

    def payload(self) -> dict[str, object]:
        return {
            "method": "platt_sigmoid",
            "coefficient": self.coefficient,
            "intercept": self.intercept,
            "fit_split": "validation",
        }


@dataclass(slots=True)
class FittedModel:
    name: str
    family: str
    feature_names: tuple[str, ...]
    preprocessor: Preprocessor | None
    model: object | None
    market_probability_index: int | None
    calibration: PlattCalibrator | None = None
    calibration_method: str = "identity"
    hyperparameters: dict[str, object] | None = None

    def predict(self, rows: tuple[DatasetExample, ...]) -> np.ndarray:
        if self.family == "market_implied":
            if self.market_probability_index is None:
                raise ModelZooError("market-implied model lacks certified feature index")
            values = []
            for row in rows:
                value = row.values[self.market_probability_index]
                if value is None:
                    raise ModelZooError("market-implied probability is missing in a certified row")
                values.append(float(value))
            probabilities = np.asarray(values, dtype=np.float64)
        elif self.family == "logistic_l2":
            if self.preprocessor is None or not isinstance(self.model, tuple):
                raise ModelZooError("logistic model state is malformed")
            weights, intercept = self.model
            probabilities = _sigmoid_array(self.preprocessor.transform(rows) @ weights + intercept)
        elif self.family == "xgboost":
            if self.preprocessor is None or not isinstance(self.model, xgb.Booster):
                raise ModelZooError("XGBoost model state is malformed")
            probabilities = self.model.predict(xgb.DMatrix(self.preprocessor.transform(rows)))
        else:
            raise ModelZooError("unknown model family")
        probabilities = np.clip(np.asarray(probabilities, dtype=np.float64), 1e-6, 1 - 1e-6)
        return (
            self.calibration.transform(probabilities)
            if self.calibration is not None
            else probabilities
        )

    def model_bytes(self) -> bytes:
        if self.family == "market_implied":
            return _canonical_json(
                {
                    "family": self.family,
                    "feature": "market_probability_midpoint",
                    "calibration": self.calibration_method,
                }
            ).encode("utf-8")
        if self.family == "logistic_l2":
            assert isinstance(self.model, tuple)
            weights, intercept = self.model
            return _canonical_json(
                {
                    "family": self.family,
                    "weights": [float(value) for value in weights],
                    "intercept": float(intercept),
                    "calibration": self.calibration.payload() if self.calibration else None,
                }
            ).encode("utf-8")
        if self.family == "xgboost":
            assert isinstance(self.model, xgb.Booster)
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
                temporary = Path(handle.name)
            try:
                self.model.save_model(temporary)
                return temporary.read_bytes()
            finally:
                temporary.unlink(missing_ok=True)
        raise ModelZooError("unknown model family")

    def payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "family": self.family,
            "hyperparameters": self.hyperparameters or {},
            "preprocessing": self.preprocessor.payload() if self.preprocessor else None,
            "calibration": self.calibration.payload()
            if self.calibration
            else {
                "method": "identity",
                "fit_split": None,
            },
        }


@dataclass(frozen=True, slots=True)
class ModelEvaluation:
    metrics: dict[str, object]
    trades: dict[str, object]
    per_asset: dict[str, object]
    per_bucket: dict[str, object]

    def payload(self) -> dict[str, object]:
        return {
            "metrics": self.metrics,
            "trades": self.trades,
            "per_asset": self.per_asset,
            "per_decision_bucket": self.per_bucket,
        }


@dataclass(frozen=True, slots=True)
class ModelZooSummary:
    zoo_id: str
    artifact_path: Path
    dataset_id: str
    champion_model_id: str | None
    status: str
    model_ids: dict[str, str]
    reused_existing_artifact: bool


def load_certified_dataset(path: Path) -> CertifiedDataset:
    """Read and verify immutable Dataset v1 files without touching raw truth."""

    root = path.resolve()
    manifest = _read_json(root / "manifest.json")
    if manifest.get("artifact_format") != "live15-certified-dataset-jsonl-v1":
        raise ModelZooError("Model Zoo requires a certified Dataset v1 artifact")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ModelZooError("Dataset v1 artifact hashes are malformed")
    for name in ("training_rows.jsonl", "splits.json"):
        item = artifacts.get(name)
        if not isinstance(item, dict) or _sha256_file(root / name) != item.get("sha256"):
            raise ModelZooError("Dataset v1 immutable artifact hash mismatch")
    feature_schema = manifest.get("feature_schema")
    if (
        not isinstance(feature_schema, dict)
        or feature_schema.get("version") != FEATURE_SCHEMA_VERSION
    ):
        raise ModelZooError("Dataset v1 feature schema version is not certified")
    names = feature_schema.get("order")
    expected_names = tuple(definition.name for definition in FEATURE_REGISTRY)
    if not isinstance(names, list) or tuple(names) != expected_names:
        raise ModelZooError("Dataset v1 feature order is not the certified registry")
    label_schema = manifest.get("label_schema")
    if not isinstance(label_schema, dict) or label_schema.get("version") != LABEL_SCHEMA_VERSION:
        raise ModelZooError("Dataset v1 label schema version is not certified")
    splits = _read_json(root / "splits.json")
    split_root = splits.get("splits")
    if not isinstance(split_root, dict):
        raise ModelZooError("Dataset v1 split definition is malformed")
    split_by_ticker: dict[str, str] = {}
    for split in ("train", "validation", "test"):
        payload = split_root.get(split)
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise ModelZooError("Dataset v1 split is incomplete")
        for ticker in payload["events"]:
            if not isinstance(ticker, str) or ticker in split_by_ticker:
                raise ModelZooError("Dataset v1 event identity crosses splits")
            split_by_ticker[ticker] = split
    rows_by_split: dict[str, list[DatasetExample]] = {name: [] for name in split_root}
    for line in (root / "training_rows.jsonl").read_text(encoding="utf-8").splitlines():
        row = _read_json_text(line)
        example = _parse_dataset_row(row, expected_names)
        split = row.get("split")
        if split != split_by_ticker.get(example.ticker):
            raise ModelZooError("Dataset v1 row split does not match its event identity")
        rows_by_split[str(split)].append(example)
    frozen_splits = {
        name: tuple(sorted(values, key=_example_sort_key)) for name, values in rows_by_split.items()
    }
    _validate_chronology(frozen_splits)
    eligibility = _asset_oos_eligibility(manifest)
    return CertifiedDataset(
        root=root,
        manifest=manifest,
        feature_names=expected_names,
        splits=frozen_splits,
        oos_assets=frozenset(asset for asset, value in eligibility.items() if value),
        train_only_assets=frozenset(asset for asset, value in eligibility.items() if not value),
    )


class ModelZooV1:
    """Train a small, fully frozen Model Zoo and reveal Dataset v1 test exactly once."""

    def __init__(
        self, dataset: CertifiedDataset, artifact_root: Path, config: ModelZooConfig
    ) -> None:
        self.dataset = dataset
        self.artifact_root = artifact_root.resolve()
        self.config = config

    def build(self) -> ModelZooSummary:
        train = _formal_rows(self.dataset.splits["train"], self.dataset.oos_assets)
        validation = _formal_rows(self.dataset.splits["validation"], self.dataset.oos_assets)
        test = _formal_rows(self.dataset.splits["test"], self.dataset.oos_assets)
        if not train or not validation or not test:
            raise ModelZooError(
                "formal OOS model evaluation requires non-empty train/validation/test"
            )
        np.random.seed(self.config.seed)

        baseline = _fit_market_implied(self.dataset.feature_names)
        logistic = _fit_logistic(train, self.dataset.feature_names, self.config)
        boosted = _fit_xgboost(train, self.dataset.feature_names, self.config)
        raw_models = (baseline, logistic, boosted)
        validation_evaluations = {
            model.name: evaluate_model(
                model,
                validation,
                self.dataset.oos_assets,
                self.config,
                all_assets=self.dataset.oos_assets | self.dataset.train_only_assets,
            )
            for model in raw_models
        }
        internal_walk_forward = {
            model.name: _walk_forward_evaluation(
                model.name, train, self.dataset.feature_names, self.config
            )
            for model in (logistic, boosted)
        }
        learning_winner = min(
            (logistic, boosted),
            key=lambda model: _selection_key(validation_evaluations[model.name]),
        )
        calibrated = _freeze_calibration(learning_winner, validation, self.config)
        frozen_models = (*raw_models, calibrated)
        frozen_validation = {
            **validation_evaluations,
            calibrated.name: evaluate_model(
                calibrated,
                validation,
                self.dataset.oos_assets,
                self.config,
                all_assets=self.dataset.oos_assets | self.dataset.train_only_assets,
            ),
        }
        # Test is deliberately evaluated only after candidate, calibration, and edge policy freeze.
        test_evaluations = {
            model.name: evaluate_model(
                model,
                test,
                self.dataset.oos_assets,
                self.config,
                all_assets=self.dataset.oos_assets | self.dataset.train_only_assets,
            )
            for model in frozen_models
        }
        model_ids = {
            model.name: self._publish_model(
                model,
                validation=frozen_validation[model.name],
                test=test_evaluations[model.name],
                internal_walk_forward=internal_walk_forward.get(model.name),
            )
            for model in frozen_models
        }
        champion_name, status, champion_reasons = _select_champion(
            calibrated.name,
            baseline.name,
            frozen_validation,
            test_evaluations,
        )
        model_statuses = _model_statuses(
            raw_models,
            calibrated.name,
            champion_name,
            frozen_validation,
            baseline.name,
        )
        zoo_payload = {
            "format": "live15-model-zoo-v1",
            "version": MODEL_ZOO_VERSION,
            "dataset": {
                "dataset_id": self.dataset.dataset_id,
                "deterministic_build_hash": self.dataset.deterministic_build_hash,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "git_sha": self.dataset.manifest.get("git_sha"),
            },
            "config": self.config.payload(),
            "formal_oos_assets": sorted(self.dataset.oos_assets),
            "oos_not_eligible_assets": sorted(self.dataset.train_only_assets),
            "asset_validation_eligibility": _model_asset_validation_eligibility(self.dataset),
            "selection": {
                "selection_split": "validation",
                "learning_winner_before_calibration": learning_winner.name,
                "calibration_policy": CALIBRATION_POLICY_VERSION,
                "edge_policy": _edge_policy_payload(self.config),
                "champion_status": status,
                "champion_model": champion_name,
                "reasons": champion_reasons,
            },
            "models": {
                model.name: {
                    "model_id": model_ids[model.name],
                    "family": model.family,
                    "status": model_statuses[model.name],
                    "validation": frozen_validation[model.name].payload(),
                    "test": test_evaluations[model.name].payload(),
                    "internal_walk_forward": internal_walk_forward.get(model.name),
                }
                for model in frozen_models
            },
            "test_evaluation": {
                "state": "REVEALED_FINAL",
                "policy": (
                    "test was evaluated only after validation froze model, calibration, "
                    "and edge policy"
                ),
                "evaluated_at": datetime.now(UTC).isoformat(),
            },
            "created_at": datetime.now(UTC).isoformat(),
        }
        deterministic_hash = _hash_object(_identity_view(zoo_payload))
        zoo_id = f"live15-model-zoo-v1-{deterministic_hash[:20]}"
        zoo_payload["zoo_id"] = zoo_id
        zoo_payload["deterministic_build_hash"] = deterministic_hash
        return self._publish_zoo(
            zoo_id, deterministic_hash, zoo_payload, model_ids, status, champion_name
        )

    def _publish_model(
        self,
        model: FittedModel,
        *,
        validation: ModelEvaluation,
        test: ModelEvaluation,
        internal_walk_forward: dict[str, object] | None,
    ) -> str:
        model_bytes = model.model_bytes()
        model_hash = _sha256_bytes(model_bytes)
        lineage = _lineage(self.dataset)
        identity = {
            "version": MODEL_ARTIFACT_VERSION,
            "dataset": lineage,
            "config": self.config.payload(),
            "model": model.payload(),
            "model_sha256": model_hash,
            "validation": validation.payload(),
            "test": test.payload(),
            "internal_walk_forward": internal_walk_forward,
            "edge_policy": _edge_policy_payload(self.config),
            "asset_validation_eligibility": _model_asset_validation_eligibility(self.dataset),
        }
        build_hash = _hash_object(identity)
        model_id = f"live15-{model.name}-{build_hash[:16]}"
        root = self.artifact_root / "models"
        manifest = {
            "format": "live15-model-artifact-v1",
            "version": MODEL_ARTIFACT_VERSION,
            "model_id": model_id,
            "deterministic_build_hash": build_hash,
            "created_at": datetime.now(UTC).isoformat(),
            "lineage": lineage,
            "model": model.payload(),
            "seed": self.config.seed,
            "decision_threshold_policy": _edge_policy_payload(self.config),
            "asset_validation_eligibility": _model_asset_validation_eligibility(self.dataset),
            "validation": validation.payload(),
            "test": test.payload(),
            "internal_walk_forward": internal_walk_forward,
            "test_evaluation": {"state": "REVEALED_FINAL"},
            "artifacts": {"model.json": {"sha256": model_hash, "bytes": len(model_bytes)}},
        }
        _atomic_publish(root, model_id, {"model.json": model_bytes}, manifest)
        return model_id

    def _publish_zoo(
        self,
        zoo_id: str,
        build_hash: str,
        payload: dict[str, object],
        model_ids: dict[str, str],
        status: str,
        champion_name: str | None,
    ) -> ModelZooSummary:
        root = self.artifact_root / "model_zoo"
        leaderboard = _canonical_json(_leaderboard(payload)).encode("utf-8") + b"\n"
        payload["artifacts"] = {
            "leaderboard.json": {"sha256": _sha256_bytes(leaderboard), "bytes": len(leaderboard)}
        }
        reused = _atomic_publish(root, zoo_id, {"leaderboard.json": leaderboard}, payload)
        return ModelZooSummary(
            zoo_id=zoo_id,
            artifact_path=root / zoo_id,
            dataset_id=self.dataset.dataset_id,
            champion_model_id=model_ids[champion_name] if champion_name else None,
            status=status,
            model_ids=model_ids,
            reused_existing_artifact=reused,
        )


def evaluate_model(
    model: FittedModel,
    rows: tuple[DatasetExample, ...],
    oos_assets: frozenset[str],
    config: ModelZooConfig,
    *,
    all_assets: frozenset[str] | None = None,
) -> ModelEvaluation:
    probabilities = model.predict(rows)
    labels = np.asarray([row.label_yes for row in rows], dtype=np.int8)
    metrics = _probability_metrics(probabilities, labels, config.reliability_bins)
    trades = _edge_evaluation(rows, probabilities, config)
    per_asset: dict[str, object] = {}
    known_assets = set(all_assets) if all_assets is not None else set(oos_assets)
    for asset in sorted({row.asset for row in rows} | known_assets):
        subset = tuple(row for row in rows if row.asset == asset)
        if asset not in oos_assets:
            per_asset[asset] = {"status": OOS_NOT_ELIGIBLE, "rows": len(subset)}
            continue
        indexes = [index for index, row in enumerate(rows) if row.asset == asset]
        per_asset[asset] = {
            "status": "OOS_EVALUATED",
            "rows": len(indexes),
            "metrics": _probability_metrics(
                probabilities[indexes], labels[indexes], config.reliability_bins
            ),
            "trades": _edge_evaluation(
                tuple(rows[index] for index in indexes), probabilities[indexes], config
            ),
        }
    per_bucket: dict[str, object] = {}
    for bucket in sorted({row.bucket_seconds for row in rows}):
        indexes = [index for index, row in enumerate(rows) if row.bucket_seconds == bucket]
        per_bucket[str(bucket)] = {
            "rows": len(indexes),
            "metrics": _probability_metrics(
                probabilities[indexes], labels[indexes], config.reliability_bins
            ),
            "trades": _edge_evaluation(
                tuple(rows[index] for index in indexes), probabilities[indexes], config
            ),
        }
    return ModelEvaluation(
        metrics=metrics, trades=trades, per_asset=per_asset, per_bucket=per_bucket
    )


def _fit_market_implied(feature_names: tuple[str, ...]) -> FittedModel:
    try:
        index = feature_names.index("market_probability_midpoint")
    except ValueError as error:
        raise ModelZooError("certified schema lacks market-implied probability") from error
    return FittedModel(
        name="market_implied",
        family="market_implied",
        feature_names=feature_names,
        preprocessor=None,
        model=None,
        market_probability_index=index,
        hyperparameters={"source_feature": "market_probability_midpoint"},
    )


def _fit_logistic(
    rows: tuple[DatasetExample, ...], feature_names: tuple[str, ...], config: ModelZooConfig
) -> FittedModel:
    preprocessor = Preprocessor.fit(rows, feature_names)
    matrix = preprocessor.transform(rows)
    labels = np.asarray([row.label_yes for row in rows], dtype=np.float64)
    weights = np.zeros(matrix.shape[1], dtype=np.float64)
    positive_rate = float(np.mean(labels))
    intercept = _logit(positive_rate)
    for _ in range(config.logistic_iterations):
        predicted = _sigmoid_array(matrix @ weights + intercept)
        error = predicted - labels
        weights -= config.logistic_learning_rate * (
            (matrix.T @ error) / len(labels) + config.logistic_l2 * weights
        )
        intercept -= config.logistic_learning_rate * float(np.mean(error))
    return FittedModel(
        name="logistic_l2",
        family="logistic_l2",
        feature_names=feature_names,
        preprocessor=preprocessor,
        model=(weights, intercept),
        market_probability_index=None,
        hyperparameters={
            "iterations": config.logistic_iterations,
            "learning_rate": config.logistic_learning_rate,
            "l2": config.logistic_l2,
            "seed": config.seed,
        },
    )


def _fit_xgboost(
    rows: tuple[DatasetExample, ...], feature_names: tuple[str, ...], config: ModelZooConfig
) -> FittedModel:
    preprocessor = Preprocessor.fit(rows, feature_names)
    parameters: dict[str, Any] = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "seed": config.seed,
        "nthread": 1,
        "tree_method": "hist",
        "max_depth": config.xgboost_max_depth,
        "eta": config.xgboost_learning_rate,
        "subsample": 1.0,
        "colsample_bytree": 1.0,
        "min_child_weight": 8,
        "lambda": 5.0,
    }
    matrix = preprocessor.transform(rows)
    labels = np.asarray([row.label_yes for row in rows], dtype=np.float64)
    booster = xgb.train(parameters, xgb.DMatrix(matrix, label=labels), config.xgboost_rounds)
    return FittedModel(
        name="xgboost",
        family="xgboost",
        feature_names=feature_names,
        preprocessor=preprocessor,
        model=booster,
        market_probability_index=None,
        hyperparameters={**parameters, "rounds": config.xgboost_rounds},
    )


def _freeze_calibration(
    model: FittedModel,
    validation: tuple[DatasetExample, ...],
    config: ModelZooConfig,
) -> FittedModel:
    labels = np.asarray([row.label_yes for row in validation], dtype=np.int8)
    raw = model.predict(validation)
    raw_brier = _probability_metrics(raw, labels, config.reliability_bins)["brier_score"]
    try:
        calibrator = PlattCalibrator.fit(raw, labels)
        calibrated = calibrator.transform(raw)
        calibrated_brier = _probability_metrics(calibrated, labels, config.reliability_bins)[
            "brier_score"
        ]
    except ModelZooError:
        calibrator = None
        calibrated_brier = None
    if (
        calibrator is not None
        and isinstance(calibrated_brier, float)
        and calibrated_brier < raw_brier
    ):
        return FittedModel(
            name=f"{model.name}_platt",
            family=model.family,
            feature_names=model.feature_names,
            preprocessor=model.preprocessor,
            model=model.model,
            market_probability_index=model.market_probability_index,
            calibration=calibrator,
            calibration_method="platt_sigmoid",
            hyperparameters=model.hyperparameters,
        )
    return FittedModel(
        name=f"{model.name}_identity",
        family=model.family,
        feature_names=model.feature_names,
        preprocessor=model.preprocessor,
        model=model.model,
        market_probability_index=model.market_probability_index,
        calibration_method="identity",
        hyperparameters=model.hyperparameters,
    )


def _walk_forward_evaluation(
    name: str,
    rows: tuple[DatasetExample, ...],
    feature_names: tuple[str, ...],
    config: ModelZooConfig,
) -> dict[str, object]:
    folds = _walk_forward_folds(rows, config.internal_walk_forward_folds)
    metrics: list[dict[str, object]] = []
    for train, validation in folds:
        model = (
            _fit_logistic(train, feature_names, config)
            if name == "logistic_l2"
            else _fit_xgboost(train, feature_names, config)
        )
        evaluation = evaluate_model(
            model, validation, frozenset(row.asset for row in validation), config
        )
        metrics.append(evaluation.metrics)
    return {
        "policy": "event-window-grouped-expanding-walk-forward-v1",
        "folds": len(folds),
        "mean_brier_score": fmean(float(item["brier_score"]) for item in metrics),
        "mean_log_loss": fmean(float(item["log_loss"]) for item in metrics),
        "fold_metrics": metrics,
    }


def _walk_forward_folds(
    rows: tuple[DatasetExample, ...], count: int
) -> tuple[tuple[tuple[DatasetExample, ...], tuple[DatasetExample, ...]], ...]:
    grouped: dict[tuple[datetime, datetime], list[DatasetExample]] = {}
    for row in rows:
        grouped.setdefault((row.window_start, row.window_end), []).append(row)
    windows = tuple(
        tuple(sorted(value, key=_example_sort_key)) for _key, value in sorted(grouped.items())
    )
    if len(windows) < count + 2:
        raise ModelZooError("insufficient chronological train windows for walk-forward evaluation")
    first_train_end = max(1, len(windows) // 2)
    remaining = len(windows) - first_train_end
    validation_width = max(1, remaining // count)
    result = []
    for index in range(count):
        train_end = first_train_end + index * validation_width
        if train_end >= len(windows):
            break
        validation_end = min(len(windows), train_end + validation_width)
        if validation_end == train_end:
            break
        result.append(
            (
                tuple(row for window in windows[:train_end] for row in window),
                tuple(row for window in windows[train_end:validation_end] for row in window),
            )
        )
    if not result:
        raise ModelZooError("walk-forward policy did not produce a fold")
    return tuple(result)


def _probability_metrics(
    probabilities: np.ndarray, labels: np.ndarray, bins: int
) -> dict[str, object]:
    if len(probabilities) != len(labels) or not len(labels):
        raise ModelZooError("probability metric inputs are malformed")
    probability = np.clip(np.asarray(probabilities, dtype=np.float64), 1e-6, 1 - 1e-6)
    truth = np.asarray(labels, dtype=np.int8)
    brier = float(np.mean((probability - truth) ** 2))
    log_loss = float(-np.mean(truth * np.log(probability) + (1 - truth) * np.log(1 - probability)))
    prediction = (probability >= 0.5).astype(np.int8)
    true_positive = int(np.sum((prediction == 1) & (truth == 1)))
    false_positive = int(np.sum((prediction == 1) & (truth == 0)))
    true_negative = int(np.sum((prediction == 0) & (truth == 0)))
    false_negative = int(np.sum((prediction == 0) & (truth == 1)))
    precision = (
        true_positive / (true_positive + false_positive) if true_positive + false_positive else None
    )
    recall = (
        true_positive / (true_positive + false_negative) if true_positive + false_negative else None
    )
    reliability = []
    ece = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        mask = (probability >= lower) & (
            (probability < upper) if index < bins - 1 else (probability <= upper)
        )
        count = int(np.sum(mask))
        if not count:
            reliability.append(
                {
                    "lower": lower,
                    "upper": upper,
                    "count": 0,
                    "mean_probability": None,
                    "yes_rate": None,
                }
            )
            continue
        mean_probability = float(np.mean(probability[mask]))
        yes_rate = float(np.mean(truth[mask]))
        ece += abs(mean_probability - yes_rate) * count / len(truth)
        reliability.append(
            {
                "lower": lower,
                "upper": upper,
                "count": count,
                "mean_probability": mean_probability,
                "yes_rate": yes_rate,
            }
        )
    histogram = np.histogram(probability, bins=bins, range=(0.0, 1.0))[0].tolist()
    return {
        "rows": len(truth),
        "brier_score": brier,
        "log_loss": log_loss,
        "expected_calibration_error": ece,
        "accuracy": float(np.mean(prediction == truth)),
        "precision": precision,
        "recall": recall,
        "confusion_matrix": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "true_negative": true_negative,
            "false_negative": false_negative,
        },
        "reliability_bins": reliability,
        "predicted_probability_histogram": histogram,
    }


def _edge_evaluation(
    rows: tuple[DatasetExample, ...], probabilities: np.ndarray, config: ModelZooConfig
) -> dict[str, object]:
    index = {
        name: position
        for position, name in enumerate(tuple(definition.name for definition in FEATURE_REGISTRY))
    }
    yes_ask_index = index["yes_ask"]
    no_ask_index = index["no_ask"]
    fee_model = KalshiTakerFeeModel()
    net_pnl = Decimal(0)
    peak = Decimal(0)
    max_drawdown = Decimal(0)
    gross_profit = Decimal(0)
    gross_loss = Decimal(0)
    gross_pnl = Decimal(0)
    fees = Decimal(0)
    total_edge = Decimal(0)
    signals = 0
    for row, probability in sorted(
        zip(rows, probabilities, strict=True), key=lambda item: _example_sort_key(item[0])
    ):
        yes_ask = row.values[yes_ask_index]
        no_ask = row.values[no_ask_index]
        if yes_ask is None or no_ask is None:
            continue
        yes_edge = Decimal(str(float(probability))) - yes_ask
        no_edge = Decimal(1) - Decimal(str(float(probability))) - no_ask
        if max(yes_edge, no_edge) < config.fixed_edge_threshold:
            continue
        side_yes = yes_edge >= no_edge
        price = yes_ask if side_yes else no_ask
        edge = yes_edge if side_yes else no_edge
        won = bool(row.label_yes) == side_yes
        gross = Decimal(1) - price if won else -price
        side = "yes" if side_yes else "no"
        order_id = f"model-zoo-{row.ticker}-{row.decision_timestamp.isoformat()}-{side}"
        fee = fee_model.compute(
            order_id=order_id,
            quantity=Decimal(1),
            price=price,
            action=ExecutionAction.BUY,
        )
        fee_model.finish_order(order_id)
        net = gross - fee.net_fee
        gross_pnl += gross
        fees += fee.net_fee
        net_pnl += net
        peak = max(peak, net_pnl)
        max_drawdown = max(max_drawdown, peak - net_pnl)
        if net > 0:
            gross_profit += net
        elif net < 0:
            gross_loss += -net
        total_edge += edge
        signals += 1
    return {
        "policy": _edge_policy_payload(config),
        "actionable_signals": signals,
        "trade_count": signals,
        "gross_pnl_estimate": str(gross_pnl),
        "estimated_fees_costs": str(fees),
        "net_pnl_estimate": str(net_pnl),
        "max_drawdown": str(max_drawdown),
        "profit_factor": str(gross_profit / gross_loss) if gross_loss else None,
        "average_edge": str(total_edge / signals) if signals else None,
    }


def _edge_policy_payload(config: ModelZooConfig) -> dict[str, object]:
    return {
        "version": EDGE_POLICY_VERSION,
        "one_contract_per_row": True,
        "entry": (
            "buy YES at certified yes_ask or BUY NO at certified no_ask, whichever has larger edge"
        ),
        "minimum_expected_edge": str(config.fixed_edge_threshold),
        "hold": "to official Kalshi final settlement label",
        "fees": "existing conservative Kalshi taker fee model; no fill/slippage optimism",
        "not_execution": "historical model comparison only; never an order instruction",
    }


def _select_champion(
    calibrated_name: str,
    baseline_name: str,
    validation: dict[str, ModelEvaluation],
    test: dict[str, ModelEvaluation],
) -> tuple[str | None, str, list[str]]:
    candidate_validation = validation[calibrated_name].metrics
    baseline_validation = validation[baseline_name].metrics
    candidate_test = test[calibrated_name]
    reasons: list[str] = []
    if float(candidate_validation["brier_score"]) >= float(baseline_validation["brier_score"]):
        reasons.append("validation_brier_does_not_beat_market_implied_baseline")
    if float(candidate_validation["expected_calibration_error"]) > 0.10:
        reasons.append("validation_calibration_error_exceeds_fixed_gate")
    if int(candidate_test.trades["trade_count"]) < 10:
        reasons.append("insufficient_final_test_trade_count")
    if Decimal(str(candidate_test.trades["net_pnl_estimate"])) <= 0:
        reasons.append("final_test_net_pnl_not_positive_after_estimated_costs")
    if (
        candidate_test.trades["profit_factor"] is None
        or Decimal(str(candidate_test.trades["profit_factor"])) <= 1
    ):
        reasons.append("final_test_profit_factor_not_above_one")
    if reasons:
        return None, "NO_CHAMPION", reasons
    return (
        calibrated_name,
        "CHAMPION",
        ["validation-selected configuration passed frozen final test gates"],
    )


def _model_statuses(
    raw_models: tuple[FittedModel, ...],
    calibrated_name: str,
    champion_name: str | None,
    validation: dict[str, ModelEvaluation],
    baseline_name: str,
) -> dict[str, str]:
    """Classify models using validation evidence; final champion is additionally gated on test."""

    baseline_brier = float(validation[baseline_name].metrics["brier_score"])
    statuses: dict[str, str] = {}
    for model in raw_models:
        if model.name == baseline_name:
            statuses[model.name] = "CHALLENGER"
        elif float(validation[model.name].metrics["brier_score"]) < baseline_brier:
            statuses[model.name] = "CHALLENGER"
        else:
            statuses[model.name] = "REJECTED"
    statuses[calibrated_name] = "CHAMPION" if champion_name == calibrated_name else "REJECTED"
    return statuses


def _formal_rows(
    rows: tuple[DatasetExample, ...], oos_assets: frozenset[str]
) -> tuple[DatasetExample, ...]:
    return tuple(row for row in rows if row.asset in oos_assets)


def _model_asset_validation_eligibility(dataset: CertifiedDataset) -> dict[str, object]:
    return {
        asset: {
            "out_of_sample_validation": asset in dataset.oos_assets,
            "status": "OOS_ELIGIBLE" if asset in dataset.oos_assets else OOS_NOT_ELIGIBLE,
        }
        for asset in sorted(dataset.oos_assets | dataset.train_only_assets)
    }


def _asset_oos_eligibility(manifest: dict[str, object]) -> dict[str, bool]:
    explicit = manifest.get("asset_validation_eligibility")
    if isinstance(explicit, dict):
        result: dict[str, bool] = {}
        for asset, payload in explicit.items():
            if not isinstance(payload, dict) or not isinstance(
                payload.get("out_of_sample_validation"), bool
            ):
                raise ModelZooError("Dataset v1 asset validation eligibility is malformed")
            result[str(asset)] = bool(payload["out_of_sample_validation"])
        return result
    diagnostics = manifest.get("diagnostics")
    if not isinstance(diagnostics, dict) or not isinstance(diagnostics.get("splits"), dict):
        raise ModelZooError("Dataset v1 lacks asset eligibility diagnostics")
    result = {}
    assets = manifest.get("decision_time_policy", {})
    names = assets.get("assets", []) if isinstance(assets, dict) else []
    if not isinstance(names, list) or not all(isinstance(asset, str) for asset in names):
        raise ModelZooError("Dataset v1 asset policy is malformed")
    for asset in names:
        validation = diagnostics["splits"].get("validation", {})
        test = diagnostics["splits"].get("test", {})
        validation_events = (
            validation.get("events_per_asset", {}).get(asset, 0)
            if isinstance(validation, dict)
            else 0
        )
        test_events = (
            test.get("events_per_asset", {}).get(asset, 0) if isinstance(test, dict) else 0
        )
        result[str(asset)] = (
            isinstance(validation_events, int)
            and isinstance(test_events, int)
            and validation_events > 0
            and test_events > 0
        )
    return result


def _parse_dataset_row(
    payload: dict[str, object], expected_names: tuple[str, ...]
) -> DatasetExample:
    required = (
        "asset",
        "event_identity",
        "decision_timestamp",
        "window_start",
        "window_end",
        "label",
        "features",
        "time_remaining_seconds",
    )
    if any(name not in payload for name in required):
        raise ModelZooError("Dataset v1 training row is malformed")
    raw_features = payload["features"]
    if not isinstance(raw_features, list) or len(raw_features) != len(expected_names):
        raise ModelZooError("Dataset v1 row feature count is malformed")
    values: list[Decimal | None] = []
    reasons: list[str | None] = []
    decision = _parse_time(payload["decision_timestamp"])
    for name, feature in zip(expected_names, raw_features, strict=True):
        if not isinstance(feature, dict) or feature.get("name") != name:
            raise ModelZooError("Dataset v1 feature ordering is malformed")
        value = feature.get("value")
        reason = feature.get("missing_reason")
        source = feature.get("source_timestamp")
        if source is not None and _parse_time(source) > decision:
            raise ModelZooError("Dataset v1 feature leaks beyond decision time")
        if (value is None) != (reason is not None):
            raise ModelZooError("Dataset v1 typed missing semantics are malformed")
        values.append(Decimal(str(value)) if value is not None else None)
        reasons.append(str(reason) if reason is not None else None)
    label = payload["label"]
    if label not in {"yes", "no"}:
        raise ModelZooError("Dataset v1 label is not official YES/NO settlement truth")
    return DatasetExample(
        asset=str(payload["asset"]),
        ticker=str(payload["event_identity"]),
        decision_timestamp=decision,
        window_start=_parse_time(payload["window_start"]),
        window_end=_parse_time(payload["window_end"]),
        bucket_seconds=int(Decimal(str(payload["time_remaining_seconds"]))),
        label_yes=1 if label == "yes" else 0,
        values=tuple(values),
        missing_reasons=tuple(reasons),
    )


def _validate_chronology(splits: dict[str, tuple[DatasetExample, ...]]) -> None:
    events = {name: {row.ticker for row in rows} for name, rows in splits.items()}
    if (
        events["train"] & events["validation"]
        or events["train"] & events["test"]
        or events["validation"] & events["test"]
    ):
        raise ModelZooError("Dataset v1 event identity crosses model split boundaries")
    if max(row.window_end for row in splits["train"]) > min(
        row.window_start for row in splits["validation"]
    ):
        raise ModelZooError("Dataset v1 train/validation chronology is invalid")
    if max(row.window_end for row in splits["validation"]) > min(
        row.window_start for row in splits["test"]
    ):
        raise ModelZooError("Dataset v1 validation/test chronology is invalid")


def _lineage(dataset: CertifiedDataset) -> dict[str, str]:
    dataset_git_sha = dataset.manifest.get("git_sha")
    if not isinstance(dataset_git_sha, str):
        raise ModelZooError("Dataset v1 lineage lacks code Git SHA")
    lineage = ModelDatasetLineage(
        dataset_id=dataset.dataset_id,
        deterministic_build_hash=dataset.deterministic_build_hash,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        code_git_sha=current_git_sha(),
    ).as_dict()
    lineage["dataset_code_git_sha"] = dataset_git_sha
    lineage["model_source_sha256"] = _sha256_file(Path(__file__))
    return lineage


def _atomic_publish(
    root: Path, artifact_id: str, files: dict[str, bytes], manifest: dict[str, object]
) -> bool:
    root.mkdir(parents=True, exist_ok=True)
    _recover_stale_staging(root, artifact_id)
    final = root / artifact_id
    if final.exists():
        _verify_published(final, manifest, files)
        return True
    staging = root / f".{artifact_id}.staging-{os.getpid()}"
    if staging.exists():
        raise ModelZooError("model artifact publisher staging directory already exists")
    staging.mkdir()
    try:
        for name, payload in files.items():
            (staging / name).write_bytes(payload)
        (staging / "manifest.json").write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
        _verify_published(staging, manifest, files)
        try:
            os.rename(staging, final)
        except FileExistsError:
            _verify_published(final, manifest, files)
            return True
    finally:
        if staging.exists():
            _remove_tree(staging)
    return False


def _recover_stale_staging(root: Path, artifact_id: str) -> None:
    """Remove only abandoned same-artifact staging directories before publication."""

    prefix = f".{artifact_id}.staging-"
    for path in root.glob(f"{prefix}*"):
        if not path.is_dir():
            raise ModelZooError("model artifact staging path is not a directory")
        suffix = path.name.removeprefix(prefix)
        try:
            pid = int(suffix)
        except ValueError as exc:
            raise ModelZooError(
                "model artifact staging directory has invalid publisher identity"
            ) from exc
        if process_alive(pid):
            raise ModelZooError("another model artifact publisher is already active")
        _remove_tree(path)


def _verify_published(
    path: Path, expected_manifest: dict[str, object], files: dict[str, bytes]
) -> None:
    manifest = _read_json(path / "manifest.json")
    if _identity_view(manifest) != _identity_view(expected_manifest):
        raise ModelZooError("existing immutable model artifact conflicts with this build")
    for name, payload in files.items():
        if not (path / name).is_file() or (path / name).read_bytes() != payload:
            raise ModelZooError("existing immutable model artifact bytes conflict with this build")


def _leaderboard(payload: dict[str, object]) -> dict[str, object]:
    models = payload.get("models")
    if not isinstance(models, dict):
        raise ModelZooError("Model Zoo payload is malformed")
    entries = []
    for name, item in models.items():
        if not isinstance(item, dict):
            continue
        validation = item.get("validation", {})
        test = item.get("test", {})
        validation_metrics = validation.get("metrics", {}) if isinstance(validation, dict) else {}
        test_metrics = test.get("metrics", {}) if isinstance(test, dict) else {}
        test_trades = test.get("trades", {}) if isinstance(test, dict) else {}
        entries.append(
            {
                "model": name,
                "status": item.get("status"),
                "validation_brier": validation_metrics.get("brier_score"),
                "validation_log_loss": validation_metrics.get("log_loss"),
                "validation_calibration_error": validation_metrics.get(
                    "expected_calibration_error"
                ),
                "test_brier": test_metrics.get("brier_score"),
                "test_log_loss": test_metrics.get("log_loss"),
                "test_calibration_error": test_metrics.get("expected_calibration_error"),
                "test_net_pnl": test_trades.get("net_pnl_estimate"),
                "test_max_drawdown": test_trades.get("max_drawdown"),
                "test_profit_factor": test_trades.get("profit_factor"),
                "test_trades": test_trades.get("trade_count"),
            }
        )
    return {
        "format": "live15-model-zoo-leaderboard-v1",
        "dataset_id": payload.get("dataset", {}).get("dataset_id")
        if isinstance(payload.get("dataset"), dict)
        else None,
        "champion_status": payload.get("selection", {}).get("champion_status")
        if isinstance(payload.get("selection"), dict)
        else None,
        "models": sorted(entries, key=lambda item: str(item["model"])),
    }


def _selection_key(evaluation: ModelEvaluation) -> tuple[float, float]:
    return (
        float(evaluation.metrics["brier_score"]),
        float(evaluation.metrics["log_loss"]),
    )


def _identity_view(manifest: dict[str, object]) -> dict[str, object]:
    value = json.loads(_canonical_json(manifest))
    assert isinstance(value, dict)
    value.pop("created_at", None)
    test = value.get("test_evaluation")
    if isinstance(test, dict):
        test.pop("evaluated_at", None)
    return value


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ModelZooError("immutable JSON artifact is missing or malformed") from error
    if not isinstance(value, dict):
        raise ModelZooError("immutable JSON artifact root is malformed")
    return value


def _read_json_text(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ModelZooError("Dataset v1 JSONL row is malformed") from error
    if not isinstance(payload, dict):
        raise ModelZooError("Dataset v1 JSONL row is malformed")
    return payload


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ModelZooError("Dataset v1 timestamp is malformed")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ModelZooError("Dataset v1 timestamp is not UTC-aware")
    return result.astimezone(UTC)


def _example_sort_key(row: DatasetExample) -> tuple[datetime, datetime, str, datetime]:
    return row.window_start, row.window_end, row.ticker, row.decision_timestamp


def _logit(value: float) -> float:
    clipped = min(max(value, 1e-6), 1 - 1e-6)
    return math.log(clipped / (1 - clipped))


def _sigmoid_array(value: np.ndarray) -> np.ndarray:
    clipped = np.clip(value, -40.0, 40.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _hash_object(value: object) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _remove_tree(path: Path) -> None:
    for child in sorted(path.iterdir(), reverse=True):
        if child.is_dir():
            _remove_tree(child)
        else:
            child.unlink()
    path.rmdir()


def current_git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, check=True, text=True
    ).stdout.strip()
    if len(result) != 40:
        raise ModelZooError("unable to resolve full Git SHA for model lineage")
    return result
