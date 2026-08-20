"""Event-group chronological and walk-forward dataset splits."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from live15_quant.dataset import TrainingRow


@dataclass(frozen=True, slots=True)
class DatasetSplit:
    train: tuple[TrainingRow, ...]
    validation: tuple[TrainingRow, ...]
    test: tuple[TrainingRow, ...]

    def __post_init__(self) -> None:
        groups = tuple(
            {row.ticker for row in part} for part in (self.train, self.validation, self.test)
        )
        if groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2]:
            raise ValueError("one event cannot cross dataset split boundaries")


class WalkForwardMode(StrEnum):
    EXPANDING = "expanding"
    ROLLING = "rolling"


@dataclass(frozen=True, slots=True)
class WalkForwardPolicy:
    mode: WalkForwardMode
    train_events: int
    validation_events: int
    test_events: int = 0
    step_events: int = 1

    def __post_init__(self) -> None:
        if min(self.train_events, self.validation_events, self.step_events) <= 0:
            raise ValueError("walk-forward event counts must be positive")
        if self.test_events < 0:
            raise ValueError("walk-forward test event count must be non-negative")


def chronological_split(
    rows: tuple[TrainingRow, ...],
    *,
    train_events: int,
    validation_events: int,
) -> DatasetSplit:
    """Split ordered event groups; all remaining events form the test partition."""

    if train_events <= 0 or validation_events <= 0:
        raise ValueError("chronological split event counts must be positive")
    groups = _event_groups(rows)
    if train_events + validation_events >= len(groups):
        raise ValueError("chronological split requires at least one test event")
    return DatasetSplit(
        train=_flatten(groups[:train_events]),
        validation=_flatten(groups[train_events : train_events + validation_events]),
        test=_flatten(groups[train_events + validation_events :]),
    )


def walk_forward_splits(
    rows: tuple[TrainingRow, ...], policy: WalkForwardPolicy
) -> tuple[DatasetSplit, ...]:
    groups = _event_groups(rows)
    required = policy.train_events + policy.validation_events + policy.test_events
    folds: list[DatasetSplit] = []
    end = required
    while end <= len(groups):
        train_end = end - policy.validation_events - policy.test_events
        train_start = (
            0 if policy.mode is WalkForwardMode.EXPANDING else train_end - policy.train_events
        )
        validation_end = train_end + policy.validation_events
        folds.append(
            DatasetSplit(
                train=_flatten(groups[train_start:train_end]),
                validation=_flatten(groups[train_end:validation_end]),
                test=_flatten(groups[validation_end:end]),
            )
        )
        end += policy.step_events
    return tuple(folds)


def _event_groups(rows: tuple[TrainingRow, ...]) -> tuple[tuple[TrainingRow, ...], ...]:
    grouped: dict[str, list[TrainingRow]] = {}
    for row in rows:
        grouped.setdefault(row.ticker, []).append(row)
    result = tuple(
        tuple(sorted(group, key=lambda row: row.decision_timestamp)) for group in grouped.values()
    )
    return tuple(
        sorted(
            result,
            key=lambda group: (
                group[0].window_start,
                group[0].window_end,
                group[0].ticker,
            ),
        )
    )


def _flatten(groups: tuple[tuple[TrainingRow, ...], ...]) -> tuple[TrainingRow, ...]:
    return tuple(row for group in groups for row in group)
