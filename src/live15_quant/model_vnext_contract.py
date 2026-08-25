"""Machine-enforceable Model vNext data, target, and leakage contracts.

MVN-001 freezes boundaries only.  It does not train a model, read the recorder, or
publish an artifact.  Later builders can use these small validators at their public
boundaries instead of duplicating timestamp and split policy in each model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

MVN001_VERSION = "1.0.0"
DATASET_V1_ID = "live15-dataset-v1-f81d7d1feebcbbaecff9"
DATASET_V1_BUILD_HASH = "f81d7d1feebcbbaecff93086c2e1a577aeb72cc98b7bfabd22d826e05a4cce95"
DATASET_V1_FINAL_TEST_STATE = "REVEALED_FINAL"

PATH_HORIZONS_SECONDS = (5, 15, 30, 60, 120, 180, 300)
TERMINAL_WINDOW_END_HORIZON = "window_end"
TARGET_LOOKUP_TOLERANCE_SECONDS = 2
PROHIBITED_SETTLEMENT_FIELD_TOKENS = (
    "label",
    "settlement",
    "final_result",
    "outcome",
    "resolved",
)


class ContractSide(StrEnum):
    YES = "yes"
    NO = "no"


class LeakageRule(StrEnum):
    LOOKAHEAD = "look-ahead"
    LABEL = "label"
    BACKFILL = "backfill"
    JOIN = "join"
    ROLLING_WINDOW = "rolling-window"
    EVENT_SPLIT = "event-split"
    NORMALIZATION = "normalization"
    MODEL_SELECTION = "model-selection"
    CALIBRATION = "calibration"
    HYPERPARAMETER = "hyperparameter"
    CROSS_ASSET = "cross-asset-temporal"
    ARCHIVE_REPLAY = "archive-replay"


class LeakageError(ValueError):
    """A machine-checkable anti-leakage contract failed."""


class TargetUnavailableError(ValueError):
    """A future path target cannot be proven available under the target contract."""


class FinalTestGuardError(ValueError):
    """A vNext operation attempted to consume the revealed Dataset v1 final test."""


def _require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DecisionTimeContract:
    """The complete information boundary for one model example."""

    event_id: str
    ticker: str
    window_start: datetime
    window_end: datetime
    decision_timestamp: datetime
    target_level: Decimal
    side: ContractSide
    lookback_seconds: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        start = _require_aware(self.window_start, "window_start")
        end = _require_aware(self.window_end, "window_end")
        decision = _require_aware(self.decision_timestamp, "decision_timestamp")
        if not self.event_id or not self.ticker:
            raise ValueError("event_id and ticker are required")
        if not start <= decision < end:
            raise ValueError("decision must be inside its event window")
        if not self.target_level.is_finite() or self.target_level <= 0:
            raise ValueError("target_level must be a positive finite Decimal")
        if any(value <= 0 for value in self.lookback_seconds):
            raise ValueError("lookback seconds must be positive")
        if len(set(self.lookback_seconds)) != len(self.lookback_seconds):
            raise ValueError("lookback seconds must be unique")

    @property
    def time_remaining_seconds(self) -> Decimal:
        return Decimal(str((self.window_end - self.decision_timestamp).total_seconds()))

    @property
    def max_lookback_seconds(self) -> int:
        return max(self.lookback_seconds, default=0)

    def validate_feature_observation(self, observation: ObservationProvenance) -> None:
        observation.validate_as_of(self.decision_timestamp)


@dataclass(frozen=True, slots=True)
class ObservationProvenance:
    """Timestamps and provenance required before an observation can be a feature."""

    name: str
    observation_timestamp: datetime
    received_timestamp: datetime
    source_timestamp: datetime | None = None
    backfilled: bool = False
    synthetic: bool = False

    def validate_as_of(self, decision_timestamp: datetime) -> None:
        decision = _require_aware(decision_timestamp, "decision_timestamp")
        _require_aware(self.observation_timestamp, "observation_timestamp")
        received = _require_aware(self.received_timestamp, "received_timestamp")
        if self.source_timestamp is not None:
            source = _require_aware(self.source_timestamp, "source_timestamp")
            if source > decision:
                raise LeakageError(
                    f"{LeakageRule.LOOKAHEAD.value}: {self.name} source_timestamp is after decision"
                )
        if received > decision:
            raise LeakageError(
                f"{LeakageRule.LOOKAHEAD.value}: {self.name} received_timestamp is after decision"
            )
        if self.backfilled:
            raise LeakageError(
                f"{LeakageRule.BACKFILL.value}: {self.name} is a later-recovered observation"
            )
        if self.synthetic:
            raise LeakageError(f"{LeakageRule.ROLLING_WINDOW.value}: {self.name} is synthetic")


@dataclass(frozen=True, slots=True)
class PathTargetSpec:
    """One exact future path lookup; no forward-fill or post-window substitution."""

    horizon_seconds: int | str
    tolerance_seconds: int = TARGET_LOOKUP_TOLERANCE_SECONDS

    def __post_init__(self) -> None:
        if self.horizon_seconds != TERMINAL_WINDOW_END_HORIZON and (
            not isinstance(self.horizon_seconds, int)
            or self.horizon_seconds not in PATH_HORIZONS_SECONDS
        ):
            raise ValueError("unsupported Model vNext path horizon")
        if self.tolerance_seconds < 0:
            raise ValueError("target tolerance must be non-negative")


@dataclass(frozen=True, slots=True)
class PathObservation:
    timestamp: datetime
    value: Decimal

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "target observation timestamp")
        if not self.value.is_finite() or self.value <= 0:
            raise ValueError("path observation value must be positive and finite")


@dataclass(frozen=True, slots=True)
class PathTarget:
    spec: PathTargetSpec
    decision_timestamp: datetime
    target_timestamp: datetime
    base_value: Decimal
    future_value: Decimal

    @property
    def return_value(self) -> Decimal:
        return self.future_value / self.base_value - Decimal(1)


def select_path_observation(
    contract: DecisionTimeContract,
    spec: PathTargetSpec,
    observations: Sequence[PathObservation],
) -> PathObservation:
    """Select the nearest explicitly observed target within the declared tolerance."""

    expected = (
        contract.window_end
        if spec.horizon_seconds == TERMINAL_WINDOW_END_HORIZON
        else contract.decision_timestamp + timedelta(seconds=spec.horizon_seconds)
    )
    is_terminal = spec.horizon_seconds == TERMINAL_WINDOW_END_HORIZON
    if expected > contract.window_end or (expected == contract.window_end and not is_terminal):
        raise TargetUnavailableError("path target reaches or crosses the event window end")
    candidates = tuple(
        item
        for item in observations
        if contract.decision_timestamp < item.timestamp <= contract.window_end
        and abs((item.timestamp - expected).total_seconds()) <= spec.tolerance_seconds
    )
    if not candidates:
        raise TargetUnavailableError("no observed target exists within the declared tolerance")
    return min(
        candidates,
        key=lambda item: (abs((item.timestamp - expected).total_seconds()), item.timestamp),
    )


def build_path_target(
    contract: DecisionTimeContract,
    spec: PathTargetSpec,
    *,
    base_value: Decimal,
    observations: Sequence[PathObservation],
) -> PathTarget:
    """Build a future movement target from an exact observed value."""

    if not base_value.is_finite() or base_value <= 0:
        raise TargetUnavailableError("path target base is unavailable or non-positive")
    future = select_path_observation(contract, spec, observations)
    return PathTarget(
        spec,
        contract.decision_timestamp,
        future.timestamp,
        base_value,
        future.value,
    )


@dataclass(frozen=True, slots=True)
class TerminalLabel:
    """The only terminal label admitted to a training row."""

    ticker: str
    result: str
    finalized: bool
    source: str
    settlement_timestamp: datetime

    def validate(self, contract: DecisionTimeContract) -> None:
        timestamp = _require_aware(self.settlement_timestamp, "settlement_timestamp")
        if self.ticker != contract.ticker:
            raise LeakageError(f"{LeakageRule.LABEL.value}: terminal ticker mismatch")
        if self.result not in {"yes", "no"} or not self.finalized or self.source != "kalshi":
            raise LeakageError(
                f"{LeakageRule.LABEL.value}: only finalized Kalshi yes/no truth is admissible"
            )
        if timestamp < contract.window_end:
            raise LeakageError(
                f"{LeakageRule.LABEL.value}: settlement truth is not terminal at window end"
            )


def assert_feature_names_clean(feature_names: Iterable[str]) -> None:
    """Reject settlement-derived fields at the feature boundary."""

    for name in feature_names:
        normalized = name.casefold()
        if any(token in normalized for token in PROHIBITED_SETTLEMENT_FIELD_TOKENS):
            raise LeakageError(
                f"{LeakageRule.LABEL.value}: prohibited settlement-derived feature {name!r}"
            )


def validate_event_group_splits(partitions: Mapping[str, Iterable[Any]]) -> None:
    """Ensure whole event/ticker groups are isolated across every split."""

    seen: dict[str, str] = {}
    for partition, rows in partitions.items():
        for row in rows:
            group = getattr(row, "event_id", None) or getattr(row, "ticker", None)
            if not isinstance(group, str) or not group:
                raise LeakageError("event split rows require a non-empty event_id or ticker")
            prior = seen.setdefault(group, partition)
            if prior != partition:
                raise LeakageError(
                    f"{LeakageRule.EVENT_SPLIT.value}: event group {group!r} crosses "
                    f"{prior}/{partition}"
                )


def required_purge_embargo_seconds(*, max_lookback_seconds: int, max_horizon_seconds: int) -> int:
    """Derive the boundary guard from the largest feature/target temporal span."""

    if min(max_lookback_seconds, max_horizon_seconds) < 0:
        raise ValueError("lookback and horizon must be non-negative")
    return max_lookback_seconds + max_horizon_seconds


def assert_train_only_normalization(fit_partition: str) -> None:
    if fit_partition != "train":
        raise LeakageError(
            f"{LeakageRule.NORMALIZATION.value}: normalization must be fitted on train only"
        )


def assert_final_test_not_consumed(
    dataset_id: str,
    *,
    purpose: str,
    rows_consumed: bool,
) -> None:
    """Allow lineage references, but reject revealed-final-test consumption or tuning."""

    if dataset_id == DATASET_V1_ID and rows_consumed:
        raise FinalTestGuardError(
            f"{LeakageRule.MODEL_SELECTION.value}: Dataset v1 revealed final test cannot be "
            f"used for {purpose}"
        )


@dataclass(frozen=True, slots=True)
class LeakageChecker:
    """Independent review profile for future Dataset/Model vNext tasks."""

    profile: str = "MVN-001"

    def check_features(
        self, decision_timestamp: datetime, observations: Sequence[ObservationProvenance]
    ) -> None:
        for observation in observations:
            observation.validate_as_of(decision_timestamp)

    def check_splits(self, partitions: Mapping[str, Iterable[Any]]) -> None:
        validate_event_group_splits(partitions)

    def check_normalization(self, fit_partition: str) -> None:
        assert_train_only_normalization(fit_partition)

    def check_final_test(self, dataset_id: str, *, purpose: str, rows_consumed: bool) -> None:
        assert_final_test_not_consumed(dataset_id, purpose=purpose, rows_consumed=rows_consumed)

    def check_feature_names(self, feature_names: Iterable[str]) -> None:
        assert_feature_names_clean(feature_names)


def contract_manifest() -> dict[str, object]:
    """Return the stable, human/machine-readable MVN-001 contract identity."""

    return {
        "contract": "MVN-001",
        "version": MVN001_VERSION,
        "decision_time": {
            "feature_rule": "source_timestamp and received_timestamp <= decision_timestamp",
            "missing_policy": (
                "reject; no forward-fill, interpolation, synthetic future, or backfill"
            ),
        },
        "path_targets": {
            "horizons_seconds": list(PATH_HORIZONS_SECONDS),
            "terminal_horizon": TERMINAL_WINDOW_END_HORIZON,
            "formula": "future_value / decision_value - 1",
            "lookup_tolerance_seconds": TARGET_LOOKUP_TOLERANCE_SECONDS,
        },
        "terminal_label": "finalized Kalshi settlement with official yes/no only",
        "split_policy": {
            "grouping": "whole event/ticker/window groups",
            "ordering": "chronological UTC",
            "purge_embargo": "max_feature_lookback + max_target_horizon",
            "random_row_split": False,
        },
        "frozen_final_test": {
            "dataset_id": DATASET_V1_ID,
            "state": DATASET_V1_FINAL_TEST_STATE,
            "vnext_consumption": False,
        },
        "implementation_order": [
            "structured logistic baseline",
            "structured XGBoost path baseline",
            "causal sequence challenger only after independent evidence gate",
        ],
    }
