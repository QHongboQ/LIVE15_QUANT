"""Typed normalization profiles fitted only from caller-selected training rows."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from live15_quant.dataset import TrainingRow
from live15_quant.feature_registry import FEATURE_BY_NAME


class NormalizationScope(StrEnum):
    GLOBAL = "global"
    PER_ASSET = "per_asset"


@dataclass(frozen=True, slots=True)
class NormalizationPolicy:
    scope: NormalizationScope
    feature_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.feature_names or len(set(self.feature_names)) != len(self.feature_names):
            raise ValueError("normalization features must be non-empty and unique")
        unknown = set(self.feature_names) - set(FEATURE_BY_NAME)
        if unknown:
            raise ValueError(f"normalization features are not registered: {sorted(unknown)}")


@dataclass(frozen=True, slots=True)
class NormalizationStatistic:
    group: str
    feature_name: str
    count: int
    mean: Decimal
    standard_deviation: Decimal


@dataclass(frozen=True, slots=True)
class NormalizationProfile:
    policy: NormalizationPolicy
    statistics: tuple[NormalizationStatistic, ...]

    def transform(self, row: TrainingRow) -> Mapping[str, Decimal | None]:
        group = "global" if self.policy.scope is NormalizationScope.GLOBAL else row.asset.value
        stats = {(item.group, item.feature_name): item for item in self.statistics}
        observations = row.features.by_name()
        transformed: dict[str, Decimal | None] = {}
        for name in self.policy.feature_names:
            observation = observations[name]
            statistic = stats.get((group, name))
            if observation.value is None or statistic is None:
                transformed[name] = None
            elif statistic.standard_deviation == 0:
                transformed[name] = Decimal(0)
            else:
                transformed[name] = (
                    observation.value - statistic.mean
                ) / statistic.standard_deviation
        return transformed


def fit_normalization(
    rows: tuple[TrainingRow, ...], policy: NormalizationPolicy
) -> NormalizationProfile:
    """Fit caller-selected rows; callers must pass train groups only to avoid leakage."""

    values: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    for row in rows:
        group = "global" if policy.scope is NormalizationScope.GLOBAL else row.asset.value
        observations = row.features.by_name()
        for name in policy.feature_names:
            value = observations[name].value
            if value is not None:
                values[(group, name)].append(value)
    statistics = tuple(
        _statistic(group, name, samples) for (group, name), samples in sorted(values.items())
    )
    return NormalizationProfile(policy, statistics)


def _statistic(group: str, feature_name: str, values: list[Decimal]) -> NormalizationStatistic:
    mean = sum(values, Decimal(0)) / Decimal(len(values))
    variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / Decimal(len(values))
    return NormalizationStatistic(group, feature_name, len(values), mean, variance.sqrt())
