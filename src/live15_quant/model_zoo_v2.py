"""Development-only, leakage-safe Model Zoo v2.

Model Zoo v1 has revealed Dataset v1's final test.  This module deliberately
does *not* read that split for model, calibration, asset, or threshold
selection.  It works only from the certified training split using grouped,
chronological internal development folds.  Its output is a forward-candidate
artifact, never a champion or a new final-test result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from live15_quant.execution import ExecutionAction
from live15_quant.fees import KalshiTakerFeeModel
from live15_quant.model_zoo import (
    FEATURE_REGISTRY,
    CertifiedDataset,
    DatasetExample,
    FittedModel,
    ModelZooError,
    PlattCalibrator,
    Preprocessor,
    _atomic_publish,
    _canonical_json,
    _fit_logistic,
    _fit_market_implied,
    _fit_xgboost,
    _hash_object,
    _probability_metrics,
    _read_json,
    _sha256_bytes,
    current_git_sha,
    xgb,
)

MODEL_ZOO_V2_VERSION = "1.0.0"
DEVELOPMENT_POLICY_VERSION = "event-grouped-chronological-development-v1"
FINAL_TEST_POLICY = "DATASET_V1_FINAL_TEST_REVEALED_NOT_USED_FOR_V2_DEVELOPMENT"
DEFAULT_THRESHOLDS = (
    Decimal("0.03"),
    Decimal("0.05"),
    Decimal("0.075"),
    Decimal("0.10"),
    Decimal("0.125"),
    Decimal("0.15"),
)


@dataclass(frozen=True, slots=True)
class ModelZooV2Config:
    """Frozen small development grid; no configuration is selected on final test."""

    seed: int = 20260823
    thresholds: tuple[Decimal, ...] = DEFAULT_THRESHOLDS
    folds: int = 3
    logistic_iterations: int = 1_500
    logistic_learning_rate: float = 0.08
    logistic_l2: float = 0.25
    xgboost_rounds: int = 80
    xgboost_max_depth: int = 3
    xgboost_learning_rate: float = 0.05
    # Promotion gates are intentionally fixed before development execution.
    min_trade_count: int = 40
    min_profit_factor: Decimal = Decimal("1.05")
    max_drawdown: Decimal = Decimal("20")
    max_brier_regression_vs_market: float = 0.005
    min_positive_assets: int = 2
    min_trades_per_positive_asset: int = 10
    max_positive_net_pnl_concentration: Decimal = Decimal("0.75")
    min_positive_development_folds: int = 2
    asset_caution_brier_delta: float = 0.01
    min_asset_caution_folds: int = 2
    adverse_entry_cents: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if self.seed < 0 or self.folds < 2:
            raise ValueError("invalid deterministic development configuration")
        if not self.thresholds or tuple(sorted(set(self.thresholds))) != self.thresholds:
            raise ValueError("threshold ladder must be non-empty, unique, and ascending")
        if any(not Decimal(0) < value < Decimal(1) for value in self.thresholds):
            raise ValueError("all edge thresholds must be between zero and one")
        if min(self.logistic_iterations, self.xgboost_rounds, self.xgboost_max_depth) <= 0:
            raise ValueError("model iteration/depth values must be positive")
        if (
            self.min_trade_count <= 0
            or self.min_positive_assets <= 0
            or self.min_trades_per_positive_asset <= 0
            or self.min_asset_caution_folds <= 0
            or self.min_positive_development_folds <= 0
        ):
            raise ValueError("promotion count gates must be positive")
        if not Decimal(0) < self.max_positive_net_pnl_concentration <= Decimal(1):
            raise ValueError("positive PnL concentration gate must be in (0, 1]")

    def payload(self) -> dict[str, object]:
        value = asdict(self)
        for name in (
            "thresholds",
            "min_profit_factor",
            "max_drawdown",
            "max_positive_net_pnl_concentration",
            "adverse_entry_cents",
        ):
            raw = value[name]
            if isinstance(raw, tuple):
                value[name] = [str(item) for item in raw]
            else:
                value[name] = str(raw)
        return value


@dataclass(frozen=True, slots=True)
class DevelopmentSummary:
    zoo_id: str
    artifact_path: Path
    dataset_id: str
    status: str
    forward_candidate_ids: tuple[str, ...]
    reused_existing_artifact: bool


@dataclass(frozen=True, slots=True)
class _Spec:
    name: str
    family: str
    calibration: str
    promotable: bool = True


_SPECS = (
    _Spec("market_implied_identity", "market_implied", "identity", promotable=False),
    _Spec("logistic_l2_identity", "logistic_l2", "identity"),
    _Spec("xgboost_pooled_identity", "xgboost", "identity"),
    _Spec("xgboost_pooled_platt", "xgboost", "chronological_platt"),
    _Spec("xgboost_asset_identity", "xgboost_asset_identity", "identity"),
    _Spec("xgboost_market_residual", "xgboost_market_residual", "identity"),
)


def _candidate_definitions() -> dict[str, object]:
    """Static candidate scope, frozen before development-fold evaluation."""

    return {
        "market_implied_identity": {
            "role": "reference_only",
            "definition": "certified market_probability_midpoint",
        },
        "logistic_l2_identity": {
            "role": "pooled_direct_probability",
            "definition": "regularized logistic model over all certified features",
        },
        "xgboost_pooled_identity": {
            "role": "pooled_direct_probability",
            "definition": "fixed-seed bounded XGBoost over all certified features",
        },
        "xgboost_pooled_platt": {
            "role": "pooled_direct_probability",
            "definition": "pooled XGBoost plus past-only chronological Platt calibration",
        },
        "xgboost_asset_identity": {
            "role": "asset_aware_pooled_probability",
            "definition": (
                "pooled XGBoost plus known-at-decision one-hot asset identity; "
                "tree splits provide limited asset-feature interactions without per-asset models"
            ),
        },
        "xgboost_market_residual": {
            "role": "market_residual_probability",
            "definition": "market_probability_midpoint plus train-only learned residual correction",
        },
    }


@dataclass(frozen=True, slots=True)
class _Predictions:
    rows: tuple[DatasetExample, ...]
    probabilities: np.ndarray
    fold_details: tuple[dict[str, object], ...]


class _AssetAwareXgb:
    """Pooled model with known-at-decision asset identity, never per-asset selection."""

    def __init__(
        self,
        preprocessor: Preprocessor,
        booster: Any,
        assets: tuple[str, ...],
    ) -> None:
        self.preprocessor = preprocessor
        self.booster = booster
        self.assets = assets

    def _matrix(self, rows: tuple[DatasetExample, ...]) -> np.ndarray:
        base = self.preprocessor.transform(rows)
        identity = np.zeros((len(rows), len(self.assets)), dtype=np.float64)
        indexes = {asset: index for index, asset in enumerate(self.assets)}
        for row_index, row in enumerate(rows):
            index = indexes.get(row.asset)
            if index is not None:
                identity[row_index, index] = 1.0
        return np.hstack((base, identity))

    def predict(self, rows: tuple[DatasetExample, ...]) -> np.ndarray:
        return np.clip(self.booster.predict(xgb.DMatrix(self._matrix(rows))), 1e-6, 1 - 1e-6)


class _ResidualXgb:
    """Learns a bounded correction to market-implied probability from past rows only."""

    def __init__(self, preprocessor: Preprocessor, booster: Any, market_index: int) -> None:
        self.preprocessor = preprocessor
        self.booster = booster
        self.market_index = market_index

    def predict(self, rows: tuple[DatasetExample, ...]) -> np.ndarray:
        market = _market_probabilities(rows, self.market_index)
        residual = self.booster.predict(xgb.DMatrix(self.preprocessor.transform(rows)))
        return np.clip(market + residual, 1e-6, 1 - 1e-6)


class ModelZooV2:
    """Generate immutable development candidates without consuming final-test rows."""

    def __init__(
        self,
        dataset: CertifiedDataset,
        artifact_root: Path,
        v1_artifact: Path,
        config: ModelZooV2Config | None = None,
    ) -> None:
        self.dataset = dataset
        self.artifact_root = artifact_root.resolve()
        self.v1_artifact = v1_artifact.resolve()
        self.config = config or ModelZooV2Config()

    def build(self) -> DevelopmentSummary:
        v1 = _verify_revealed_v1(self.v1_artifact, self.dataset)
        # Deliberately obtain only the certified *train* split.  No code below accesses
        # dataset.splits["validation"] or dataset.splits["test"].
        rows = tuple(
            row for row in self.dataset.splits["train"] if row.asset in self.dataset.oos_assets
        )
        folds = _development_folds(rows, self.config.folds)
        assets = tuple(sorted(self.dataset.oos_assets))
        evaluations: dict[str, dict[str, object]] = {}
        for spec in _SPECS:
            predictions = _cross_fitted_predictions(
                spec, folds, self.dataset.feature_names, assets, self.config
            )
            evaluation = _development_evaluation(
                predictions.rows, predictions.probabilities, self.config
            )
            evaluations[spec.name] = {
                "family": spec.family,
                "calibration": spec.calibration,
                "folds": list(predictions.fold_details),
                "aggregate": evaluation,
            }

        baseline = evaluations["market_implied_identity"]["aggregate"]
        assert isinstance(baseline, dict)
        candidates: dict[str, object] = {}
        for spec in _SPECS:
            item = evaluations[spec.name]
            candidates[spec.name] = {**item, "threshold_status": {}}

        # Threshold-level economics and candidate-level probability quality are
        # intentionally separate.  Associate them only after every cross-fitted
        # development prediction is complete, never with Dataset v1 test data.
        _attach_candidate_metrics(candidates)
        _attach_fold_stability(candidates)
        _attach_asset_cautions(candidates, self.config)
        for spec in _SPECS:
            item = candidates[spec.name]
            assert isinstance(item, dict)
            aggregate = item["aggregate"]
            assert isinstance(aggregate, dict)
            trades = aggregate["thresholds"]
            assert isinstance(trades, dict)
            item["threshold_status"] = {
                threshold: _promotion_status(spec, trades[threshold], baseline, self.config)
                for threshold in trades
            }

        forward_ids = tuple(
            f"{name}@{threshold}"
            for name, item in candidates.items()
            if isinstance(item, dict)
            for threshold, gate in item["threshold_status"].items()
            if isinstance(gate, dict) and gate["status"] == "FORWARD_CANDIDATE"
        )
        status = "FORWARD_CANDIDATE" if forward_ids else "NO_FORWARD_CANDIDATE"
        payload = {
            "format": "live15-model-zoo-v2-development-v1",
            "version": MODEL_ZOO_V2_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "dataset": {
                "dataset_id": self.dataset.dataset_id,
                "deterministic_build_hash": self.dataset.deterministic_build_hash,
                "dataset_code_git_sha": self.dataset.manifest.get("git_sha"),
            },
            "certified_data_contract": {
                "feature_schema": self.dataset.manifest.get("feature_schema"),
                "label_schema": self.dataset.manifest.get("label_schema"),
                "decision_time_policy": self.dataset.manifest.get("decision_time_policy"),
                "quarantine_gap_policy": self.dataset.manifest.get("quarantine_gap_policy"),
                "market_session_semantics": self.dataset.manifest.get("market_session_semantics"),
                "source_provider_semantics": self.dataset.manifest.get("source_provider_semantics"),
                "source_snapshot": self.dataset.manifest.get("source_snapshot"),
                "archive_manifest_snapshot": self.dataset.manifest.get("archive_manifest_snapshot"),
            },
            "lineage": {
                "model_code_git_sha": current_git_sha(),
                "model_source_sha256": _sha256_bytes(Path(__file__).read_bytes()),
                "v1_model_zoo_id": v1["zoo_id"],
                "v1_model_zoo_hash": v1["deterministic_build_hash"],
            },
            "final_test": {
                "state": "REVEALED_FINAL",
                "policy": FINAL_TEST_POLICY,
                "v2_test_rows_consumed_for_development": False,
                "v2_test_metrics_emitted": False,
                "dataset_artifact_hash_verified": True,
            },
            "development_policy": {
                "version": DEVELOPMENT_POLICY_VERSION,
                "split": "Dataset v1 train split only",
                "folds": "event-window-grouped chronological expanding walk-forward",
                "preprocessing": "fit on each fold's historical training rows only",
                "calibration": "identity or chronological past-only Platt",
                "asset_selection": "train/internal-fold evidence only; no asset is removed",
            },
            "config": self.config.payload(),
            "formal_oos_assets": list(assets),
            "oos_not_eligible_assets": sorted(self.dataset.train_only_assets),
            "candidate_definitions": _candidate_definitions(),
            "asset_aware_configuration": {
                "asset_identity_order": list(assets),
                "policy": (
                    "known at decision time; pooled only; no asset is excluded or selected "
                    "from Final Test"
                ),
            },
            "candidates": candidates,
            "status": status,
            "forward_candidates": list(forward_ids),
        }
        identity = _identity_view(payload)
        build_hash = _hash_object(identity)
        zoo_id = f"live15-model-zoo-v2-{build_hash[:20]}"
        payload["zoo_id"] = zoo_id
        payload["deterministic_build_hash"] = build_hash
        leaderboard = _leaderboard(payload).encode("utf-8") + b"\n"
        payload["artifacts"] = {
            "leaderboard.json": {
                "sha256": _sha256_bytes(leaderboard),
                "bytes": len(leaderboard),
            }
        }
        reused = _atomic_publish(
            self.artifact_root / "model_zoo_v2",
            zoo_id,
            {"leaderboard.json": leaderboard},
            payload,
        )
        return DevelopmentSummary(
            zoo_id=zoo_id,
            artifact_path=self.artifact_root / "model_zoo_v2" / zoo_id,
            dataset_id=self.dataset.dataset_id,
            status=status,
            forward_candidate_ids=forward_ids,
            reused_existing_artifact=reused,
        )


def _verify_revealed_v1(path: Path, dataset: CertifiedDataset) -> dict[str, object]:
    manifest = _read_json(path / "manifest.json")
    if manifest.get("format") != "live15-model-zoo-v1":
        raise ModelZooError("Model Zoo v2 requires an immutable Model Zoo v1 artifact")
    test = manifest.get("test_evaluation")
    if not isinstance(test, dict) or test.get("state") != "REVEALED_FINAL":
        raise ModelZooError("Model Zoo v2 requires an explicitly revealed Dataset v1 final test")
    lineage = manifest.get("dataset")
    if not isinstance(lineage, dict):
        raise ModelZooError("Model Zoo v1 dataset lineage is malformed")
    if (
        lineage.get("dataset_id") != dataset.dataset_id
        or lineage.get("deterministic_build_hash") != dataset.deterministic_build_hash
    ):
        raise ModelZooError("Model Zoo v1 artifact does not match the certified Dataset v1")
    for name in ("zoo_id", "deterministic_build_hash"):
        if not isinstance(manifest.get(name), str):
            raise ModelZooError("Model Zoo v1 immutable identity is malformed")
    return manifest


def _development_folds(
    rows: tuple[DatasetExample, ...], count: int
) -> tuple[tuple[tuple[DatasetExample, ...], tuple[DatasetExample, ...]], ...]:
    grouped: dict[tuple[datetime, datetime], list[DatasetExample]] = {}
    for row in rows:
        grouped.setdefault((row.window_start, row.window_end), []).append(row)
    windows = tuple(
        tuple(sorted(group, key=_row_key)) for _window, group in sorted(grouped.items())
    )
    if len(windows) < count + 3:
        raise ModelZooError("insufficient chronological training windows for Model Zoo v2")
    first_train_end = max(2, len(windows) // 2)
    remaining = len(windows) - first_train_end
    width = max(1, remaining // count)
    folds: list[tuple[tuple[DatasetExample, ...], tuple[DatasetExample, ...]]] = []
    for index in range(count):
        train_end = first_train_end + index * width
        validation_end = min(len(windows), train_end + width)
        if validation_end <= train_end:
            break
        train = tuple(row for window in windows[:train_end] for row in window)
        validation = tuple(row for window in windows[train_end:validation_end] for row in window)
        if train and validation:
            if max(row.window_end for row in train) > min(row.window_start for row in validation):
                raise ModelZooError("Model Zoo v2 chronological fold overlaps future rows")
            folds.append((train, validation))
    if len(folds) != count:
        raise ModelZooError("Model Zoo v2 cannot build the frozen number of development folds")
    return tuple(folds)


def _cross_fitted_predictions(
    spec: _Spec,
    folds: tuple[tuple[tuple[DatasetExample, ...], tuple[DatasetExample, ...]], ...],
    feature_names: tuple[str, ...],
    assets: tuple[str, ...],
    config: ModelZooV2Config,
) -> _Predictions:
    rows: list[DatasetExample] = []
    probabilities: list[np.ndarray] = []
    details: list[dict[str, object]] = []
    for index, (train, validation) in enumerate(folds):
        model_train = train
        calibration_rows: tuple[DatasetExample, ...] = ()
        if spec.calibration == "chronological_platt":
            model_train, calibration_rows = _calibration_partition(train)
        model = _fit_spec(spec.family, model_train, feature_names, assets, config)
        predicted = _predict(model, validation)
        calibration_detail: dict[str, object] = {"method": "identity", "fit_rows": 0}
        if spec.calibration == "chronological_platt":
            calibrator = _fit_past_only_platt(model, calibration_rows)
            if calibrator is None:
                calibration_detail = {
                    "method": "identity_fallback_insufficient_past_calibration_evidence",
                    "fit_rows": len(calibration_rows),
                }
            else:
                predicted = calibrator.transform(predicted)
                calibration_detail = {
                    "method": "platt_sigmoid",
                    "fit_rows": len(calibration_rows),
                    "fit_end": max(row.decision_timestamp for row in calibration_rows).isoformat(),
                }
        rows.extend(validation)
        probabilities.append(predicted)
        details.append(
            {
                "fold": index,
                "train_rows": len(train),
                "model_fit_rows": len(model_train),
                "validation_rows": len(validation),
                "train_window_end": max(row.window_end for row in train).isoformat(),
                "validation_window_start": min(row.window_start for row in validation).isoformat(),
                "calibration": calibration_detail,
                "development_evaluation": _development_evaluation(validation, predicted, config),
                "per_asset_probability_metrics": _per_asset_probability_metrics(
                    validation, predicted
                ),
            }
        )
    return _Predictions(tuple(rows), np.concatenate(probabilities), tuple(details))


def _calibration_partition(
    train: tuple[DatasetExample, ...],
) -> tuple[tuple[DatasetExample, ...], tuple[DatasetExample, ...]]:
    windows = sorted({(row.window_start, row.window_end) for row in train})
    split = max(2, (len(windows) * 3) // 4)
    if split >= len(windows):
        return train, ()
    cutoff = windows[split][0]
    model_train = tuple(row for row in train if row.window_end <= cutoff)
    calibration = tuple(row for row in train if row.window_start >= cutoff)
    if (
        model_train
        and calibration
        and max(row.window_end for row in model_train)
        <= min(row.window_start for row in calibration)
    ):
        return model_train, calibration
    return train, ()


def _fit_past_only_platt(model: object, rows: tuple[DatasetExample, ...]) -> PlattCalibrator | None:
    if len(rows) < 20 or len({row.label_yes for row in rows}) != 2:
        return None
    try:
        return PlattCalibrator.fit(
            _predict(model, rows), np.asarray([row.label_yes for row in rows])
        )
    except ModelZooError:
        return None


def _fit_spec(
    family: str,
    rows: tuple[DatasetExample, ...],
    feature_names: tuple[str, ...],
    assets: tuple[str, ...],
    config: ModelZooV2Config,
) -> object:
    v1_config = _v1_fit_config(config)
    if family == "market_implied":
        return _fit_market_implied(feature_names)
    if family == "logistic_l2":
        return _fit_logistic(rows, feature_names, v1_config)
    if family == "xgboost":
        return _fit_xgboost(rows, feature_names, v1_config)
    if family == "xgboost_asset_identity":
        preprocessor = Preprocessor.fit(rows, feature_names)
        matrix = _asset_matrix(preprocessor, rows, assets)
        params = _xgb_parameters(config, objective="binary:logistic")
        booster = xgb.train(
            params,
            xgb.DMatrix(
                matrix, label=np.asarray([row.label_yes for row in rows], dtype=np.float64)
            ),
            config.xgboost_rounds,
        )
        return _AssetAwareXgb(preprocessor, booster, assets)
    if family == "xgboost_market_residual":
        preprocessor = Preprocessor.fit(rows, feature_names)
        try:
            market_index = feature_names.index("market_probability_midpoint")
        except ValueError as error:
            raise ModelZooError("residual candidate needs market_probability_midpoint") from error
        residual = np.asarray(
            [row.label_yes for row in rows], dtype=np.float64
        ) - _market_probabilities(rows, market_index)
        booster = xgb.train(
            _xgb_parameters(config, objective="reg:squarederror"),
            xgb.DMatrix(preprocessor.transform(rows), label=residual),
            config.xgboost_rounds,
        )
        return _ResidualXgb(preprocessor, booster, market_index)
    raise ModelZooError("unknown Model Zoo v2 candidate family")


def _v1_fit_config(config: ModelZooV2Config) -> Any:
    # Keep v1 fit helpers as a narrow numerical implementation dependency only.
    from live15_quant.model_zoo import ModelZooConfig

    return ModelZooConfig(
        seed=config.seed,
        logistic_iterations=config.logistic_iterations,
        logistic_learning_rate=config.logistic_learning_rate,
        logistic_l2=config.logistic_l2,
        xgboost_rounds=config.xgboost_rounds,
        xgboost_max_depth=config.xgboost_max_depth,
        xgboost_learning_rate=config.xgboost_learning_rate,
    )


def _xgb_parameters(config: ModelZooV2Config, *, objective: str) -> dict[str, object]:
    return {
        "objective": objective,
        "eval_metric": "logloss" if objective == "binary:logistic" else "rmse",
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


def _asset_matrix(
    preprocessor: Preprocessor, rows: tuple[DatasetExample, ...], assets: tuple[str, ...]
) -> np.ndarray:
    base = preprocessor.transform(rows)
    identity = np.zeros((len(rows), len(assets)), dtype=np.float64)
    indexes = {asset: index for index, asset in enumerate(assets)}
    for index, row in enumerate(rows):
        asset_index = indexes.get(row.asset)
        if asset_index is not None:
            identity[index, asset_index] = 1.0
    return np.hstack((base, identity))


def _predict(model: object, rows: tuple[DatasetExample, ...]) -> np.ndarray:
    if isinstance(model, FittedModel):
        return model.predict(rows)
    predict = getattr(model, "predict", None)
    if not callable(predict):
        raise ModelZooError("Model Zoo v2 model has no prediction interface")
    return np.asarray(predict(rows), dtype=np.float64)


def _market_probabilities(rows: tuple[DatasetExample, ...], index: int) -> np.ndarray:
    values = []
    for row in rows:
        value = row.values[index]
        if value is None:
            raise ModelZooError("certified development row lacks market probability")
        values.append(float(value))
    return np.asarray(values, dtype=np.float64)


def _development_evaluation(
    rows: tuple[DatasetExample, ...], probabilities: np.ndarray, config: ModelZooV2Config
) -> dict[str, object]:
    labels = np.asarray([row.label_yes for row in rows], dtype=np.int8)
    aggregate: dict[str, object] = {
        "rows": len(rows),
        "probability_metrics": _probability_metrics(probabilities, labels, 10),
        "thresholds": {},
    }
    for threshold in config.thresholds:
        trade = _trade_evaluation(rows, probabilities, threshold, config)
        aggregate["thresholds"][str(threshold)] = trade
    return aggregate


def _per_asset_probability_metrics(
    rows: tuple[DatasetExample, ...], probabilities: np.ndarray
) -> dict[str, object]:
    labels = np.asarray([row.label_yes for row in rows], dtype=np.int8)
    result: dict[str, object] = {}
    for asset in sorted({row.asset for row in rows}):
        indexes = [index for index, row in enumerate(rows) if row.asset == asset]
        result[asset] = _probability_metrics(probabilities[indexes], labels[indexes], 10)
    return result


def _trade_evaluation(
    rows: tuple[DatasetExample, ...],
    probabilities: np.ndarray,
    threshold: Decimal,
    config: ModelZooV2Config,
) -> dict[str, object]:
    feature_indexes = {definition.name: index for index, definition in enumerate(FEATURE_REGISTRY)}
    yes_ask_index = feature_indexes["yes_ask"]
    no_ask_index = feature_indexes["no_ask"]
    fee_model = KalshiTakerFeeModel()
    gross = Decimal(0)
    fees = Decimal(0)
    net = Decimal(0)
    peak = Decimal(0)
    drawdown = Decimal(0)
    profit = Decimal(0)
    loss = Decimal(0)
    raw_edges: list[Decimal] = []
    after_cost_edges: list[Decimal] = []
    selected_rows: list[DatasetExample] = []
    selected_probabilities: list[float] = []
    per_asset: dict[str, dict[str, Decimal | int]] = {}
    adverse_net = Decimal(0)
    adverse_fees = Decimal(0)
    for row, probability in sorted(
        zip(rows, probabilities, strict=True), key=lambda pair: _row_key(pair[0])
    ):
        yes_ask = row.values[yes_ask_index]
        no_ask = row.values[no_ask_index]
        if yes_ask is None or no_ask is None:
            continue
        p = Decimal(str(float(probability)))
        yes_edge = p - yes_ask
        no_edge = Decimal(1) - p - no_ask
        if max(yes_edge, no_edge) < threshold:
            continue
        side_yes = yes_edge >= no_edge
        price = yes_ask if side_yes else no_ask
        edge = yes_edge if side_yes else no_edge
        order_id = f"model-zoo-v2-{threshold}-{row.ticker}-{row.decision_timestamp.isoformat()}"
        fee = fee_model.compute(
            order_id=order_id,
            quantity=Decimal(1),
            price=price,
            action=ExecutionAction.BUY,
        )
        fee_model.finish_order(order_id)
        won = bool(row.label_yes) == side_yes
        gross_pnl = Decimal(1) - price if won else -price
        trade_net = gross_pnl - fee.net_fee
        gross += gross_pnl
        fees += fee.net_fee
        net += trade_net
        peak = max(peak, net)
        drawdown = max(drawdown, peak - net)
        if trade_net > 0:
            profit += trade_net
        elif trade_net < 0:
            loss -= trade_net
        raw_edges.append(edge)
        after_cost_edges.append(edge - fee.net_fee)
        selected_rows.append(row)
        selected_probabilities.append(float(probability))
        asset = per_asset.setdefault(row.asset, {"trades": 0, "net_pnl": Decimal(0)})
        asset["trades"] = int(asset["trades"]) + 1
        asset["net_pnl"] = Decimal(asset["net_pnl"]) + trade_net

        # A deliberately adverse, capped one-cent entry scenario.  It is a sensitivity
        # report only, never a promotion input, because no historical fill is inferred.
        adverse_price = min(Decimal("0.99"), price + config.adverse_entry_cents)
        adverse_fee_id = f"{order_id}-adverse"
        adverse_fee = fee_model.compute(
            order_id=adverse_fee_id,
            quantity=Decimal(1),
            price=adverse_price,
            action=ExecutionAction.BUY,
        )
        fee_model.finish_order(adverse_fee_id)
        adverse_gross = Decimal(1) - adverse_price if won else -adverse_price
        adverse_net += adverse_gross - adverse_fee.net_fee
        adverse_fees += adverse_fee.net_fee

    labels = np.asarray([row.label_yes for row in selected_rows], dtype=np.int8)
    traded_metrics: dict[str, object] | None = None
    if len(selected_rows):
        traded_metrics = _probability_metrics(np.asarray(selected_probabilities), labels, 10)
    return {
        "threshold": str(threshold),
        "trade_count": len(selected_rows),
        "gross_pnl_estimate": str(gross),
        "estimated_fees_costs": str(fees),
        "net_pnl_estimate": str(net),
        "average_realized_net_pnl": str(net / len(selected_rows)) if selected_rows else None,
        "max_drawdown": str(drawdown),
        "profit_factor": str(profit / loss) if loss else None,
        "edge_decomposition": {
            "raw_model_edge_before_entry_cost": _mean_decimal(raw_edges),
            "executable_ask_edge_before_fee": _mean_decimal(raw_edges),
            "after_cost_edge": _mean_decimal(after_cost_edges),
            "semantics": (
                "entry uses certified YES/NO ask; raw and executable coincide before fees"
            ),
        },
        "cost_sensitivity": {
            "primary": "certified_executable_ask_plus_existing_kalshi_taker_fee",
            "adverse_one_cent_entry_capped": {
                "net_pnl_estimate": str(adverse_net),
                "estimated_fees_costs": str(adverse_fees),
                "assumption": (
                    "one-cent worse entry capped at 99 cents; sensitivity only, not promotion input"
                ),
            },
        },
        "traded_subset_probability_metrics": traded_metrics,
        "per_asset": {
            asset: {"trades": int(item["trades"]), "net_pnl_estimate": str(item["net_pnl"])}
            for asset, item in sorted(per_asset.items())
        },
    }


def _promotion_status(
    spec: _Spec,
    trade: dict[str, object],
    baseline: dict[str, object],
    config: ModelZooV2Config,
) -> dict[str, object]:
    if not spec.promotable:
        return {"status": "REJECTED", "reasons": ["reference_baseline_not_forward_promotable"]}
    reasons: list[str] = []
    metrics = baseline["probability_metrics"]
    # The caller adds candidate metrics to this mapping below; keeping the gate here
    # forces every threshold to use the same predeclared baseline comparison.
    candidate_metrics = trade.get("candidate_probability_metrics")
    if not isinstance(candidate_metrics, dict):
        raise ModelZooError("development candidate lacks probability metrics")
    if (
        float(candidate_metrics["brier_score"])
        > float(metrics["brier_score"]) + config.max_brier_regression_vs_market
    ):
        reasons.append("development_probability_quality_regresses_beyond_fixed_tolerance")
    if int(trade["trade_count"]) < config.min_trade_count:
        reasons.append("insufficient_development_trade_count")
    if Decimal(str(trade["net_pnl_estimate"])) <= 0:
        reasons.append("development_after_cost_net_pnl_not_positive")
    profit_factor = trade["profit_factor"]
    if profit_factor is None or Decimal(str(profit_factor)) < config.min_profit_factor:
        reasons.append("development_profit_factor_below_fixed_gate")
    if Decimal(str(trade["max_drawdown"])) > config.max_drawdown:
        reasons.append("development_drawdown_exceeds_fixed_gate")
    if int(trade["positive_net_pnl_folds"]) < config.min_positive_development_folds:
        reasons.append("development_economics_not_repeated_across_fixed_chronological_folds")
    per_asset = trade["per_asset"]
    assert isinstance(per_asset, dict)
    positive_assets = [
        item
        for item in per_asset.values()
        if isinstance(item, dict)
        and Decimal(str(item["net_pnl_estimate"])) > 0
        and int(item["trades"]) >= config.min_trades_per_positive_asset
    ]
    if len(positive_assets) < config.min_positive_assets:
        reasons.append("development_economics_not_supported_by_multiple_assets")
    positive_net = [Decimal(str(item["net_pnl_estimate"])) for item in positive_assets]
    total_positive_net = sum(positive_net, Decimal(0))
    concentration = (
        max(positive_net, default=Decimal(0)) / total_positive_net if total_positive_net else None
    )
    if concentration is not None and concentration > config.max_positive_net_pnl_concentration:
        reasons.append("development_positive_pnl_is_too_concentrated_in_one_asset")
    return {
        "status": "REJECTED" if reasons else "FORWARD_CANDIDATE",
        "reasons": reasons or ["predeclared_development_gates_passed_requires_forward_validation"],
        "positive_net_pnl_assets": len(positive_assets),
        "positive_net_pnl_concentration": str(concentration) if concentration is not None else None,
        "promotion_scope": "development_only_requires_new_forward_paper_shadow_or_demo_evidence",
    }


def _attach_candidate_metrics(candidates: dict[str, object]) -> None:
    """Attach aggregate quality to every threshold without referencing Dataset test."""

    for item in candidates.values():
        if not isinstance(item, dict):
            continue
        aggregate = item.get("aggregate")
        if not isinstance(aggregate, dict):
            continue
        metrics = aggregate.get("probability_metrics")
        thresholds = aggregate.get("thresholds")
        if not isinstance(metrics, dict) or not isinstance(thresholds, dict):
            raise ModelZooError("development aggregate is malformed")
        for trade in thresholds.values():
            if isinstance(trade, dict):
                trade["candidate_probability_metrics"] = metrics


def _attach_fold_stability(candidates: dict[str, object]) -> None:
    """Attach threshold-specific repeated-fold evidence before promotion gating."""

    for item in candidates.values():
        if not isinstance(item, dict):
            continue
        aggregate = item.get("aggregate")
        folds = item.get("folds")
        if not isinstance(aggregate, dict) or not isinstance(folds, list):
            raise ModelZooError("development candidate fold evidence is malformed")
        thresholds = aggregate.get("thresholds")
        if not isinstance(thresholds, dict):
            raise ModelZooError("development candidate threshold evidence is malformed")
        for threshold, trade in thresholds.items():
            if not isinstance(trade, dict):
                raise ModelZooError("development trade summary is malformed")
            per_fold = []
            for fold in folds:
                if not isinstance(fold, dict):
                    raise ModelZooError("development fold is malformed")
                evaluation = fold.get("development_evaluation")
                if not isinstance(evaluation, dict) or not isinstance(
                    evaluation.get("thresholds"), dict
                ):
                    raise ModelZooError("development fold threshold diagnostics are malformed")
                fold_trade = evaluation["thresholds"].get(threshold)
                if not isinstance(fold_trade, dict):
                    raise ModelZooError("development fold is missing a predeclared threshold")
                per_fold.append(fold_trade)
            trade["positive_net_pnl_folds"] = sum(
                Decimal(str(fold["net_pnl_estimate"])) > 0 for fold in per_fold
            )
            trade["profitable_fold_fraction"] = str(
                Decimal(trade["positive_net_pnl_folds"]) / Decimal(len(per_fold))
            )


def _attach_asset_cautions(candidates: dict[str, object], config: ModelZooV2Config) -> None:
    """Report train-fold heterogeneity without excluding any asset from evaluation."""

    baseline = candidates.get("market_implied_identity")
    if not isinstance(baseline, dict) or not isinstance(baseline.get("folds"), list):
        raise ModelZooError("development baseline folds are malformed")
    baseline_folds = baseline["folds"]
    for _name, item in candidates.items():
        if not isinstance(item, dict) or not isinstance(item.get("folds"), list):
            raise ModelZooError("development candidate folds are malformed")
        deltas: dict[str, list[float]] = {}
        for candidate_fold, baseline_fold in zip(item["folds"], baseline_folds, strict=True):
            if not isinstance(candidate_fold, dict) or not isinstance(baseline_fold, dict):
                raise ModelZooError("development fold diagnostics are malformed")
            candidate_assets = candidate_fold.get("per_asset_probability_metrics")
            baseline_assets = baseline_fold.get("per_asset_probability_metrics")
            if not isinstance(candidate_assets, dict) or not isinstance(baseline_assets, dict):
                raise ModelZooError("development per-asset diagnostics are malformed")
            for asset in sorted(set(candidate_assets) & set(baseline_assets)):
                candidate_metrics = candidate_assets[asset]
                baseline_metrics = baseline_assets[asset]
                if isinstance(candidate_metrics, dict) and isinstance(baseline_metrics, dict):
                    deltas.setdefault(asset, []).append(
                        float(candidate_metrics["brier_score"])
                        - float(baseline_metrics["brier_score"])
                    )
        item["asset_cautions"] = {
            asset: {
                "status": (
                    "ASSET_CAUTION"
                    if sum(delta > config.asset_caution_brier_delta for delta in values)
                    >= config.min_asset_caution_folds
                    else "NO_ASSET_CAUTION"
                ),
                "folds_observed": len(values),
                "brier_delta_vs_market_by_fold": values,
                "policy": "diagnostic only; no asset is excluded from development evaluation",
            }
            for asset, values in sorted(deltas.items())
        }


def _leaderboard(payload: dict[str, object]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, dict):
        raise ModelZooError("Model Zoo v2 payload is malformed")
    rows = []
    for name, item in sorted(candidates.items()):
        if not isinstance(item, dict):
            continue
        aggregate = item.get("aggregate")
        status = item.get("threshold_status")
        if not isinstance(aggregate, dict) or not isinstance(status, dict):
            continue
        metrics = aggregate.get("probability_metrics", {})
        thresholds = aggregate.get("thresholds", {})
        rows.append(
            {
                "candidate": name,
                "family": item.get("family"),
                "calibration": item.get("calibration"),
                "development_brier": metrics.get("brier_score")
                if isinstance(metrics, dict)
                else None,
                "development_log_loss": metrics.get("log_loss")
                if isinstance(metrics, dict)
                else None,
                "thresholds": [
                    {
                        "threshold": threshold,
                        "status": gate.get("status") if isinstance(gate, dict) else None,
                        "net_pnl": thresholds.get(threshold, {}).get("net_pnl_estimate")
                        if isinstance(thresholds.get(threshold), dict)
                        else None,
                    }
                    for threshold, gate in sorted(status.items(), key=lambda pair: Decimal(pair[0]))
                ],
            }
        )
    return _canonical_json(
        {
            "format": "live15-model-zoo-v2-development-leaderboard-v1",
            "dataset_id": payload.get("dataset", {}).get("dataset_id")
            if isinstance(payload.get("dataset"), dict)
            else None,
            "status": payload.get("status"),
            "final_test_policy": FINAL_TEST_POLICY,
            "candidates": rows,
        }
    )


def _identity_view(payload: dict[str, object]) -> dict[str, object]:
    value = __import__("json").loads(_canonical_json(payload))
    assert isinstance(value, dict)
    value.pop("created_at", None)
    return value


def _row_key(row: DatasetExample) -> tuple[datetime, datetime, str, datetime]:
    return row.window_start, row.window_end, row.ticker, row.decision_timestamp


def _mean_decimal(values: list[Decimal]) -> str | None:
    return str(sum(values, Decimal(0)) / len(values)) if values else None
