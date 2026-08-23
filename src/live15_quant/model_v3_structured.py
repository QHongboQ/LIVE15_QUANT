"""Development-only, leakage-safe structured experts for Model Architecture v3.

This module deliberately consumes *only* Dataset v1's certified train split.
It never opens the recorder database and never accesses Dataset v1 validation
or revealed final-test rows.  Its historical book targets are explicitly
``kalshi_rest`` diagnostics: an atomic WebSocket microstructure model requires
a separately verified archive/replay dataset and therefore fails closed here.
"""

from __future__ import annotations

import json
import math
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from live15_quant.model_v3 import ModelV3Error, RegimeInputs, RuleAssistedRegimeExpert
from live15_quant.models import Asset

if TYPE_CHECKING:
    from live15_quant.model_zoo import CertifiedDataset, DatasetExample

V3_STRUCTURED_VERSION = "1.0.0"
FINAL_TEST_POLICY = "DATASET_V1_REVEALED_FINAL_NOT_USED_FOR_V3_STRUCTURED_DEVELOPMENT"
PATH_HORIZONS = (30, 60, 180, 300)
MICRO_HORIZONS = (30, 60, 180, 300)
INPUT_FEATURES = (
    "underlying_price",
    "target_price",
    "signed_distance_to_target",
    "normalized_distance_to_target",
    "time_remaining_seconds",
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
    "volatility_change",
    "volatility_regime_ratio",
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "yes_spread",
    "yes_midpoint",
    "yes_top_depth",
    "no_top_depth",
    "yes_cumulative_depth",
    "no_cumulative_depth",
    "top_depth_imbalance",
    "orderbook_imbalance",
    "depth_ratio",
    "spread_depth_interaction",
)


class V3StructuredError(ModelV3Error):
    """A structured development lineage or temporal invariant failed."""


@dataclass(frozen=True, slots=True)
class V3StructuredConfig:
    seed: int = 20260823
    folds: int = 3
    xgboost_rounds: int = 60
    max_depth: int = 3
    learning_rate: float = 0.05
    direction_band: Decimal = Decimal("0.0005")
    min_independent_events: int = 300
    min_examples_per_horizon: int = 200
    min_examples_per_fold: int = 40
    min_calendar_days: int = 2
    min_positive_folds: int = 2
    min_trade_count: int = 40
    max_positive_pnl_concentration: Decimal = Decimal("0.75")
    conservative_cost_cents: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if self.folds < 2 or min(self.xgboost_rounds, self.max_depth) <= 0:
            raise ValueError("structured development configuration is invalid")
        if not Decimal(0) < self.direction_band < Decimal(1):
            raise ValueError("direction band must be in (0, 1)")
        if (
            min(self.min_independent_events, self.min_examples_per_horizon, self.min_calendar_days)
            <= 0
        ):
            raise ValueError("structured evidence gates must be positive")
        if self.min_examples_per_fold <= 0:
            raise ValueError("minimum examples per fold must be positive")

    def payload(self) -> dict[str, object]:
        item = asdict(self)
        for name in ("direction_band", "max_positive_pnl_concentration", "conservative_cost_cents"):
            item[name] = str(item[name])
        return item


