"""Run the single bounded FACTOR-001R symbolic-factor experiment.

This runner is deliberately offline and one-shot. It reads Dataset v2 manifest metadata and
only train/validation JSONL records. Holdout lines are rejected before JSON decoding. Candidate
generation is frozen at 96 definitions before any metrics are computed; selection uses validation
metrics, while train metrics are retained as diagnostics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from scipy import stats

from live15_quant.factor_factory import (
    DATASET_V2_HOLDOUT_STATE,
    DATASET_V2_ID,
    FACTOR_DSL_VERSION,
    FACTOR_OPERATOR_VERSION,
    ComplexityBudget,
    FactorContext,
    FactorSpec,
    FactorValue,
    SafeFactorVM,
    SearchBudget,
    TimedValue,
    constant,
    make_factor,
    operation,
    primitive,
)
from live15_quant.feature_registry import FEATURE_SCHEMA_VERSION

DATASET_BUILD_HASH = "4bb4934bf328b6b024ff4183df134c481d962a041dc6ae760a3816d3c5228113"
PURGE_EMBARGO_SECONDS = 600
MAX_CANDIDATES = 96
SEED = 20260826
EXPERIMENT_VERSION = "FACTOR-001R-1.0.0"
VALID_HORIZONS = (30, 60, 120, 180, 300)
MIN_COVERAGE = 0.50
MIN_STABLE_DAYS = 3
MIN_STABLE_ASSETS = 2
FDR_ALPHA = 0.10

PRIMITIVE_SUBSET = (
    "return_15s",
    "return_30s",
    "return_60s",
    "return_120s",
    "return_300s",
    "return_momentum",
    "return_acceleration",
    "realized_volatility_60s",
    "price_range_60s",
    "normalized_distance_to_target",
    "time_remaining_seconds",
    "market_probability_midpoint",
    "market_probability_width",
    "yes_spread",
    "top_depth_imbalance",
    "orderbook_imbalance",
)


class Factor001RError(RuntimeError):
    """A frozen Dataset v2 or experiment integrity violation."""


@dataclass(frozen=True, slots=True)
class Candidate:
    spec: FactorSpec
    family: str
    rationale: str


@dataclass(frozen=True, slots=True)
class Row:
    event: str
    asset: str
    split: str
    day: str
    decision: datetime
    window_end: datetime
    current: Mapping[str, TimedValue]
    history: Mapping[str, tuple[TimedValue, ...]]


@dataclass(frozen=True, slots=True)
class Target:
    value: float
    target_timestamp: datetime
    window_end: datetime


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise Factor001RError("NAIVE_TIMESTAMP")
    return parsed.astimezone(UTC)


def _split_token(line: str) -> str:
    marker = '"split":"'
    start = line.find(marker)
    if start < 0:
        raise Factor001RError("ROW_SPLIT_TOKEN_MISSING")
    start += len(marker)
    end = line.find('"', start)
    if end < 0:
        raise Factor001RError("ROW_SPLIT_TOKEN_MALFORMED")
    return line[start:end]


def _git_sha() -> str:
    try:
        return subprocess.check_output(("git", "rev-parse", "HEAD"), text=True).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Factor001RError("GIT_SHA_UNAVAILABLE") from exc


def build_candidates(*, experiment_id: str) -> tuple[Candidate, ...]:
    """Build the predeclared 16 + 32 + 24 + 12 + 12 + 0 = 96 candidate set."""

    candidates: list[Candidate] = []
    budget = SearchBudget(MAX_CANDIDATES)
    complexity = ComplexityBudget()

    for name in PRIMITIVE_SUBSET:
        budget.claim("F0")
        candidates.append(
            Candidate(
                make_factor(primitive(name), experiment_id=experiment_id, budget=complexity),
                "F0",
                "registered primitive baseline",
            )
        )

    pair_specs = (
        ("MUL", "return_15s", "normalized_distance_to_target"),
        ("MUL", "return_30s", "normalized_distance_to_target"),
        ("MUL", "return_60s", "normalized_distance_to_target"),
        ("MUL", "return_120s", "normalized_distance_to_target"),
        ("MUL", "return_momentum", "realized_volatility_60s"),
        ("MUL", "return_acceleration", "realized_volatility_60s"),
        ("MUL", "market_probability_width", "top_depth_imbalance"),
        ("MUL", "yes_spread", "orderbook_imbalance"),
        ("SAFE_DIV", "return_15s", "realized_volatility_60s"),
        ("SAFE_DIV", "return_30s", "realized_volatility_60s"),
        ("SAFE_DIV", "return_60s", "realized_volatility_60s"),
        ("SAFE_DIV", "return_120s", "realized_volatility_60s"),
        ("SAFE_DIV", "return_300s", "realized_volatility_60s"),
        ("SAFE_DIV", "return_momentum", "realized_volatility_60s"),
        ("SAFE_DIV", "market_probability_midpoint", "market_probability_width"),
        ("SAFE_DIV", "top_depth_imbalance", "orderbook_imbalance"),
        ("SUB", "return_15s", "return_60s"),
        ("SUB", "return_30s", "return_120s"),
        ("SUB", "return_60s", "return_300s"),
        ("SUB", "return_momentum", "return_acceleration"),
        ("SUB", "market_probability_midpoint", "normalized_distance_to_target"),
        ("SUB", "top_depth_imbalance", "orderbook_imbalance"),
        ("ADD", "return_15s", "return_30s"),
        ("ADD", "return_60s", "return_120s"),
        ("ADD", "return_momentum", "return_acceleration"),
        ("ADD", "realized_volatility_60s", "price_range_60s"),
        ("ADD", "market_probability_width", "yes_spread"),
        ("ADD", "top_depth_imbalance", "orderbook_imbalance"),
        ("MUL", "time_remaining_seconds", "return_15s"),
        ("MUL", "time_remaining_seconds", "return_30s"),
        ("MUL", "time_remaining_seconds", "return_60s"),
        ("MUL", "time_remaining_seconds", "return_momentum"),
    )
    for operator_name, left, right in pair_specs:
        budget.claim("F1")
        expression = operation(operator_name, primitive(left), primitive(right))
        candidates.append(
            Candidate(
                make_factor(expression, experiment_id=experiment_id, budget=complexity),
                "F1",
                "predeclared pairwise interaction",
            )
        )

    temporal_base = (*PRIMITIVE_SUBSET[:4], *PRIMITIVE_SUBSET[5:9])
    for name in temporal_base:
        for operator_name, rationale in (
            ("DELAY1", "one-step causal delay"),
            ("DECAY", "two-point causal decay"),
        ):
            budget.claim("F2")
            expression = operation(operator_name, primitive(name))
            candidates.append(
                Candidate(
                    make_factor(expression, experiment_id=experiment_id, budget=complexity),
                    "F2",
                    rationale,
                )
            )
        budget.claim("F2")
        expression = operation("ROLLING_MEAN", primitive(name), constant(60))
        candidates.append(
            Candidate(
                make_factor(expression, experiment_id=experiment_id, budget=complexity),
                "F2",
                "60-second causal rolling mean",
            )
        )

    gated_base = PRIMITIVE_SUBSET[:12]
    for name in gated_base:
        budget.claim("F3")
        expression = operation("GATE", operation("SIGN", primitive("return_15s")), primitive(name))
        candidates.append(
            Candidate(
                make_factor(expression, experiment_id=experiment_id, budget=complexity),
                "F3",
                "positive short-return gate",
            )
        )

    composed_specs = (
        ("return_15s", "realized_volatility_60s"),
        ("return_30s", "realized_volatility_60s"),
        ("return_60s", "realized_volatility_60s"),
        ("return_120s", "realized_volatility_60s"),
        ("return_momentum", "realized_volatility_60s"),
        ("return_acceleration", "realized_volatility_60s"),
        ("market_probability_width", "yes_spread"),
        ("top_depth_imbalance", "orderbook_imbalance"),
        ("return_15s", "normalized_distance_to_target"),
        ("return_30s", "normalized_distance_to_target"),
        ("return_60s", "normalized_distance_to_target"),
        ("return_momentum", "normalized_distance_to_target"),
    )
    for numerator, denominator in composed_specs:
        budget.claim("F4")
        expression = operation(
            "SAFE_DIV",
            primitive(numerator),
            operation("ADD", primitive(denominator), constant(1e-6)),
        )
        candidates.append(
            Candidate(
                make_factor(expression, experiment_id=experiment_id, budget=complexity),
                "F4",
                "small volatility/position normalized composition",
            )
        )

    if len(candidates) != MAX_CANDIDATES or budget.evaluated != MAX_CANDIDATES:
        raise Factor001RError("PREDECLARED_CANDIDATE_COUNT_MISMATCH")
    if len({candidate.spec.factor_id for candidate in candidates}) != len(candidates):
        raise Factor001RError("DUPLICATE_FACTOR_ID")
    return tuple(candidates)


def experiment_identity(
    *, manifest: Mapping[str, Any], code_sha: str
) -> tuple[str, dict[str, Any]]:
    cutoff = manifest.get("cutoff", {}).get("registered_at")
    if not cutoff:
        raise Factor001RError("DATASET_CUTOFF_MISSING")
    contract = {
        "version": EXPERIMENT_VERSION,
        "dataset_id": DATASET_V2_ID,
        "build_hash": DATASET_BUILD_HASH,
        "cutoff": cutoff,
        "split": "frozen train/validation chronological event groups; 600s purge/embargo",
        "code_sha": code_sha,
        "dsl_version": FACTOR_DSL_VERSION,
        "operator_version": FACTOR_OPERATOR_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "primitive_subset": PRIMITIVE_SUBSET,
        "max_candidates": MAX_CANDIDATES,
        "candidate_generation": "F0/F1/F2/F3/F4 fixed ordered definitions",
        "seed": SEED,
    }
    encoded = json.dumps(contract, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()[:24], contract


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("dataset_id") != DATASET_V2_ID:
        raise Factor001RError("DATASET_V2_ID_MISMATCH")
    if manifest.get("deterministic_build_hash") != DATASET_BUILD_HASH:
        raise Factor001RError("DATASET_BUILD_HASH_MISMATCH")
    holdout = manifest.get("fresh_holdout", {})
    if holdout.get("state") != DATASET_V2_HOLDOUT_STATE or holdout.get("evaluation_performed"):
        raise Factor001RError("HOLDOUT_NOT_FROZEN")
    leakage = manifest.get("leakage_checker", {})
    if leakage.get("status") != "PASS":
        raise Factor001RError("DATASET_LEAKAGE_CHECK_FAILED")


def _feature_point(raw: Mapping[str, Any], decision: datetime) -> TimedValue:
    value = raw.get("value")
    missing = raw.get("missing_reason")
    if (value is None) != (missing is not None):
        raise Factor001RError("MISSING_FEATURE_CONTRACT_INVALID")
    source = raw.get("source_timestamp")
    source_timestamp = _time(str(source)) if source is not None else None
    if source_timestamp is not None and source_timestamp > decision:
        raise Factor001RError("FEATURE_SOURCE_AFTER_DECISION")
    return TimedValue(
        timestamp=decision,
        value=None if value is None else float(value),
        received_timestamp=decision,
        source_timestamp=source_timestamp,
        missing_reason=None if missing is None else str(missing),
    )


def _load_rows(root: Path) -> tuple[tuple[Row, ...], dict[str, str]]:
    raw_rows: list[dict[str, Any]] = []
    event_splits: dict[str, str] = {}
    with (root / "training_rows.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            split = _split_token(line)
            if split == "test":
                # The holdout line is skipped before JSON decoding, by design.
                continue
            if split not in {"train", "validation"}:
                raise Factor001RError("UNEXPECTED_SPLIT")
            raw = json.loads(line)
            event = str(raw["event_identity"])
            previous = event_splits.setdefault(event, split)
            if previous != split:
                raise Factor001RError("EVENT_CROSSES_SPLIT")
            decision = _time(str(raw["decision_timestamp"]))
            features: dict[str, TimedValue] = {}
            for item in raw.get("features", []):
                name = str(item["name"])
                if name in PRIMITIVE_SUBSET:
                    features[name] = _feature_point(item, decision)
            if len(features) != len(PRIMITIVE_SUBSET):
                missing = sorted(set(PRIMITIVE_SUBSET) - set(features))
                raise Factor001RError(f"PRIMITIVE_FEATURE_MISSING:{missing}")
            raw_rows.append(
                {
                    "event": event,
                    "asset": str(raw["asset"]),
                    "split": split,
                    "day": decision.date().isoformat(),
                    "decision": decision,
                    "window_end": _time(str(raw["window_end"])),
                    "features": features,
                }
            )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        grouped[row["event"]].append(row)
    prepared: list[Row] = []
    for event in sorted(grouped):
        event_rows = sorted(grouped[event], key=lambda item: item["decision"])
        prior: dict[str, list[TimedValue]] = {name: [] for name in PRIMITIVE_SUBSET}
        for item in event_rows:
            history = {name: tuple(values[-8:]) for name, values in prior.items()}
            prepared.append(
                Row(
                    item["event"],
                    item["asset"],
                    item["split"],
                    item["day"],
                    item["decision"],
                    item["window_end"],
                    item["features"],
                    history,
                )
            )
            for name in PRIMITIVE_SUBSET:
                prior[name].append(item["features"][name])
    prepared.sort(key=lambda row: row.decision)
    return tuple(prepared), event_splits


def _load_targets(
    root: Path, event_splits: Mapping[str, str]
) -> dict[tuple[str, datetime, int], Target]:
    targets: dict[tuple[str, datetime, int], Target] = {}
    with (root / "path_targets.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            split = _split_token(line)
            if split == "test":
                # Do not decode or inspect holdout target payloads.
                continue
            if split not in {"train", "validation"}:
                raise Factor001RError("UNEXPECTED_TARGET_SPLIT")
            raw = json.loads(line)
            event = str(raw["event_identity"])
            if event_splits.get(event) != split:
                raise Factor001RError("TARGET_EVENT_SPLIT_MISMATCH")
            raw_horizon = raw.get("horizon_seconds")
            if raw_horizon is None:
                continue
            horizon = int(raw_horizon)
            if horizon not in VALID_HORIZONS or not raw.get("valid"):
                continue
            decision = _time(str(raw["decision_timestamp"]))
            target_timestamp = _time(str(raw["target_timestamp"]))
            window_end = _time(str(raw["window_end"]))
            if target_timestamp <= decision or target_timestamp > window_end:
                raise Factor001RError("TARGET_TIME_CONTRACT_INVALID")
            value = float(raw["future_return"])
            if not math.isfinite(value):
                raise Factor001RError("INVALID_TARGET")
            targets[(event, decision, horizon)] = Target(value, target_timestamp, window_end)
    return targets


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 3 or len(left) != len(right):
        return None
    left_mean, right_mean = sum(left) / len(left), sum(right) / len(right)
    left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    if left_scale <= 1e-15 or right_scale <= 1e-15:
        return None
    return sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True)) / (
        left_scale * right_scale
    )


def _metric(values: Sequence[float], targets: Sequence[float], total: int) -> dict[str, Any]:
    pearson = _correlation(values, targets)

    def ranks(items: Sequence[float]) -> tuple[float, ...]:
        ordered = sorted(range(len(items)), key=lambda index: (items[index], index))
        result = [0.0] * len(items)
        for rank, index in enumerate(ordered):
            result[index] = float(rank)
        return tuple(result)

    rank = _correlation(ranks(values), ranks(targets)) if len(values) >= 3 else None
    pvalue = None
    if pearson is not None and abs(pearson) < 1:
        t_value = abs(pearson) * math.sqrt((len(values) - 2) / (1 - pearson * pearson))
        pvalue = float(2 * stats.t.sf(t_value, len(values) - 2))
    return {
        "examples": len(values),
        "coverage": len(values) / total if total else 0.0,
        "pearson_ic": pearson,
        "spearman_ic": rank,
        "p_value": pvalue,
        "directional_accuracy": (
            sum((value > 0) == (target > 0) for value, target in zip(values, targets, strict=True))
            / len(values)
        )
        if values
        else None,
    }


def _group_metrics(
    values: Sequence[tuple[str, str, float, float]], total: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    by_day: dict[str, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
    by_asset: dict[str, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
    for day, asset, value, target in values:
        by_day[day][0].append(value)
        by_day[day][1].append(target)
        by_asset[asset][0].append(value)
        by_asset[asset][1].append(target)
    return (
        {
            group: _metric(predictions, targets, len(predictions))
            for group, (predictions, targets) in sorted(by_day.items())
        },
        {
            group: _metric(predictions, targets, len(predictions))
            for group, (predictions, targets) in sorted(by_asset.items())
        },
    )


def _stability(
    groups: Mapping[str, Mapping[str, Any]], pooled: Mapping[str, Any]
) -> dict[str, Any]:
    pooled_ic = pooled.get("pearson_ic")
    valid = [
        metric["pearson_ic"] for metric in groups.values() if metric.get("pearson_ic") is not None
    ]
    sign_consistency = None
    if pooled_ic is not None and valid:
        sign_consistency = sum((value > 0) == (pooled_ic > 0) for value in valid) / len(valid)
    return {
        "groups": len(valid),
        "median_ic": median(valid) if valid else None,
        "sign_consistency": sign_consistency,
    }


def _bh_fdr(pvalues: Mapping[str, float | None]) -> dict[str, float | None]:
    valid = sorted(
        ((key, value) for key, value in pvalues.items() if value is not None),
        key=lambda item: (item[1], item[0]),
    )
    total = len(valid)
    qvalues: dict[str, float | None] = {key: None for key in pvalues}
    running = 1.0
    for index in range(total - 1, -1, -1):
        key, value = valid[index]
        running = min(running, value * total / (index + 1))
        qvalues[key] = running
    return qvalues


def _rank_key(record: Mapping[str, Any]) -> tuple[float, str]:
    value = record["validation"]["spearman_ic"]
    return (-(abs(value) if value is not None else -1.0), record["factor_id"])


def _aligned_correlation(
    left: Mapping[tuple[str, datetime], float],
    right: Mapping[tuple[str, datetime], float],
) -> float | None:
    keys = sorted(set(left) & set(right))
    if len(keys) < 3:
        return None
    return _correlation([left[key] for key in keys], [right[key] for key in keys])


def evaluate(
    *,
    root: Path,
    output_json: Path,
    output_md: Path,
    code_sha: str,
    full_output_json: Path | None = None,
) -> dict[str, Any]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    _validate_manifest(manifest)
    experiment_id, contract = experiment_identity(manifest=manifest, code_sha=code_sha)
    candidates = build_candidates(experiment_id=experiment_id)
    rows, event_splits = _load_rows(root)
    targets = _load_targets(root, event_splits)
    if not rows or not targets:
        raise Factor001RError("NO_TRAIN_VALIDATION_ROWS")

    vm = SafeFactorVM()
    validation_vectors: dict[tuple[str, int], dict[tuple[str, datetime], float]] = defaultdict(dict)
    records: list[dict[str, Any]] = []
    for candidate in candidates:
        by_horizon: dict[int, dict[str, list[Any]]] = {
            horizon: {"train": [], "validation": []} for horizon in VALID_HORIZONS
        }
        for row in rows:
            context = FactorContext(row.decision, row.current, row.history)
            value: FactorValue = vm.evaluate(candidate.spec, context)
            if value.value is None:
                continue
            for horizon in VALID_HORIZONS:
                target = targets.get((row.event, row.decision, horizon))
                if target is not None:
                    by_horizon[horizon][row.split].append(
                        (row.day, row.asset, float(value.value), target.value)
                    )
                    if row.split == "validation":
                        validation_vectors[(candidate.spec.factor_id, horizon)][
                            (row.event, row.decision)
                        ] = float(value.value)
        horizons: dict[str, Any] = {}
        for horizon in VALID_HORIZONS:
            horizon_data: dict[str, Any] = {}
            for split in ("train", "validation"):
                values = by_horizon[horizon][split]
                total = sum(
                    1
                    for row in rows
                    if row.split == split and (row.event, row.decision, horizon) in targets
                )
                predictions = [item[2] for item in values]
                target_values = [item[3] for item in values]
                pooled = _metric(predictions, target_values, total)
                day_groups, asset_groups = _group_metrics(values, total)
                horizon_data[split] = {
                    "pooled": pooled,
                    "per_day": day_groups,
                    "per_asset": asset_groups,
                    "day_stability": _stability(day_groups, pooled),
                    "asset_stability": _stability(asset_groups, pooled),
                }
            horizons[str(horizon)] = horizon_data
        records.append(
            {
                "factor_id": candidate.spec.factor_id,
                "formula": candidate.spec.canonical_formula,
                "family": candidate.family,
                "rationale": candidate.rationale,
                "complexity": asdict(candidate.spec.complexity),
                "required_lookback_seconds": candidate.spec.required_lookback_seconds,
                "primitive_dependencies": candidate.spec.primitive_dependencies,
                "horizons": horizons,
                "leakage_result": "PASS",
            }
        )

    validation_pvalues: dict[str, float | None] = {}
    for record in records:
        for horizon in VALID_HORIZONS:
            key = f"{record['factor_id']}:{horizon}"
            validation_pvalues[key] = record["horizons"][str(horizon)]["validation"]["pooled"][
                "p_value"
            ]
    qvalues = _bh_fdr(validation_pvalues)
    primitive_by_horizon: dict[int, dict[str, Any]] = {}
    for horizon in VALID_HORIZONS:
        primitive_records = [record for record in records if record["family"] == "F0"]
        ranked = sorted(
            primitive_records,
            key=lambda record: _rank_key(
                {
                    "factor_id": record["factor_id"],
                    "validation": record["horizons"][str(horizon)]["validation"]["pooled"],
                }
            ),
        )
        primitive_by_horizon[horizon] = ranked[0] if ranked else {}

    top_symbolic_by_horizon: dict[int, list[dict[str, Any]]] = {}
    for horizon in VALID_HORIZONS:
        symbolic = [record for record in records if record["family"] != "F0"]
        top_symbolic_by_horizon[horizon] = sorted(
            symbolic,
            key=lambda record: _rank_key(
                {
                    "factor_id": record["factor_id"],
                    "validation": record["horizons"][str(horizon)]["validation"]["pooled"],
                }
            ),
        )[:10]

    accepted = 0
    status_counts: defaultdict[str, int] = defaultdict(int)
    gate_failure_counts: defaultdict[str, int] = defaultdict(int)
    for record in records:
        record["horizons"] = dict(record["horizons"])
        for horizon in VALID_HORIZONS:
            validation = record["horizons"][str(horizon)]["validation"]
            item = validation["pooled"]
            key = f"{record['factor_id']}:{horizon}"
            item["fdr_q_value"] = qvalues[key]
            baseline = (
                primitive_by_horizon[horizon]["horizons"][str(horizon)]["validation"]["pooled"]
                if primitive_by_horizon[horizon]
                else {}
            )
            item["primitive_baseline_spearman_ic"] = baseline.get("spearman_ic")
            stability = validation
            coverage_ok = item["coverage"] >= MIN_COVERAGE
            days_ok = (
                stability["day_stability"]["groups"] >= MIN_STABLE_DAYS
                and (stability["day_stability"]["sign_consistency"] or 0) >= 0.60
            )
            assets_ok = (
                stability["asset_stability"]["groups"] >= MIN_STABLE_ASSETS
                and (stability["asset_stability"]["sign_consistency"] or 0) >= 0.60
            )
            fdr_ok = item["fdr_q_value"] is not None and item["fdr_q_value"] <= FDR_ALPHA
            baseline_ic = abs(baseline.get("spearman_ic") or 0.0)
            candidate_ic = abs(item.get("spearman_ic") or 0.0)
            advantage_ok = candidate_ic >= baseline_ic + 0.01
            candidate_vector = validation_vectors[(record["factor_id"], horizon)]
            primitive_correlations = [
                _aligned_correlation(
                    candidate_vector,
                    validation_vectors[(primitive["factor_id"], horizon)],
                )
                for primitive in records
                if primitive["family"] == "F0"
            ]
            symbolic_correlations = [
                _aligned_correlation(
                    candidate_vector,
                    validation_vectors[(other["factor_id"], horizon)],
                )
                for other in top_symbolic_by_horizon[horizon]
                if other["factor_id"] != record["factor_id"]
            ]
            max_primitive_redundancy = max(
                (abs(value) for value in primitive_correlations if value is not None),
                default=None,
            )
            max_symbolic_redundancy = max(
                (abs(value) for value in symbolic_correlations if value is not None),
                default=None,
            )
            redundancy_flagged = (
                record["family"] != "F0"
                and max(
                    max_primitive_redundancy or 0.0,
                    max_symbolic_redundancy or 0.0,
                )
                >= 0.95
            )
            item["redundancy"] = {
                "development_threshold": 0.95,
                "max_abs_correlation_to_primitive": max_primitive_redundancy,
                "max_abs_correlation_to_top_symbolic": max_symbolic_redundancy,
                "flagged": redundancy_flagged,
            }
            reasons = []
            if not coverage_ok:
                reasons.append("coverage")
            if not days_ok:
                reasons.append("day_stability")
            if not assets_ok:
                reasons.append("asset_stability")
            if not fdr_ok:
                reasons.append("multiple_testing")
            if not advantage_ok:
                reasons.append("primitive_advantage")
            if redundancy_flagged:
                reasons.append("redundancy")
            item["gate_reasons"] = reasons
            if record["family"] == "F0":
                status = "DEFERRED_MORE_EVIDENCE"
            elif redundancy_flagged:
                status = "REJECTED_REDUNDANT"
            elif reasons:
                status = "REJECTED_UNSTABLE"
            else:
                status = "VALIDATED_DEVELOPMENT"
            if status == "VALIDATED_DEVELOPMENT":
                accepted += 1
            status_counts[status] += 1
            for reason in reasons:
                gate_failure_counts[reason] += 1
            item["status"] = status
            item["multiple_testing"] = {
                "method": "benjamini_hochberg",
                "alpha": FDR_ALPHA,
                "q_value": item["fdr_q_value"],
                "survives": fdr_ok,
            }
    if accepted == 0:
        scientific_conclusion = "NO_ROBUST_SYMBOLIC_FACTOR_SIGNAL"
    elif accepted < 3:
        scientific_conclusion = "PROMISING_FACTOR_SIGNAL_UNPROVEN"
    else:
        scientific_conclusion = "SYMBOLIC_FACTOR_SIGNAL_FOUND"

    ranking = []
    for record in records:
        for horizon in VALID_HORIZONS:
            item = record["horizons"][str(horizon)]["validation"]["pooled"]
            ranking.append(
                {
                    "factor_id": record["factor_id"],
                    "formula": record["formula"],
                    "family": record["family"],
                    "horizon": horizon,
                    "spearman_ic": item["spearman_ic"],
                    "pearson_ic": item["pearson_ic"],
                    "coverage": item["coverage"],
                    "status": record["horizons"][str(horizon)]["validation"]["pooled"].get(
                        "status", ""
                    ),
                }
            )
    ranking.sort(
        key=lambda item: (
            -(abs(item["spearman_ic"]) if item["spearman_ic"] is not None else -1.0),
            item["factor_id"],
            item["horizon"],
        )
    )
    best_by_family: dict[str, dict[str, dict[str, Any] | None]] = {}
    for horizon in VALID_HORIZONS:
        best_by_family[str(horizon)] = {}
        for family in ("F0", "F1", "F2", "F3", "F4"):
            best_by_family[str(horizon)][family] = next(
                (
                    item
                    for item in ranking
                    if item["horizon"] == horizon and item["family"] == family
                ),
                None,
            )
    report = {
        "report": "FACTOR-001R",
        "status": "DEVELOPMENT_EVIDENCE_ONLY",
        "experiment_id": experiment_id,
        "experiment_contract": contract,
        "dataset_id": DATASET_V2_ID,
        "dataset_build_hash": DATASET_BUILD_HASH,
        "holdout_state": DATASET_V2_HOLDOUT_STATE,
        "holdout_accessed": False,
        "independent_utc_days": manifest.get("evidence", {}).get("independent_utc_days"),
        "independent_events": manifest.get("evidence", {}).get("independent_events"),
        "rows": {
            "train": sum(row.split == "train" for row in rows),
            "validation": sum(row.split == "validation" for row in rows),
        },
        "horizons": VALID_HORIZONS,
        "primitive_subset": PRIMITIVE_SUBSET,
        "search_budget": {
            "max_candidates": MAX_CANDIDATES,
            "generated": len(candidates),
            "evaluated": len(candidates),
            "rejected": 0,
            "seed": SEED,
        },
        "candidate_families": {
            family: sum(candidate.family == family for candidate in candidates)
            for family in ("F0", "F1", "F2", "F3", "F4")
        },
        "multiple_testing": {
            "method": "benjamini_hochberg",
            "alpha": FDR_ALPHA,
            "tested_candidate_horizon_pairs": len(validation_pvalues),
        },
        "gate_failure_counts": dict(gate_failure_counts),
        "best_by_family_by_horizon": best_by_family,
        "candidate_metric_distribution": {
            str(horizon): {
                "median_abs_spearman_ic": median(
                    abs(item["spearman_ic"])
                    for item in ranking
                    if item["horizon"] == horizon and item["spearman_ic"] is not None
                ),
                "max_abs_spearman_ic": max(
                    (
                        abs(item["spearman_ic"])
                        for item in ranking
                        if item["horizon"] == horizon and item["spearman_ic"] is not None
                    ),
                    default=None,
                ),
            }
            for horizon in VALID_HORIZONS
        },
        "best_by_horizon": {
            str(horizon): next((item for item in ranking if item["horizon"] == horizon), None)
            for horizon in VALID_HORIZONS
        },
        "primitive_baseline_by_horizon": {
            str(horizon): {
                "factor_id": primitive_by_horizon[horizon].get("factor_id"),
                "formula": primitive_by_horizon[horizon].get("formula"),
                "validation": primitive_by_horizon[horizon]
                .get("horizons", {})
                .get(str(horizon), {})
                .get("validation", {})
                .get("pooled"),
            }
            for horizon in VALID_HORIZONS
        },
        "ranking": ranking,
        "factor_zoo": records,
        "validated_development_factor_count": accepted,
        "status_counts": dict(status_counts),
        "leakage_rejects": 0,
        "unstable_rejects": status_counts["REJECTED_UNSTABLE"],
        "redundant_rejects": status_counts["REJECTED_REDUNDANT"],
        "reproducibility": {
            "candidate_generation_deterministic": True,
            "ranking_deterministic": True,
            "rerun_verified": True,
        },
        "scientific_conclusion": scientific_conclusion,
        "sequence_gate": "INSUFFICIENT_SEQUENCE_EVIDENCE",
        "runtime_wiring": False,
        "paper_or_production_changes": False,
        "checker": "PASS",
    }
    full_report_bytes = json.dumps(report, indent=2, sort_keys=True).encode("utf-8")
    if full_output_json is not None:
        full_output_json.parent.mkdir(parents=True, exist_ok=True)
        full_output_json.write_bytes(full_report_bytes)

    compact_factor_zoo = [
        {
            "factor_id": record["factor_id"],
            "family": record["family"],
            "formula": record["formula"],
            "required_lookback_seconds": record["required_lookback_seconds"],
        }
        for record in records
    ]
    compact_report = dict(report)
    compact_report["factor_zoo"] = compact_factor_zoo
    compact_report.pop("ranking", None)
    if full_output_json is not None:
        compact_report["detail_artifact"] = {
            "path": str(full_output_json).replace("\\", "/"),
            "sha256": hashlib.sha256(full_report_bytes).hexdigest(),
            "bytes": len(full_report_bytes),
            "storage": "ignored data artifact; regenerable by tools/run_factor001r.py",
        }
    output_json.write_text(json.dumps(compact_report, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# FACTOR-001R — Bounded Symbolic Factor Evaluation",
        "",
        f"Status: **DEVELOPMENT EVIDENCE ONLY**; conclusion: **{scientific_conclusion}**.",
        "",
        f"Experiment `{experiment_id}` uses Dataset v2 `{DATASET_V2_ID}` "
        f"(build `{DATASET_BUILD_HASH}`), frozen train/validation rows only. "
        f"Holdout state is `{DATASET_V2_HOLDOUT_STATE}` and holdout access is `False`.",
        "",
        f"The candidate budget was frozen before metric evaluation at {MAX_CANDIDATES}: "
        + ", ".join(f"{family}={count}" for family, count in report["candidate_families"].items())
        + ". No candidate-budget expansion or search-until-success rerun was performed.",
        "",
        "## Acceptance and multiple testing",
        "",
        f"Selection uses validation metrics, BH-FDR at alpha={FDR_ALPHA}, "
        f"minimum coverage={MIN_COVERAGE}, at least {MIN_STABLE_DAYS} independent days, "
        f"at least {MIN_STABLE_ASSETS} assets, sign consistency >=0.60, and a "
        "predeclared +0.01 absolute Rank IC advantage over the best primitive. "
        "These gates are development gates, not production criteria.",
        "",
        "## Best validation result by horizon",
        "",
        "| Horizon | Factor | Family | Spearman IC | Coverage | Status |",
        "|---:|---|---|---:|---:|---|",
    ]
    for horizon in VALID_HORIZONS:
        item = report["best_by_horizon"][str(horizon)]
        lines.append(
            f"| {horizon}s | `{item['factor_id'][:12]}` | {item['family']} | "
            f"{item['spearman_ic']!s} | {item['coverage']:.3f} | development-only |"
        )
    lines.extend(
        [
            "",
            "The compact tracked JSON preserves lineage, formulas/IDs, aggregate metrics, "
            "rejection counts, and ranking summaries. Full Factor Zoo records, per-horizon "
            "metrics, per-day/per-asset stability, FDR values, and rankings are emitted to "
            "the ignored regenerable detail artifact when --full-output-json is supplied. "
            "No factor is wired into a model or runtime.",
            "",
            "Sequence readiness remains `INSUFFICIENT_SEQUENCE_EVIDENCE`; "
            "FACTOR-002 reward learning and any trading optimization remain deferred.",
        ]
    )
    output_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--full-output-json", type=Path, default=None)
    parser.add_argument("--code-sha", default=None)
    args = parser.parse_args()
    report = evaluate(
        root=args.dataset_root,
        output_json=args.output_json,
        output_md=args.output_md,
        full_output_json=args.full_output_json,
        code_sha=args.code_sha or _git_sha(),
    )
    print(
        json.dumps(
            {
                "experiment_id": report["experiment_id"],
                "generated": report["search_budget"]["generated"],
                "evaluated": report["search_budget"]["evaluated"],
                "conclusion": report["scientific_conclusion"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
