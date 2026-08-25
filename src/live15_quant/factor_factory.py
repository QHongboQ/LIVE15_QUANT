"""Leakage-safe symbolic factor factory primitives for offline research.

FACTOR-001 intentionally stops at a bounded, deterministic DSL/VM and Factor
Zoo manifest.  It has no model, Paper, Recorder, filesystem, network, or
dynamic-evaluation capability.  Dataset v2 is accepted as lineage only; the
factory never opens the large dataset artifacts itself.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from live15_quant.feature_registry import FEATURE_BY_NAME, FEATURE_SCHEMA_VERSION
from live15_quant.model_vnext_contract import LeakageChecker, ObservationProvenance

FACTOR_DSL_VERSION = "1.0.0"
FACTOR_OPERATOR_VERSION = "1.0.0"
DATASET_V2_ID = "live15-dataset-v2-4bb4934bf328b6b024ff"
DATASET_V2_HOLDOUT_STATE = "UNREVEALED_FROZEN"
FACTOR_STATUS_DEVELOPMENT = "VALIDATED_DEVELOPMENT"
FACTOR_STATUS_PROPOSED = "PROPOSED"
FACTOR_STATUS_REJECTED_LEAKAGE = "REJECTED_LEAKAGE"
FACTOR_STATUS_REJECTED_UNSTABLE = "REJECTED_UNSTABLE"
FACTOR_STATUS_REJECTED_REDUNDANT = "REJECTED_REDUNDANT"
FACTOR_STATUS_DEFERRED = "DEFERRED_MORE_EVIDENCE"


class FactorError(ValueError):
    """Base error for invalid factor definitions or evaluation contracts."""


class FactorComplexityExceeded(FactorError):
    """Raised when a factor exceeds the frozen FACTOR-001 budget."""


class FactorEvaluationBlocked(FactorError):
    """Raised when dataset, holdout, split, or leakage policy is violated."""


class MissingReason(StrEnum):
    NO_OBSERVATION = "no_observation"
    NOT_ENOUGH_LOOKBACK = "not_enough_lookback"
    SOURCE_UNAVAILABLE = "source_unavailable"
    STALE = "stale"
    DIVISION_BY_ZERO = "division_by_zero"
    GATE_CLOSED = "gate_closed"
    INVALID = "invalid"


OPERATORS = {
    "NEG": 1,
    "ABS": 1,
    "SIGN": 1,
    "ADD": 2,
    "SUB": 2,
    "MUL": 2,
    "SAFE_DIV": 2,
    "DELAY1": 1,
    "DECAY": 1,
    "ROLLING_MEAN": 2,
    "ROLLING_STD": 2,
    "GATE": 2,
}
UNARY_OPERATORS = {"NEG", "ABS", "SIGN", "DELAY1", "DECAY"}
ROLLING_OPERATORS = {"ROLLING_MEAN", "ROLLING_STD"}


@dataclass(frozen=True, slots=True)
class FactorExpression:
    """Typed AST node; only primitive, constant, and registered operator nodes exist."""

    kind: str
    name: str | None = None
    value: float | None = None
    operator: str | None = None
    args: tuple[FactorExpression, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"primitive", "constant", "operator"}:
            raise FactorError("invalid factor expression kind")
        if self.kind == "primitive":
            if not self.name or self.name not in FEATURE_BY_NAME:
                raise FactorError("UNKNOWN_PRIMITIVE_FEATURE")
            if self.value is not None or self.operator is not None or self.args:
                raise FactorError("primitive node has unexpected fields")
        elif self.kind == "constant":
            if self.value is None or not math.isfinite(self.value):
                raise FactorError("factor constants must be finite")
            if self.name is not None or self.operator is not None or self.args:
                raise FactorError("constant node has unexpected fields")
        else:
            if self.operator not in OPERATORS:
                raise FactorError("UNKNOWN_FACTOR_OPERATOR")
            if len(self.args) != OPERATORS[self.operator]:
                raise FactorError("factor operator arity mismatch")
            if self.name is not None or self.value is not None:
                raise FactorError("operator node has unexpected fields")


def primitive(name: str) -> FactorExpression:
    return FactorExpression("primitive", name=name)


def constant(value: float) -> FactorExpression:
    return FactorExpression("constant", value=float(value))


def operation(operator: str, *args: FactorExpression) -> FactorExpression:
    return FactorExpression("operator", operator=operator, args=tuple(args))


def parse_expression(payload: str | Mapping[str, Any]) -> FactorExpression:
    """Parse the compact JSON DSL without executing arbitrary Python."""

    raw: Any = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(raw, Mapping):
        raise FactorError("factor expression must be an object")
    keys = set(raw)
    if keys == {"feature"} and isinstance(raw["feature"], str):
        return primitive(raw["feature"])
    if (
        keys == {"const"}
        and isinstance(raw["const"], (int, float))
        and not isinstance(raw["const"], bool)
    ):
        return constant(float(raw["const"]))
    if keys == {"op", "args"} and isinstance(raw["op"], str) and isinstance(raw["args"], list):
        return operation(raw["op"], *(parse_expression(item) for item in raw["args"]))
    raise FactorError("malformed factor expression")


def _expression_payload(expression: FactorExpression) -> dict[str, Any]:
    if expression.kind == "primitive":
        return {"feature": expression.name}
    if expression.kind == "constant":
        return {"const": expression.value}
    return {
        "op": expression.operator,
        "args": [_expression_payload(arg) for arg in expression.args],
    }


def canonical_expression(expression: FactorExpression) -> str:
    return json.dumps(_expression_payload(expression), separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class ComplexityBudget:
    max_depth: int = 3
    max_operators: int = 5
    max_primitives: int = 6
    max_lookback_seconds: int = 300


DEFAULT_COMPLEXITY_BUDGET = ComplexityBudget()


@dataclass(frozen=True, slots=True)
class ExpressionComplexity:
    depth: int
    operators: int
    primitives: int


def _complexity(expression: FactorExpression, depth: int = 1) -> ExpressionComplexity:
    if expression.kind != "operator":
        return ExpressionComplexity(depth, 0, int(expression.kind == "primitive"))
    children = [_complexity(arg, depth + 1) for arg in expression.args]
    return ExpressionComplexity(
        max(item.depth for item in children),
        1 + sum(item.operators for item in children),
        sum(item.primitives for item in children),
    )


def _required_lookback(expression: FactorExpression) -> int:
    if expression.kind == "primitive":
        return FEATURE_BY_NAME[expression.name].lookback_seconds  # type: ignore[index]
    if expression.kind == "constant":
        return 0
    children = [_required_lookback(arg) for arg in expression.args]
    child_max = max(children, default=0)
    if expression.operator in {"DELAY1", "DECAY"}:
        return child_max + 1
    if expression.operator in ROLLING_OPERATORS:
        window = expression.args[1]
        if window.kind != "constant" or window.value is None or window.value <= 0:
            raise FactorError("rolling window must be a positive constant")
        if int(window.value) != window.value:
            raise FactorError("rolling window must be an integer number of seconds")
        return max(children[0], int(window.value))
    return child_max


def _dependencies(expression: FactorExpression) -> tuple[str, ...]:
    names: set[str] = set()
    if expression.kind == "primitive":
        names.add(expression.name or "")
    for arg in expression.args:
        names.update(_dependencies(arg))
    return tuple(sorted(name for name in names if name))


def validate_complexity(
    expression: FactorExpression, budget: ComplexityBudget | None = None
) -> ExpressionComplexity:
    budget = budget or DEFAULT_COMPLEXITY_BUDGET
    complexity = _complexity(expression)
    lookback = _required_lookback(expression)
    if (
        complexity.depth > budget.max_depth
        or complexity.operators > budget.max_operators
        or complexity.primitives > budget.max_primitives
        or lookback > budget.max_lookback_seconds
    ):
        raise FactorComplexityExceeded("FACTOR_COMPLEXITY_EXCEEDED")
    return complexity


@dataclass(frozen=True, slots=True)
class FactorSpec:
    factor_id: str
    canonical_formula: str
    expression: FactorExpression
    primitive_dependencies: tuple[str, ...]
    complexity: ExpressionComplexity
    required_lookback_seconds: int
    dataset_id: str
    experiment_id: str
    dsl_version: str = FACTOR_DSL_VERSION
    operator_version: str = FACTOR_OPERATOR_VERSION


def make_factor(
    expression: FactorExpression,
    *,
    experiment_id: str,
    dataset_id: str = DATASET_V2_ID,
    budget: ComplexityBudget | None = None,
) -> FactorSpec:
    complexity = validate_complexity(expression, budget)
    formula = canonical_expression(expression)
    lookback = _required_lookback(expression)
    identity = json.dumps(
        {
            "dataset_id": dataset_id,
            "dsl_version": FACTOR_DSL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "operator_version": FACTOR_OPERATOR_VERSION,
            "formula": formula,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    factor_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return FactorSpec(
        factor_id,
        formula,
        expression,
        _dependencies(expression),
        complexity,
        lookback,
        dataset_id,
        experiment_id,
    )


@dataclass(frozen=True, slots=True)
class TimedValue:
    timestamp: datetime
    value: float | None
    received_timestamp: datetime
    source_timestamp: datetime | None = None
    missing_reason: str | None = None
    backfilled: bool = False
    synthetic: bool = False

    def __post_init__(self) -> None:
        for value in (self.timestamp, self.received_timestamp, self.source_timestamp):
            if value is not None and value.tzinfo is None:
                raise FactorError("factor timestamps must be timezone-aware")
        if self.value is not None and not math.isfinite(self.value):
            raise FactorError("factor observations must be finite")
        if (self.value is None) != (self.missing_reason is not None):
            raise FactorError("factor missing values require an explicit reason")


@dataclass(frozen=True, slots=True)
class FactorContext:
    decision_timestamp: datetime
    current: Mapping[str, TimedValue]
    history: Mapping[str, tuple[TimedValue, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.decision_timestamp.tzinfo is None:
            raise FactorError("decision timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class FactorValue:
    value: float | None
    missing_reason: str | None = None

    @classmethod
    def missing(cls, reason: MissingReason | str) -> FactorValue:
        return cls(None, str(reason))

    @classmethod
    def present(cls, value: float) -> FactorValue:
        if not math.isfinite(value):
            return cls.missing(MissingReason.INVALID)
        return cls(value, None)


def _as_of_provenance(name: str, point: TimedValue) -> ObservationProvenance:
    return ObservationProvenance(
        name=name,
        observation_timestamp=point.timestamp,
        received_timestamp=point.received_timestamp,
        source_timestamp=point.source_timestamp,
        backfilled=point.backfilled,
        synthetic=point.synthetic,
    )


class SafeFactorVM:
    """Deterministic evaluator over explicit as-of histories."""

    def validate_context(self, factor: FactorSpec, context: FactorContext) -> None:
        observations: list[ObservationProvenance] = []
        for name in factor.primitive_dependencies:
            points = tuple(context.history.get(name, ())) + (
                (context.current[name],) if name in context.current else ()
            )
            for point in points:
                if point.timestamp > context.decision_timestamp:
                    raise FactorEvaluationBlocked("FACTOR_LOOKAHEAD_DETECTED")
                observations.append(_as_of_provenance(name, point))
        try:
            LeakageChecker().check_features(context.decision_timestamp, observations)
        except Exception as exc:  # normalize all contract failures at the factor boundary
            raise FactorEvaluationBlocked("FACTOR_LEAKAGE_CHECK_FAILED") from exc

    def evaluate(self, factor: FactorSpec, context: FactorContext) -> FactorValue:
        self.validate_context(factor, context)
        return self._evaluate(factor.expression, context, context.decision_timestamp)

    def _points(self, name: str, context: FactorContext, as_of: datetime) -> tuple[TimedValue, ...]:
        points = list(context.history.get(name, ()))
        if name in context.current:
            points.append(context.current[name])
        points = [point for point in points if point.timestamp <= as_of]
        points.sort(key=lambda point: point.timestamp)
        unique: dict[datetime, TimedValue] = {point.timestamp: point for point in points}
        return tuple(unique[timestamp] for timestamp in sorted(unique))

    def _primitive(self, name: str, context: FactorContext, as_of: datetime) -> FactorValue:
        points = self._points(name, context, as_of)
        if not points:
            return FactorValue.missing(MissingReason.NO_OBSERVATION)
        point = points[-1]
        if point.value is None:
            return FactorValue.missing(point.missing_reason or MissingReason.SOURCE_UNAVAILABLE)
        return FactorValue.present(point.value)

    def _timestamps(self, context: FactorContext, as_of: datetime) -> tuple[datetime, ...]:
        timestamps = {
            point.timestamp
            for name in context.current.keys() | context.history.keys()
            for point in self._points(name, context, as_of)
        }
        return tuple(sorted(timestamps))

    def _evaluate(
        self, expression: FactorExpression, context: FactorContext, as_of: datetime
    ) -> FactorValue:
        if expression.kind == "primitive":
            return self._primitive(expression.name or "", context, as_of)
        if expression.kind == "constant":
            return FactorValue.present(expression.value or 0.0)
        operator = expression.operator or ""
        if operator in UNARY_OPERATORS:
            if operator in {"DELAY1", "DECAY"}:
                timestamps = self._timestamps(context, as_of)
                if len(timestamps) < 2:
                    return FactorValue.missing(MissingReason.NOT_ENOUGH_LOOKBACK)
                current_time, previous_time = timestamps[-1], timestamps[-2]
                current = self._evaluate(expression.args[0], context, current_time)
                previous = self._evaluate(expression.args[0], context, previous_time)
                if current.value is None or previous.value is None:
                    return FactorValue.missing(
                        current.missing_reason
                        or previous.missing_reason
                        or MissingReason.NOT_ENOUGH_LOOKBACK
                    )
                if operator == "DELAY1":
                    return previous
                return FactorValue.present(0.5 * current.value + 0.5 * previous.value)
            value = self._evaluate(expression.args[0], context, as_of)
            if value.value is None:
                return value
            if operator == "NEG":
                return FactorValue.present(-value.value)
            if operator == "ABS":
                return FactorValue.present(abs(value.value))
            return FactorValue.present(float(value.value > 0) - float(value.value < 0))
        if operator in ROLLING_OPERATORS:
            window = expression.args[1]
            if window.kind != "constant" or window.value is None or window.value <= 0:
                return FactorValue.missing(MissingReason.INVALID)
            start = as_of - timedelta(seconds=window.value)
            values: list[float] = []
            for timestamp in self._timestamps(context, as_of):
                if timestamp < start:
                    continue
                value = self._evaluate(expression.args[0], context, timestamp)
                if value.value is None:
                    return value
                values.append(value.value)
            if not values:
                return FactorValue.missing(MissingReason.NOT_ENOUGH_LOOKBACK)
            mean = sum(values) / len(values)
            if operator == "ROLLING_MEAN":
                return FactorValue.present(mean)
            return FactorValue.present(
                math.sqrt(sum((item - mean) ** 2 for item in values) / len(values))
            )
        left = self._evaluate(expression.args[0], context, as_of)
        if left.value is None:
            return left
        right = self._evaluate(expression.args[1], context, as_of)
        if operator == "GATE":
            if left.value <= 0:
                return FactorValue.missing(MissingReason.GATE_CLOSED)
            return right
        if right.value is None:
            return right
        if operator == "ADD":
            return FactorValue.present(left.value + right.value)
        if operator == "SUB":
            return FactorValue.present(left.value - right.value)
        if operator == "MUL":
            return FactorValue.present(left.value * right.value)
        if abs(right.value) <= 1e-12:
            return FactorValue.missing(MissingReason.DIVISION_BY_ZERO)
        return FactorValue.present(left.value / right.value)


@dataclass(frozen=True, slots=True)
class EvaluationPlan:
    experiment_id: str
    dataset_id: str = DATASET_V2_ID
    holdout_state: str = DATASET_V2_HOLDOUT_STATE
    purge_embargo_seconds: int = 600
    max_candidates: int = 100

    def validate(self) -> None:
        if self.dataset_id != DATASET_V2_ID:
            raise FactorEvaluationBlocked("DATASET_V2_ID_MISMATCH")
        if self.holdout_state != DATASET_V2_HOLDOUT_STATE:
            raise FactorEvaluationBlocked("HOLDOUT_NOT_FROZEN")
        if self.purge_embargo_seconds < 600:
            raise FactorEvaluationBlocked("PURGE_EMBARGO_TOO_SMALL")
        if self.max_candidates <= 0:
            raise FactorEvaluationBlocked("INVALID_SEARCH_BUDGET")


@dataclass(frozen=True, slots=True)
class FactorRow:
    factor_context: FactorContext
    target: float
    split: str
    event_id: str
    asset: str
    day: str
    target_timestamp: datetime | None = None
    window_end: datetime | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.target):
            raise FactorEvaluationBlocked("INVALID_TARGET")
        for timestamp in (self.target_timestamp, self.window_end):
            if timestamp is not None and timestamp.tzinfo is None:
                raise FactorEvaluationBlocked("TARGET_TIMESTAMP_NOT_TIMEZONE_AWARE")


@dataclass(frozen=True, slots=True)
class FactorMetrics:
    examples: int
    coverage: float
    pearson_ic: float | None
    spearman_ic: float | None
    directional_accuracy: float | None


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean, right_mean = sum(left) / len(left), sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_scale = math.sqrt(sum((a - left_mean) ** 2 for a in left))
    right_scale = math.sqrt(sum((b - right_mean) ** 2 for b in right))
    if left_scale <= 1e-12 or right_scale <= 1e-12:
        return None
    return numerator / (left_scale * right_scale)


def _rank(values: Sequence[float]) -> tuple[float, ...]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    for rank, index in enumerate(order):
        ranks[index] = float(rank)
    return tuple(ranks)


def _metrics(values: Sequence[float], targets: Sequence[float], total: int) -> FactorMetrics:
    directional = sum(
        (value > 0) == (target > 0) for value, target in zip(values, targets, strict=True)
    )
    return FactorMetrics(
        len(values),
        len(values) / total if total else 0.0,
        _correlation(values, targets),
        _correlation(_rank(values), _rank(targets)),
        directional / len(values) if values else None,
    )


@dataclass(frozen=True, slots=True)
class FactorEvaluationResult:
    factor_id: str
    dataset_id: str
    experiment_id: str
    train: FactorMetrics
    validation: FactorMetrics
    per_day: Mapping[str, FactorMetrics]
    per_asset: Mapping[str, FactorMetrics]
    validation_exposure_count: int
    leakage_result: str = "PASS"


def evaluate_factor(
    factor: FactorSpec,
    rows: Sequence[FactorRow],
    plan: EvaluationPlan,
    vm: SafeFactorVM | None = None,
) -> FactorEvaluationResult:
    plan.validate()
    if factor.dataset_id != plan.dataset_id:
        raise FactorEvaluationBlocked("FACTOR_DATASET_ID_MISMATCH")
    if factor.required_lookback_seconds + 300 > plan.purge_embargo_seconds:
        raise FactorEvaluationBlocked("FACTOR_PURGE_EMBARGO_INSUFFICIENT")
    evaluator = vm or SafeFactorVM()
    split_values: dict[str, tuple[list[float], list[float], int]] = {
        "train": ([], [], 0),
        "validation": ([], [], 0),
    }
    grouped_day: dict[str, tuple[list[float], list[float], int]] = {}
    grouped_asset: dict[str, tuple[list[float], list[float], int]] = {}
    event_splits: dict[str, str] = {}
    for row in rows:
        if row.split not in split_values:
            raise FactorEvaluationBlocked("HOLDOUT_ACCESS_PROHIBITED")
        previous_split = event_splits.setdefault(row.event_id, row.split)
        if previous_split != row.split:
            raise FactorEvaluationBlocked("EVENT_CROSSES_SPLIT")
        if row.target_timestamp is not None:
            if row.target_timestamp <= row.factor_context.decision_timestamp:
                raise FactorEvaluationBlocked("TARGET_NOT_IN_FUTURE")
            if row.window_end is not None and row.target_timestamp > row.window_end:
                raise FactorEvaluationBlocked("TARGET_OUTSIDE_WINDOW")
        values, targets, total = split_values[row.split]
        split_values[row.split] = (values, targets, total + 1)
        result = evaluator.evaluate(factor, row.factor_context)
        if result.value is None:
            continue
        values.append(result.value)
        targets.append(row.target)
        group_values, group_targets, group_total = grouped_day.setdefault(row.day, ([], [], 0))
        group_values.append(result.value)
        group_targets.append(row.target)
        grouped_day[row.day] = (group_values, group_targets, group_total + 1)
        asset_values, asset_targets, asset_total = grouped_asset.setdefault(row.asset, ([], [], 0))
        asset_values.append(result.value)
        asset_targets.append(row.target)
        grouped_asset[row.asset] = (asset_values, asset_targets, asset_total + 1)
    train_values, train_targets, train_total = split_values["train"]
    validation_values, validation_targets, validation_total = split_values["validation"]
    per_day: dict[str, FactorMetrics] = {}
    per_asset: dict[str, FactorMetrics] = {}
    for day, (values, targets, total) in grouped_day.items():
        per_day[day] = _metrics(values, targets, total)
    for asset, (values, targets, total) in grouped_asset.items():
        per_asset[asset] = _metrics(values, targets, total)
    return FactorEvaluationResult(
        factor.factor_id,
        plan.dataset_id,
        plan.experiment_id,
        _metrics(train_values, train_targets, train_total),
        _metrics(validation_values, validation_targets, validation_total),
        per_day,
        per_asset,
        validation_total,
    )


@dataclass(slots=True)
class SearchBudget:
    max_candidates: int = 100
    evaluated: int = 0
    families: Counter[str] = field(default_factory=Counter)

    def claim(self, family: str) -> None:
        if self.evaluated >= self.max_candidates:
            raise FactorEvaluationBlocked("FACTOR_SEARCH_BUDGET_EXCEEDED")
        self.evaluated += 1
        self.families[family] += 1


@dataclass(frozen=True, slots=True)
class RedundancyDiagnostic:
    correlation: float | None
    primitive_overlap: tuple[str, ...]
    preliminary_redundant: bool


def redundancy_diagnostic(
    candidate: FactorSpec,
    candidate_values: Sequence[float],
    existing: Sequence[tuple[FactorSpec, Sequence[float]]],
    *,
    preliminary_correlation_threshold: float = 0.95,
) -> Mapping[str, RedundancyDiagnostic]:
    result: dict[str, RedundancyDiagnostic] = {}
    for accepted, values in existing:
        correlation = _correlation(candidate_values, values)
        overlap = tuple(
            sorted(set(candidate.primitive_dependencies) & set(accepted.primitive_dependencies))
        )
        result[accepted.factor_id] = RedundancyDiagnostic(
            correlation,
            overlap,
            correlation is not None and abs(correlation) >= preliminary_correlation_threshold,
        )
    return result


@dataclass(frozen=True, slots=True)
class FactorRecord:
    factor: FactorSpec
    status: str
    evaluation: FactorEvaluationResult | None = None
    redundancy: Mapping[str, RedundancyDiagnostic] = field(default_factory=dict)
    code_sha: str = ""


@dataclass(slots=True)
class FactorZoo:
    experiment_id: str
    dataset_id: str = DATASET_V2_ID
    records: list[FactorRecord] = field(default_factory=list)

    def add(self, record: FactorRecord) -> None:
        if record.factor.dataset_id != self.dataset_id:
            raise FactorEvaluationBlocked("FACTOR_DATASET_ID_MISMATCH")
        if record.status not in {
            FACTOR_STATUS_PROPOSED,
            FACTOR_STATUS_DEVELOPMENT,
            FACTOR_STATUS_REJECTED_LEAKAGE,
            FACTOR_STATUS_REJECTED_UNSTABLE,
            FACTOR_STATUS_REJECTED_REDUNDANT,
            FACTOR_STATUS_DEFERRED,
        }:
            raise FactorError("invalid factor zoo status")
        if any(item.factor.factor_id == record.factor.factor_id for item in self.records):
            raise FactorError("duplicate factor identity")
        self.records.append(record)

    def manifest(self, search_budget: SearchBudget) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "dataset_id": self.dataset_id,
            "dsl_version": FACTOR_DSL_VERSION,
            "operator_version": FACTOR_OPERATOR_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "search_budget": {
                "max_candidates": search_budget.max_candidates,
                "evaluated": search_budget.evaluated,
                "families": dict(search_budget.families),
            },
            "factors": [
                {
                    "factor_id": record.factor.factor_id,
                    "canonical_formula": record.factor.canonical_formula,
                    "primitive_dependencies": record.factor.primitive_dependencies,
                    "complexity": asdict(record.factor.complexity),
                    "required_lookback_seconds": record.factor.required_lookback_seconds,
                    "status": record.status,
                    "leakage_result": record.evaluation.leakage_result
                    if record.evaluation
                    else "NOT_EVALUATED",
                    "validation_metrics": asdict(record.evaluation.validation)
                    if record.evaluation
                    else None,
                    "code_sha": record.code_sha,
                }
                for record in self.records
            ],
        }


def demo_factor_candidates(*, experiment_id: str = "FACTOR-001-DEMO") -> tuple[FactorSpec, ...]:
    """Small deterministic demonstration set; no data search is performed."""

    definitions = (
        operation("MUL", primitive("return_momentum"), primitive("normalized_distance_to_target")),
        operation("MUL", primitive("top_depth_imbalance"), primitive("time_remaining_seconds")),
        operation(
            "SAFE_DIV",
            primitive("return_30s"),
            operation("ADD", primitive("realized_volatility_60s"), constant(1e-6)),
        ),
        operation(
            "GATE", operation("SIGN", primitive("return_15s")), primitive("return_acceleration")
        ),
        operation(
            "SAFE_DIV",
            primitive("yes_spread"),
            operation("ADD", primitive("yes_cumulative_depth"), primitive("no_cumulative_depth")),
        ),
        operation("DECAY", primitive("return_acceleration")),
    )
    return tuple(make_factor(expression, experiment_id=experiment_id) for expression in definitions)


__all__ = [
    "DATASET_V2_HOLDOUT_STATE",
    "DATASET_V2_ID",
    "FACTOR_DSL_VERSION",
    "FACTOR_OPERATOR_VERSION",
    "FACTOR_STATUS_DEVELOPMENT",
    "FACTOR_STATUS_PROPOSED",
    "ComplexityBudget",
    "FactorComplexityExceeded",
    "FactorContext",
    "FactorError",
    "FactorEvaluationBlocked",
    "FactorEvaluationResult",
    "FactorExpression",
    "FactorRecord",
    "FactorRow",
    "FactorSpec",
    "FactorValue",
    "FactorZoo",
    "MissingReason",
    "RedundancyDiagnostic",
    "SafeFactorVM",
    "SearchBudget",
    "TimedValue",
    "canonical_expression",
    "constant",
    "demo_factor_candidates",
    "evaluate_factor",
    "make_factor",
    "operation",
    "parse_expression",
    "primitive",
    "redundancy_diagnostic",
    "validate_complexity",
]