@dataclass(frozen=True, slots=True)
class StructuredSample:
    event_id: str
    asset: str
    ticker: str
    decision_timestamp: datetime
    target_timestamp: datetime
    window_start: datetime
    window_end: datetime
    horizon_seconds: int
    values: tuple[Decimal, ...]
    underlying_return: Decimal
    contract_midpoint_return: Decimal

    def __post_init__(self) -> None:
        for value in (
            self.decision_timestamp,
            self.target_timestamp,
            self.window_start,
            self.window_end,
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise V3StructuredError("structured timestamps must be UTC-aware")
        if (
            not self.window_start
            <= self.decision_timestamp
            < self.target_timestamp
            < self.window_end
        ):
            raise V3StructuredError(
                "structured target must be strictly after input within one event"
            )
        if not self.values or not all(value.is_finite() for value in self.values):
            raise V3StructuredError("structured inputs must be observed finite Decimal values")


@dataclass(frozen=True, slots=True)
class StructuredDataset:
    samples_by_horizon: dict[int, tuple[StructuredSample, ...]]
    skipped_missing_inputs: int
    skipped_invalid_target_base: int
    source_provenance: str
    atomic_ws_microstructure_status: str

    @property
    def event_ids(self) -> frozenset[str]:
        return frozenset(
            sample.event_id for values in self.samples_by_horizon.values() for sample in values
        )

    @property
    def calendar_days(self) -> frozenset[str]:
        return frozenset(
            sample.decision_timestamp.astimezone(UTC).date().isoformat()
            for values in self.samples_by_horizon.values()
            for sample in values
        )


@dataclass(frozen=True, slots=True)
class V3StructuredSummary:
    artifact_id: str
    artifact_path: Path
    status: str
    dataset_id: str
    evidence_status: str
    reused_existing_artifact: bool


def _event_id(row: DatasetExample) -> str:
    return f"{row.asset}:{row.ticker}:{row.window_start.isoformat()}:{row.window_end.isoformat()}"


def _value_map(row: DatasetExample, indexes: tuple[int, ...]) -> tuple[Decimal, ...] | None:
    values = tuple(row.values[index] for index in indexes)
    if any(value is None for value in values):
        return None
    return tuple(value for value in values if value is not None)


def build_structured_dataset(dataset: CertifiedDataset) -> StructuredDataset:
    """Build targets from train rows only; exact future rows are never inferred."""

    missing = [name for name in INPUT_FEATURES if name not in dataset.feature_names]
    if missing:
        raise V3StructuredError(f"certified feature schema lacks structured input {missing[0]}")
    indexes = tuple(dataset.feature_names.index(name) for name in INPUT_FEATURES)
    underlying_index = dataset.feature_names.index("underlying_price")
    midpoint_index = dataset.feature_names.index("yes_midpoint")
    grouped: dict[str, list[DatasetExample]] = {}
    # This is intentionally the only split read.  Do not replace it with a
    # union of splits: Dataset v1 final test is permanently revealed.
    for row in dataset.splits["train"]:
        grouped.setdefault(_event_id(row), []).append(row)
    samples: dict[int, list[StructuredSample]] = {seconds: [] for seconds in PATH_HORIZONS}
    skipped = 0
    skipped_invalid_target_base = 0
    for event_id, rows in sorted(grouped.items()):
        ordered = sorted(rows, key=lambda item: item.decision_timestamp)
        by_time: dict[datetime, DatasetExample] = {}
        for item in ordered:
            if item.decision_timestamp in by_time:
                raise V3StructuredError(
                    "duplicate Dataset v1 decision timestamp within an event is not deterministic"
                )
            by_time[item.decision_timestamp] = item
        for row in ordered:
            current_inputs = _value_map(row, indexes)
            current_underlying = row.values[underlying_index]
            current_midpoint = row.values[midpoint_index]
            if current_inputs is None or current_underlying is None or current_midpoint is None:
                skipped += 1
                continue
            if (
                not all(value.is_finite() for value in current_inputs)
                or not current_underlying.is_finite()
                or not current_midpoint.is_finite()
                or current_underlying <= 0
                or current_midpoint <= 0
            ):
                skipped_invalid_target_base += 1
                continue
            for seconds in PATH_HORIZONS:
                target_time = row.decision_timestamp + timedelta(seconds=seconds)
                target = by_time.get(target_time)
                if target is None:
                    continue
                future_underlying = target.values[underlying_index]
                future_midpoint = target.values[midpoint_index]
                if (
                    future_underlying is None
                    or future_midpoint is None
                    or not future_underlying.is_finite()
                    or not future_midpoint.is_finite()
                    or future_underlying <= 0
                ):
                    continue
                samples[seconds].append(
                    StructuredSample(
                        event_id,
                        row.asset,
                        row.ticker,
                        row.decision_timestamp,
                        target_time,
                        row.window_start,
                        row.window_end,
                        seconds,
                        current_inputs,
                        future_underlying / current_underlying - Decimal(1),
                        future_midpoint / current_midpoint - Decimal(1),
                    )
                )
    frozen = {
        seconds: tuple(sorted(values, key=lambda item: (item.decision_timestamp, item.event_id)))
        for seconds, values in samples.items()
    }
    return StructuredDataset(
        frozen,
        skipped,
        skipped_invalid_target_base,
        "kalshi_rest_structured_microstructure_baseline",
        "INSUFFICIENT_MICROSTRUCTURE_EVIDENCE",
    )


def _folds(
    samples: tuple[StructuredSample, ...], count: int
) -> tuple[tuple[tuple[StructuredSample, ...], tuple[StructuredSample, ...]], ...]:
    # Events from different assets can share one 15-minute market window.  A
    # split must keep that whole time block together: otherwise one asset's
    # future target can overlap another asset's validation decision clock.
    grouped: dict[datetime, list[StructuredSample]] = {}
    for sample in samples:
        grouped.setdefault(sample.window_start, []).append(sample)
    windows = tuple(
        tuple(sorted(value, key=lambda item: item.decision_timestamp))
        for _, value in sorted(grouped.items())
    )
    if len(windows) < count + 3:
        raise V3StructuredError("insufficient window-grouped chronological development evidence")
    first_train_end = max(2, len(windows) // 2)
    width = max(1, (len(windows) - first_train_end) // count)
    result: list[tuple[tuple[StructuredSample, ...], tuple[StructuredSample, ...]]] = []
    for index in range(count):
        train_end = first_train_end + index * width
        validation_end = min(len(windows), train_end + width)
        if validation_end <= train_end:
            break
        train = tuple(item for window in windows[:train_end] for item in window)
        validation = tuple(item for window in windows[train_end:validation_end] for item in window)
        if not train or not validation:
            continue
        if {item.event_id for item in train} & {item.event_id for item in validation}:
            raise V3StructuredError("event identity crosses structured development folds")
        if max(item.target_timestamp for item in train) >= min(
            item.decision_timestamp for item in validation
        ):
            raise V3StructuredError("structured target overlaps a future validation window")
        result.append((train, validation))
    if len(result) != count:
        raise V3StructuredError("insufficient full chronological structured folds")
    return tuple(result)


def _matrix(samples: tuple[StructuredSample, ...]) -> np.ndarray:
    return np.asarray(
        [[float(value) for value in sample.values] for sample in samples], dtype=np.float64
    )


def _target_value(sample: StructuredSample, target: str) -> Decimal:
    if target == "underlying_return":
        return sample.underlying_return
    if target == "contract_midpoint_return":
        return sample.contract_midpoint_return
    raise V3StructuredError("unknown structured target")


def _fit_horizon(
    train: tuple[StructuredSample, ...], config: V3StructuredConfig, target: str
) -> tuple[Any, Any]:
    from live15_quant.model_zoo import xgb

    matrix = _matrix(train)
    returns = np.asarray([float(_target_value(item, target)) for item in train], dtype=np.float64)
    labels = np.asarray(
        [
            0
            if _target_value(item, target) < -config.direction_band
            else 2
            if _target_value(item, target) > config.direction_band
            else 1
            for item in train
        ],
        dtype=np.int32,
    )
    common = {
        "max_depth": config.max_depth,
        "eta": config.learning_rate,
        "seed": config.seed,
        "nthread": 1,
        "verbosity": 0,
    }
    regression = xgb.train(
        {**common, "objective": "reg:squarederror"},
        xgb.DMatrix(matrix, label=returns),
        num_boost_round=config.xgboost_rounds,
    )
    direction = xgb.train(
        {**common, "objective": "multi:softprob", "num_class": 3},
        xgb.DMatrix(matrix, label=labels),
        num_boost_round=config.xgboost_rounds,
    )
    return regression, direction


def _metrics(
    samples: tuple[StructuredSample, ...],
    regression: Any,
    direction: Any,
    config: V3StructuredConfig,
    target: str,
) -> dict[str, object]:
    from live15_quant.model_zoo import xgb

    matrix = xgb.DMatrix(_matrix(samples))
    expected = np.asarray(regression.predict(matrix), dtype=np.float64)
    probabilities = np.asarray(direction.predict(matrix), dtype=np.float64).reshape(
        (len(samples), 3)
    )
    actual = np.asarray([float(_target_value(item, target)) for item in samples], dtype=np.float64)
    labels = np.asarray(
        [
            0
            if _target_value(item, target) < -config.direction_band
            else 2
            if _target_value(item, target) > config.direction_band
            else 1
            for item in samples
        ],
        dtype=np.int32,
    )
    up = (labels == 2).astype(np.float64)
    p_up = probabilities[:, 2]
    predicted_labels = np.argmax(probabilities, axis=1)
    return {
        "examples": len(samples),
        "events": len({item.event_id for item in samples}),
        "mae_return": float(np.mean(np.abs(expected - actual))),
        "rmse_return": float(math.sqrt(float(np.mean((expected - actual) ** 2)))),
        "directional_brier_up": float(np.mean((p_up - up) ** 2)),
        "directional_accuracy_three_class": float(np.mean(predicted_labels == labels)),
        "actual_direction_class_counts": {
            "down": int(np.sum(labels == 0)),
            "flat": int(np.sum(labels == 1)),
            "up": int(np.sum(labels == 2)),
        },
        "mean_probability_up": float(np.mean(p_up)),
        "mean_probability_down": float(np.mean(probabilities[:, 0])),
        "probability_mass_valid": bool(np.allclose(np.sum(probabilities, axis=1), 1.0, atol=1e-6)),
    }


def _horizon_status(
    folds: list[dict[str, object]], config: V3StructuredConfig, *, diagnostic_only: bool
) -> str:
    if any(int(item["examples"]) < config.min_examples_per_fold for item in folds):
        return "REJECTED_HORIZON_INSUFFICIENT_FOLD_EVIDENCE"
    if diagnostic_only:
        return "DIAGNOSTIC_ONLY_REST_PROVENANCE"
    return "TRAIN_ONLY_PATH_DEVELOPMENT"


def _model_bytes(model: Any) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        path = Path(handle.name)
    try:
        model.save_model(path)
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


def evaluate_rule_assisted_regime(dataset: CertifiedDataset) -> dict[str, object]:
    """Summarize train-only as-of regime labels without treating UNKNOWN as zero.

    The rule baseline uses certified decision-time returns and volatility.  Its
    reversal input is deliberately binary and transparent: a non-zero short
    and medium return with opposite signs is elevated reversal risk.  This is
    a typed diagnostic, not a fitted or promoted trading signal.
    """

    required = (
        "return_30s",
        "return_120s",
        "realized_volatility_60s",
    )
    missing = [name for name in required if name not in dataset.feature_names]
    if missing:
        raise V3StructuredError(f"certified feature schema lacks regime input {missing[0]}")
    indexes = {name: dataset.feature_names.index(name) for name in required}
    expert = RuleAssistedRegimeExpert()
    counts: Counter[str] = Counter()
    per_asset: dict[str, Counter[str]] = {}
    unavailable = 0
    for row in dataset.splits["train"]:
        short = row.values[indexes["return_30s"]]
        medium = row.values[indexes["return_120s"]]
        volatility = row.values[indexes["realized_volatility_60s"]]
        reversal = (
            None
            if short is None or medium is None
            else Decimal(1)
            if short * medium < 0
            else Decimal(0)
        )
        prediction = expert.predict(
            Asset(row.asset),
            RegimeInputs(row.decision_timestamp, short, medium, volatility, reversal),
        )
        asset_counts = per_asset.setdefault(row.asset, Counter())
        if prediction.data_status != "ready":
            unavailable += 1
            counts["DATA_UNAVAILABLE"] += 1
            asset_counts["DATA_UNAVAILABLE"] += 1
            continue
        for label in sorted(prediction.labels, key=str):
            counts[label.value] += 1
            asset_counts[label.value] += 1
    return {
        "provider": "RuleAssistedRegimeExpert",
        "status": "typed_baseline",
        "source_split": "Dataset v1 train only",
        "reversal_policy": "short_return_30s and medium_return_120s have opposite signs",
        "unknown_policy": "DATA_UNAVAILABLE; never zero-filled",
        "rows_evaluated": len(dataset.splits["train"]),
        "data_unavailable_rows": unavailable,
        "label_counts": dict(sorted(counts.items())),
        "per_asset_label_counts": {
            asset: dict(sorted(labels.items())) for asset, labels in sorted(per_asset.items())
        },
    }


class V3StructuredDevelopment:
    """Build immutable train-internal structured research artifacts only."""

    def __init__(
        self,
        dataset: CertifiedDataset,
        artifact_root: Path,
        config: V3StructuredConfig | None = None,
    ) -> None:
        self.dataset = dataset
        self.artifact_root = artifact_root.resolve()
        self.config = config or V3StructuredConfig()

    def build(self) -> V3StructuredSummary:
        from live15_quant.model_zoo import (
            _atomic_publish,
            _hash_object,
            _sha256_bytes,
            current_git_sha,
        )

        structured = build_structured_dataset(self.dataset)
        regime_evaluation = evaluate_rule_assisted_regime(self.dataset)
        event_count = len(structured.event_ids)
        day_count = len(structured.calendar_days)
        per_horizon = {str(key): len(value) for key, value in structured.samples_by_horizon.items()}
        evidence_ready = (
            event_count >= self.config.min_independent_events
            and day_count >= self.config.min_calendar_days
            and all(count >= self.config.min_examples_per_horizon for count in per_horizon.values())
        )
        evidence_status = (
            "READY_STRUCTURED_EVIDENCE" if evidence_ready else "INSUFFICIENT_SEQUENCE_EVIDENCE"
        )
        path_evaluations: dict[str, object] = {}
        microstructure_evaluations: dict[str, object] = {}
        files: dict[str, bytes] = {}
        if evidence_ready:
            for target, prefix, destination in (
                ("underlying_return", "path", path_evaluations),
                ("contract_midpoint_return", "microstructure_rest", microstructure_evaluations),
            ):
                for seconds, samples in structured.samples_by_horizon.items():
                    fold_metrics: list[dict[str, object]] = []
                    for train, validation in _folds(samples, self.config.folds):
                        regression, direction = _fit_horizon(train, self.config, target)
                        fold_metrics.append(
                            _metrics(validation, regression, direction, self.config, target)
                        )
                    regression, direction = _fit_horizon(samples, self.config, target)
                    regression_bytes, direction_bytes = (
                        _model_bytes(regression),
                        _model_bytes(direction),
                    )
                    return_file = f"model__{prefix}__return__{seconds}s.json"
                    direction_file = f"model__{prefix}__direction__{seconds}s.json"
                    files[return_file] = regression_bytes
                    files[direction_file] = direction_bytes
                    destination[str(seconds)] = {
                        "status": _horizon_status(
                            fold_metrics,
                            self.config,
                            diagnostic_only=target == "contract_midpoint_return",
                        ),
                        "folds": fold_metrics,
                        "full_train": _metrics(samples, regression, direction, self.config, target),
                        "model_files": {
                            "return": return_file,
                            "direction": direction_file,
                        },
                    }
        model_file_sha256 = {
            name: _sha256_bytes(contents) for name, contents in sorted(files.items())
        }
        # REST historical book targets are a useful structured diagnostic, but
        # cannot promote an atomic-WS live candidate.  This is a hard safety
        # gate, independent from optimistic in-sample fold metrics.
        candidate_status = "NO_V3_FORWARD_CANDIDATE"
        promotion_reason = "atomic_ws_microstructure_evidence_required"
        payload: dict[str, object] = {
            "format": "live15-model-v3-structured-development-v1",
            "version": V3_STRUCTURED_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "dataset": {
                "dataset_id": self.dataset.dataset_id,
                "deterministic_build_hash": self.dataset.deterministic_build_hash,
            },
            "final_test": {
                "state": "REVEALED_FINAL",
                "policy": FINAL_TEST_POLICY,
                "rows_consumed": False,
            },
            "development_policy": {
                "source_split": "Dataset v1 train only",
                "folds": (
                    "event-grouped chronological expanding; target before next validation decision"
                ),
                "feature_policy": "typed null inputs rejected; no zero/forward/backfill",
                "source_provenance": structured.source_provenance,
                "atomic_ws_microstructure": structured.atomic_ws_microstructure_status,
            },
            "config": self.config.payload(),
            "input_features": list(INPUT_FEATURES),
            "targets": {
                "path_horizons_seconds": list(PATH_HORIZONS),
                "microstructure_horizons_seconds": list(MICRO_HORIZONS),
                "path": "future certified underlying return",
                "microstructure": "future REST yes midpoint return; diagnostic only",
            },
            "evidence": {
                "status": evidence_status,
                "independent_events": event_count,
                "calendar_days": sorted(structured.calendar_days),
                "raw_examples_per_horizon": per_horizon,
                "skipped_missing_inputs": structured.skipped_missing_inputs,
                "skipped_invalid_target_base": structured.skipped_invalid_target_base,
                "not_independent_samples_note": (
                    "multiple rows per event are grouped and never cross folds"
                ),
            },
            "path_evaluations": path_evaluations,
            "microstructure_evaluations": {
                "provenance": structured.source_provenance,
                "promotion_eligible": False,
                "reason": "atomic_ws_microstructure_evidence_required",
                "horizons": microstructure_evaluations,
            },
            "regime": regime_evaluation,
            "dynamic_decision_development": {
                "status": "DATA_UNAVAILABLE",
                "reason": "atomic_ws_microstructure_evidence_required",
                "requires": [
                    "frozen terminal probability expert",
                    "as-of path expert",
                    "atomic synchronized WS microstructure expert",
                    "rule-assisted regime",
                    "executable book and fee inputs",
                ],
                "safety": (
                    "no dynamic BUY/REDUCE/TAKE_PROFIT/CUT_LOSS comparison is emitted "
                    "from REST diagnostic microstructure"
                ),
            },
            "v2_static_vs_v3_dynamic_development": {
                "status": "NOT_COMPARABLE_FAIL_CLOSED",
                "reason": "atomic_ws_microstructure_evidence_required",
                "v2_policy": "static entry then hold to Kalshi finalized settlement",
                "v3_policy": "dynamic executable-price-and-fee EV policy",
                "note": (
                    "Comparing PnL before a valid atomic-WS v3 input exists would fabricate "
                    "a dynamic-exit result."
                ),
            },
            "candidate_gate": {
                "status": "BLOCKED",
                "priority": "capital_preservation > stability > positive_expectancy > return",
                "required": [
                    "after-cost net PnL",
                    "max drawdown",
                    "profit factor",
                    "average and tail loss",
                    "chronological-fold consistency",
                    "cross-asset concentration",
                    "trade count and HOLD selectivity",
                    "plus-1-cent conservative execution sensitivity",
                ],
                "blocked_by": "atomic_ws_microstructure_evidence_required",
            },
            "candidate": {
                "status": candidate_status,
                "promotion_reason": promotion_reason,
                "default_allow_add": False,
                "paper_activation": False,
            },
            "lineage": {
                "code_git_sha": current_git_sha(),
                "source_sha256": _sha256_bytes(Path(__file__).read_bytes()),
                "dataset_final_test_revealed": True,
                "model_files_sha256": model_file_sha256,
            },
        }
        identity = {key: value for key, value in payload.items() if key != "created_at"}
        build_hash = _hash_object(identity)
        artifact_id = f"live15-model-v3-structured-{build_hash[:20]}"
        payload["artifact_id"] = artifact_id
        payload["deterministic_build_hash"] = build_hash
        # The human-readable report is part of immutable artifact bytes, so it
        # must omit wall-clock publication time just like the identity hash.
        # Publication time remains in manifest.json for auditability.
        report_payload = {key: value for key, value in payload.items() if key != "created_at"}
        files["development_report.json"] = (
            json.dumps(report_payload, sort_keys=True, indent=2, default=str).encode("utf-8")
            + b"\n"
        )
        reused = _atomic_publish(self.artifact_root / "model_v3", artifact_id, files, payload)
        return V3StructuredSummary(
            artifact_id,
            self.artifact_root / "model_v3" / artifact_id,
            candidate_status,
            self.dataset.dataset_id,
            evidence_status,
            reused,
        )
