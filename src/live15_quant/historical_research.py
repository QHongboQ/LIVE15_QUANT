"""Leakage-safe historical research manifests and chronological walk-forward folds.

HIST-001 is a research substrate only.  It records provenance and deterministic fold plans;
it does not download history, train models, or alter the authoritative Recorder/Dataset stores.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from live15_quant.model_vnext_contract import LeakageChecker

HISTORICAL_RESEARCH_SCHEMA_VERSION = "1.0.0"
PURGE_EMBARGO_SECONDS = 600
_PROHIBITED_FEATURE_TOKENS = ("label", "settlement", "final_result", "outcome", "resolved")


class HistoricalResearchError(ValueError):
    """Historical research metadata or split planning is invalid."""


class HistoricalLeakageError(HistoricalResearchError):
    """A historical sample violates the MVN-001 as-of contract."""


class HistoricalTier(StrEnum):
    H0 = "H0_LIVE_NATIVE"
    H1 = "H1_VERIFIED_HISTORICAL"
    H2 = "H2_LIMITED_CONTRACT"


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HistoricalResearchError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True, slots=True)
class HistoricalSource:
    source_id: str
    tier: HistoricalTier
    data_type: str
    earliest: datetime
    latest: datetime
    frequency: str
    as_of_quality: str
    intended_use: str
    limitations: tuple[str, ...] = ()
    row_count: int = 0
    event_count: int = 0
    assets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.source_id or not self.data_type:
            raise HistoricalResearchError("source_id and data_type are required")
        start = _aware(self.earliest, "earliest")
        end = _aware(self.latest, "latest")
        if start > end:
            raise HistoricalResearchError("source earliest must not be after latest")
        if min(self.row_count, self.event_count) < 0:
            raise HistoricalResearchError("source counts must be non-negative")
        if len(set(self.assets)) != len(self.assets):
            raise HistoricalResearchError("source assets must be unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "tier": self.tier.value,
            "data_type": self.data_type,
            "earliest": _aware(self.earliest, "earliest").isoformat(),
            "latest": _aware(self.latest, "latest").isoformat(),
            "frequency": self.frequency,
            "as_of_quality": self.as_of_quality,
            "intended_use": self.intended_use,
            "limitations": list(self.limitations),
            "row_count": self.row_count,
            "event_count": self.event_count,
            "assets": list(self.assets),
        }


@dataclass(frozen=True, slots=True)
class HistoricalSample:
    sample_id: str
    event_id: str
    asset: str
    source_id: str
    provenance_tier: HistoricalTier
    window_start: datetime
    window_end: datetime
    decision_timestamp: datetime
    source_timestamp: datetime | None
    received_timestamp: datetime | None
    target_timestamp: datetime | None = None
    feature_names: tuple[str, ...] = ()
    available: bool = True
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        start = _aware(self.window_start, "window_start")
        end = _aware(self.window_end, "window_end")
        decision = _aware(self.decision_timestamp, "decision_timestamp")
        if not self.sample_id or not self.event_id or not self.asset or not self.source_id:
            raise HistoricalResearchError("sample identity fields are required")
        if not start <= decision < end:
            raise HistoricalResearchError("decision_timestamp must be inside the event window")
        if self.available and self.source_timestamp is None:
            raise HistoricalLeakageError("available sample requires source_timestamp")
        if self.source_timestamp is not None:
            source = _aware(self.source_timestamp, "source_timestamp")
            if source > decision:
                raise HistoricalLeakageError("source_timestamp is after decision_timestamp")
        if self.received_timestamp is not None:
            received = _aware(self.received_timestamp, "received_timestamp")
            if received > decision:
                raise HistoricalLeakageError("received_timestamp is after decision_timestamp")
        if self.target_timestamp is not None:
            target = _aware(self.target_timestamp, "target_timestamp")
            if target <= decision:
                raise HistoricalLeakageError("target_timestamp must be after decision_timestamp")
            if target > end:
                raise HistoricalLeakageError("target_timestamp is outside the event window")
        if not self.available and not self.missing_reason:
            raise HistoricalResearchError("excluded sample requires missing_reason")
        if self.available and self.missing_reason is not None:
            raise HistoricalResearchError("available sample cannot carry missing_reason")
        prohibited = tuple(
            name
            for name in self.feature_names
            if any(token in name.lower() for token in _PROHIBITED_FEATURE_TOKENS)
        )
        if prohibited:
            raise HistoricalLeakageError(
                "settlement/label fields cannot enter historical features: " + ",".join(prohibited)
            )

    @property
    def excluded(self) -> bool:
        return not self.available


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    train_days: int
    validation_days: int
    step_days: int = 1
    purge_embargo_seconds: int = PURGE_EMBARGO_SECONDS
    mode: str = "expanding"

    def __post_init__(self) -> None:
        if min(self.train_days, self.validation_days, self.step_days) <= 0:
            raise HistoricalResearchError("walk-forward day counts must be positive")
        if self.purge_embargo_seconds < PURGE_EMBARGO_SECONDS:
            raise HistoricalResearchError("purge/embargo cannot weaken the 600-second contract")
        if self.mode not in {"expanding", "rolling"}:
            raise HistoricalResearchError("walk-forward mode must be expanding or rolling")


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold_id: str
    train_event_ids: tuple[str, ...]
    validation_event_ids: tuple[str, ...]
    purged_event_ids: tuple[str, ...]
    train_samples: int
    validation_samples: int
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    purge_embargo_seconds: int

    def to_dict(self) -> dict[str, object]:
        return {
            "fold_id": self.fold_id,
            "train_event_ids": list(self.train_event_ids),
            "validation_event_ids": list(self.validation_event_ids),
            "purged_event_ids": list(self.purged_event_ids),
            "train_samples": self.train_samples,
            "validation_samples": self.validation_samples,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "validation_start": self.validation_start.isoformat(),
            "validation_end": self.validation_end.isoformat(),
            "purge_embargo_seconds": self.purge_embargo_seconds,
        }


@dataclass(frozen=True, slots=True)
class HistoricalResearchManifest:
    dataset_id: str
    build_hash: str
    schema_version: str
    code_sha: str
    config: Mapping[str, object]
    sources: tuple[HistoricalSource, ...]
    earliest_timestamp: datetime | None
    latest_timestamp: datetime | None
    independent_utc_days: int
    event_count: int
    sample_count: int
    excluded_sample_count: int
    asset_universe: tuple[str, ...]
    source_sample_counts: Mapping[str, int]
    leakage_rules: tuple[str, ...]
    dataset_v2_touched: bool = False
    holdout_accessed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_name": "HistoricalResearchDataset",
            "dataset_v3_relation": "separate_historical_lineage; not Dataset v3",
            "build_hash": self.build_hash,
            "schema_version": self.schema_version,
            "code_sha": self.code_sha,
            "config": dict(self.config),
            "sources": [source.to_dict() for source in self.sources],
            "earliest_timestamp": (
                self.earliest_timestamp.isoformat() if self.earliest_timestamp else None
            ),
            "latest_timestamp": self.latest_timestamp.isoformat()
            if self.latest_timestamp
            else None,
            "independent_utc_days": self.independent_utc_days,
            "event_count": self.event_count,
            "sample_count": self.sample_count,
            "excluded_sample_count": self.excluded_sample_count,
            "asset_universe": list(self.asset_universe),
            "source_sample_counts": dict(self.source_sample_counts),
            "leakage_rules": list(self.leakage_rules),
            "dataset_v2_touched": self.dataset_v2_touched,
            "holdout_accessed": self.holdout_accessed,
        }


def build_manifest(
    *,
    sources: tuple[HistoricalSource, ...],
    samples: tuple[HistoricalSample, ...],
    code_sha: str,
    config: Mapping[str, object],
) -> HistoricalResearchManifest:
    """Create a stable manifest from source boundaries and sample provenance only."""

    if not code_sha:
        raise HistoricalResearchError("code_sha is required for historical lineage")
    if len({source.source_id for source in sources}) != len(sources):
        raise HistoricalResearchError("source IDs must be unique")
    source_ids = {source.source_id for source in sources}
    if any(sample.source_id not in source_ids for sample in samples):
        raise HistoricalResearchError("sample references an unknown source")
    active = tuple(sample for sample in samples if not sample.excluded)
    timestamps = [sample.source_timestamp or sample.decision_timestamp for sample in active]
    sample_records = [
        {
            "sample_id": sample.sample_id,
            "event_id": sample.event_id,
            "asset": sample.asset,
            "source_id": sample.source_id,
            "tier": sample.provenance_tier.value,
            "window_start": _aware(sample.window_start, "window_start").isoformat(),
            "window_end": _aware(sample.window_end, "window_end").isoformat(),
            "decision": _aware(sample.decision_timestamp, "decision_timestamp").isoformat(),
            "source": sample.source_timestamp.isoformat() if sample.source_timestamp else None,
            "received": sample.received_timestamp.isoformat()
            if sample.received_timestamp
            else None,
            "target": sample.target_timestamp.isoformat() if sample.target_timestamp else None,
            "available": sample.available,
            "missing_reason": sample.missing_reason,
        }
        for sample in sorted(samples, key=lambda item: item.sample_id)
    ]
    payload = {
        "schema_version": HISTORICAL_RESEARCH_SCHEMA_VERSION,
        "code_sha": code_sha,
        "config": dict(config),
        "sources": [
            source.to_dict() for source in sorted(sources, key=lambda item: item.source_id)
        ],
        "samples": sample_records,
    }
    build_hash = hashlib.sha256(_canonical(payload).encode()).hexdigest()
    dataset_id = f"historical-research-{build_hash[:24]}"
    return HistoricalResearchManifest(
        dataset_id=dataset_id,
        build_hash=build_hash,
        schema_version=HISTORICAL_RESEARCH_SCHEMA_VERSION,
        code_sha=code_sha,
        config=dict(config),
        sources=tuple(sorted(sources, key=lambda item: item.source_id)),
        earliest_timestamp=min(timestamps) if timestamps else None,
        latest_timestamp=max(timestamps) if timestamps else None,
        independent_utc_days=len({item.decision_timestamp.date() for item in active}),
        event_count=len({item.event_id for item in active}),
        sample_count=len(active),
        excluded_sample_count=len(samples) - len(active),
        asset_universe=tuple(sorted({item.asset for item in active})),
        source_sample_counts={
            source_id: sum(item.source_id == source_id for item in active)
            for source_id in sorted(source_ids)
        },
        leakage_rules=(
            "source_timestamp <= decision_timestamp",
            "received_timestamp <= decision_timestamp when present",
            "target_timestamp > decision_timestamp and within event window",
            "whole-event chronological walk-forward",
            "train-only transforms",
            "no forward-fill, interpolation, or implicit zero fill",
            "Dataset v2 and its holdout are isolated",
        ),
    )


def build_walk_forward_folds(
    samples: tuple[HistoricalSample, ...], config: WalkForwardConfig
) -> tuple[WalkForwardFold, ...]:
    """Build expanding or rolling UTC-day folds with whole-event purge/embargo."""

    active = tuple(sample for sample in samples if not sample.excluded)
    groups: dict[str, list[HistoricalSample]] = {}
    for sample in active:
        groups.setdefault(sample.event_id, []).append(sample)
    ordered_groups = {
        event_id: tuple(sorted(items, key=lambda item: item.decision_timestamp))
        for event_id, items in groups.items()
    }
    days = sorted({item.window_start.date() for item in active})
    folds: list[WalkForwardFold] = []
    start_index = config.train_days
    fold_index = 0
    while start_index + config.validation_days <= len(days):
        train_dates = (
            tuple(days[:start_index])
            if config.mode == "expanding"
            else tuple(days[max(0, start_index - config.train_days) : start_index])
        )
        validation_dates = tuple(days[start_index : start_index + config.validation_days])
        validation_start = datetime.combine(validation_dates[0], datetime.min.time(), tzinfo=UTC)
        purge_cutoff = validation_start - timedelta(seconds=config.purge_embargo_seconds)
        train_ids: list[str] = []
        validation_ids: list[str] = []
        purged_ids: list[str] = []
        for event_id, event_samples in ordered_groups.items():
            event_start = min(item.window_start for item in event_samples)
            event_end = max(item.window_end for item in event_samples)
            if event_start.date() in train_dates:
                if event_end <= purge_cutoff:
                    train_ids.append(event_id)
                else:
                    purged_ids.append(event_id)
            elif event_start.date() in validation_dates and event_start >= validation_start:
                validation_ids.append(event_id)
        train_ids.sort(
            key=lambda event_id: min(item.window_start for item in ordered_groups[event_id])
        )
        validation_ids.sort(
            key=lambda event_id: min(item.window_start for item in ordered_groups[event_id])
        )
        purged_ids.sort()
        train_rows = tuple(item for event_id in train_ids for item in ordered_groups[event_id])
        validation_rows = tuple(
            item for event_id in validation_ids for item in ordered_groups[event_id]
        )
        if train_rows and validation_rows:
            LeakageChecker().check_splits({"train": train_rows, "validation": validation_rows})
            fold_start = min(item.window_start for item in train_rows)
            fold_train_end = max(item.window_end for item in train_rows)
            fold_validation_start = min(item.window_start for item in validation_rows)
            fold_validation_end = max(item.window_end for item in validation_rows)
            folds.append(
                WalkForwardFold(
                    fold_id=f"wf-{fold_index:03d}",
                    train_event_ids=tuple(train_ids),
                    validation_event_ids=tuple(validation_ids),
                    purged_event_ids=tuple(purged_ids),
                    train_samples=len(train_rows),
                    validation_samples=len(validation_rows),
                    train_start=fold_start,
                    train_end=fold_train_end,
                    validation_start=fold_validation_start,
                    validation_end=fold_validation_end,
                    purge_embargo_seconds=config.purge_embargo_seconds,
                )
            )
            fold_index += 1
        start_index += config.step_days
    return tuple(folds)


def capability_matrix(sources: tuple[HistoricalSource, ...]) -> tuple[dict[str, object], ...]:
    """Return a stable machine-readable source capability table."""

    return tuple(
        {
            "data_type": source.data_type,
            "source_id": source.source_id,
            "tier": source.tier.value,
            "historical_availability": "available" if source.row_count else "not_observed",
            "earliest": _aware(source.earliest, "earliest").isoformat(),
            "latest": _aware(source.latest, "latest").isoformat(),
            "frequency": source.frequency,
            "as_of_quality": source.as_of_quality,
            "intended_use": source.intended_use,
            "limitations": list(source.limitations),
        }
        for source in sorted(sources, key=lambda item: item.source_id)
    )
