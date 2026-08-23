"""Offline-only, event-grouped sequence-dataset contracts for Model Architecture v3.

This module intentionally accepts an immutable sequence snapshot supplied by an
offline archive reader.  It never opens the active recorder database and never
uses retention-pruned HOT rows to invent missing sequence history.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from live15_quant.models import Asset


class SequenceDatasetError(RuntimeError):
    """A sequence timestamp, target, split, or lineage invariant failed."""


class SequenceEvidenceStatus(StrEnum):
    READY = "ready"
    INSUFFICIENT_SEQUENCE_EVIDENCE = "insufficient_sequence_evidence"
    DATA_UNAVAILABLE = "data_unavailable"


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SequenceDatasetError("sequence timestamps must be UTC-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class SequenceFrame:
    """One synchronized Kalshi WS frame plus decision-time predictive inputs."""

    asset: Asset
    event_id: str
    ticker: str
    received_timestamp: datetime
    window_start: datetime
    window_end: datetime
    yes_bid: Decimal
    yes_ask: Decimal
    no_bid: Decimal
    no_ask: Decimal
    yes_depth: Decimal
    no_depth: Decimal
    underlying_price: Decimal | None
    source: str = "kalshi_ws"
    synchronized: bool = True

    def __post_init__(self) -> None:
        for value in (self.received_timestamp, self.window_start, self.window_end):
            _timestamp(value)
        if not self.event_id or not self.ticker or self.source != "kalshi_ws":
            raise SequenceDatasetError("sequence frame must have Kalshi WS event identity")
        if not self.synchronized:
            raise SequenceDatasetError("unsynchronized book cannot enter sequence dataset")
        if not self.window_start <= self.received_timestamp < self.window_end:
            raise SequenceDatasetError("sequence frame is outside its contract window")
        for value in (self.yes_bid, self.yes_ask, self.no_bid, self.no_ask):
            if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
                raise SequenceDatasetError("book price must be in [0, 1]")
        if self.yes_ask < self.yes_bid or self.no_ask < self.no_bid:
            raise SequenceDatasetError("book ask cannot be below bid")
        if (
            not self.yes_depth.is_finite()
            or not self.no_depth.is_finite()
            or self.yes_depth < 0
            or self.no_depth < 0
        ):
            raise SequenceDatasetError("book depth must be finite and non-negative")
        if self.yes_bid + self.yes_ask <= 0:
            raise SequenceDatasetError("sequence frame needs a positive YES midpoint")

    @property
    def midpoint(self) -> Decimal:
        return (self.yes_bid + self.yes_ask) / Decimal(2)


@dataclass(frozen=True, slots=True)
class SequenceTarget:
    horizon: timedelta
    target_timestamp: datetime
    contract_return: Decimal
    direction: int

    def __post_init__(self) -> None:
        _timestamp(self.target_timestamp)
        if self.horizon <= timedelta(0):
            raise SequenceDatasetError("sequence target horizon must be positive")
        if self.direction not in {-1, 0, 1}:
            raise SequenceDatasetError("sequence direction must be -1, 0, or 1")
        if not self.contract_return.is_finite():
            raise SequenceDatasetError("sequence target return must be finite")


@dataclass(frozen=True, slots=True)
class SequenceExample:
    asset: Asset
    event_id: str
    ticker: str
    decision_timestamp: datetime
    frames: tuple[SequenceFrame, ...]
    targets: tuple[SequenceTarget, ...]

    def __post_init__(self) -> None:
        _timestamp(self.decision_timestamp)
        if not self.frames or not self.targets:
            raise SequenceDatasetError("sequence example requires frames and targets")
        if any(
            frame.asset is not self.asset
            or frame.event_id != self.event_id
            or frame.ticker != self.ticker
            for frame in self.frames
        ):
            raise SequenceDatasetError(
                "sequence frames must belong to exactly one asset/event/ticker"
            )
        if tuple(frame.received_timestamp for frame in self.frames) != tuple(
            sorted(frame.received_timestamp for frame in self.frames)
        ):
            raise SequenceDatasetError("sequence frames must be chronological")
        if self.frames[-1].received_timestamp > self.decision_timestamp:
            raise SequenceDatasetError("future sequence frame leaks into the decision")
        if any(target.target_timestamp <= self.decision_timestamp for target in self.targets):
            raise SequenceDatasetError("sequence target must be strictly future of decision")
        if tuple(target.horizon for target in self.targets) != tuple(
            sorted({target.horizon for target in self.targets})
        ):
            raise SequenceDatasetError("sequence target horizons must be unique and ascending")

    @property
    def event_identity(self) -> tuple[str, str, str]:
        """The complete market identity used for temporal split isolation."""
        return (self.asset.value, self.event_id, self.ticker)


@dataclass(frozen=True, slots=True)
class SequenceDatasetConfig:
    lookback_frames: int = 50
    horizons: tuple[timedelta, ...] = (
        timedelta(seconds=10),
        timedelta(seconds=30),
        timedelta(seconds=60),
    )
    target_tolerance: timedelta = timedelta(seconds=3)
    direction_band: Decimal = Decimal("0.002")
    min_events: int = 300
    min_examples: int = 50_000
    min_distinct_days: int = 7

    def __post_init__(self) -> None:
        if self.lookback_frames < 2 or self.target_tolerance < timedelta(0):
            raise ValueError("sequence dataset lookback/tolerance is invalid")
        if not self.horizons or tuple(sorted(set(self.horizons))) != self.horizons:
            raise ValueError("sequence horizons must be unique and ascending")
        if any(value <= timedelta(0) for value in self.horizons):
            raise ValueError("sequence horizons must be positive")
        if (
            not self.direction_band.is_finite()
            or self.direction_band < 0
            or self.min_events <= 0
            or self.min_examples <= 0
            or self.min_distinct_days <= 0
        ):
            raise ValueError("sequence evidence thresholds must be positive")

    def payload(self) -> dict[str, object]:
        return {
            "lookback_frames": self.lookback_frames,
            "horizons_seconds": [int(value.total_seconds()) for value in self.horizons],
            "target_tolerance_seconds": self.target_tolerance.total_seconds(),
            "direction_band": str(self.direction_band),
            "min_events": self.min_events,
            "min_examples": self.min_examples,
            "min_distinct_days": self.min_distinct_days,
        }


@dataclass(frozen=True, slots=True)
class SequenceSnapshotCoverage:
    """Offline-audited coverage for the complete source snapshot, not just retained frames."""

    source_snapshot_id: str
    archive_manifest_hash: str
    synchronized_fraction: Decimal
    gap_free_fraction: Decimal

    def __post_init__(self) -> None:
        if not self.source_snapshot_id or not self.archive_manifest_hash:
            raise SequenceDatasetError("sequence snapshot lineage must not be empty")
        if len(self.archive_manifest_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.archive_manifest_hash.lower()
        ):
            raise SequenceDatasetError("archive manifest dependency must be a SHA-256 hash")
        for value in (self.synchronized_fraction, self.gap_free_fraction):
            if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
                raise SequenceDatasetError("sequence coverage fraction must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class SequenceEvidence:
    source_snapshot_id: str
    archive_manifest_hash: str
    event_count: int
    frame_count: int
    example_count: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    distinct_utc_days: int
    synchronized_fraction: Decimal
    gap_free_fraction: Decimal

    def __post_init__(self) -> None:
        if self.event_count < 0 or self.frame_count < 0 or self.example_count < 0:
            raise SequenceDatasetError("sequence evidence counts cannot be negative")
        if self.distinct_utc_days < 0:
            raise SequenceDatasetError("sequence evidence distinct-day count cannot be negative")
        if (self.first_timestamp is None) != (self.last_timestamp is None):
            raise SequenceDatasetError("sequence evidence range must be complete or absent")
        if self.first_timestamp is not None:
            _timestamp(self.first_timestamp)
            _timestamp(self.last_timestamp)
            assert self.last_timestamp is not None
            if self.last_timestamp < self.first_timestamp:
                raise SequenceDatasetError("sequence evidence range is reversed")
        for value in (self.synchronized_fraction, self.gap_free_fraction):
            if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
                raise SequenceDatasetError("sequence evidence fraction must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class SequenceEvidenceAssessment:
    status: SequenceEvidenceStatus
    reasons: tuple[str, ...]
    evidence: SequenceEvidence


def assess_sequence_evidence(
    evidence: SequenceEvidence, config: SequenceDatasetConfig
) -> SequenceEvidenceAssessment:
    """Fail closed: insufficient or missing evidence never becomes a training pass."""

    reasons: list[str] = []
    if evidence.event_count < config.min_events:
        reasons.append("insufficient_independent_events")
    if evidence.example_count < config.min_examples:
        reasons.append("insufficient_sequence_examples")
    if evidence.distinct_utc_days < config.min_distinct_days:
        reasons.append("insufficient_temporal_diversity")
    if evidence.synchronized_fraction < Decimal("0.99"):
        reasons.append("insufficient_synchronized_orderbook_coverage")
    if evidence.gap_free_fraction < Decimal("0.99"):
        reasons.append("insufficient_gap_free_coverage")
    return SequenceEvidenceAssessment(
        SequenceEvidenceStatus.INSUFFICIENT_SEQUENCE_EVIDENCE
        if reasons
        else SequenceEvidenceStatus.READY,
        tuple(reasons),
        evidence,
    )


@dataclass(frozen=True, slots=True)
class SequenceDataset:
    examples: tuple[SequenceExample, ...]
    splits: dict[str, tuple[SequenceExample, ...]]
    evidence: SequenceEvidence
    config: SequenceDatasetConfig

    def manifest(self, *, git_sha: str) -> dict[str, object]:
        split_events = {
            name: sorted({item.event_identity for item in values})
            for name, values in self.splits.items()
        }
        payload = {
            "format": "live15-sequence-dataset-v1",
            "git_sha": git_sha,
            "source_snapshot_id": self.evidence.source_snapshot_id,
            "archive_manifest_hash": self.evidence.archive_manifest_hash,
            "config": self.config.payload(),
            "splits": {
                name: {
                    "event_ids_digest": _hash(events),
                    "events": len(events),
                    "examples": len(self.splits[name]),
                }
                for name, events in split_events.items()
            },
            "as_of_policy": (
                "frames.received_timestamp<=decision_timestamp; targets strictly future and "
                "same event/ticker"
            ),
        }
        payload["deterministic_build_hash"] = _hash(payload)
        return payload


class SequenceDatasetV1Builder:
    """Build deterministic event-grouped examples from an immutable offline snapshot."""

    def __init__(self, config: SequenceDatasetConfig | None = None) -> None:
        self.config = config or SequenceDatasetConfig()

    def build(
        self,
        frames: Iterable[SequenceFrame],
        *,
        coverage: SequenceSnapshotCoverage,
    ) -> SequenceDataset | SequenceEvidenceAssessment:
        ordered = tuple(
            sorted(frames, key=lambda item: (item.event_id, item.ticker, item.received_timestamp))
        )
        grouped: dict[tuple[str, str], list[SequenceFrame]] = {}
        for frame in ordered:
            grouped.setdefault((frame.event_id, frame.ticker), []).append(frame)
        examples: list[SequenceExample] = []
        for event_frames in grouped.values():
            examples.extend(self._examples_for_event(tuple(event_frames)))
        timestamps = [item.received_timestamp for item in ordered]
        evidence = SequenceEvidence(
            coverage.source_snapshot_id,
            coverage.archive_manifest_hash,
            len(grouped),
            len(ordered),
            len(examples),
            min(timestamps) if timestamps else None,
            max(timestamps) if timestamps else None,
            len({item.date() for item in timestamps}),
            coverage.synchronized_fraction,
            coverage.gap_free_fraction,
        )
        assessment = assess_sequence_evidence(evidence, self.config)
        if assessment.status is not SequenceEvidenceStatus.READY:
            return assessment
        split = self._split(tuple(examples))
        return SequenceDataset(tuple(examples), split, evidence, self.config)

    def _examples_for_event(self, frames: tuple[SequenceFrame, ...]) -> tuple[SequenceExample, ...]:
        if len(frames) < self.config.lookback_frames + 1:
            return ()
        result: list[SequenceExample] = []
        for index in range(self.config.lookback_frames - 1, len(frames)):
            decision = frames[index]
            targets = self._targets(frames, index)
            if len(targets) != len(self.config.horizons):
                continue
            result.append(
                SequenceExample(
                    decision.asset,
                    decision.event_id,
                    decision.ticker,
                    decision.received_timestamp,
                    frames[index - self.config.lookback_frames + 1 : index + 1],
                    targets,
                )
            )
        return tuple(result)

    def _targets(self, frames: tuple[SequenceFrame, ...], index: int) -> tuple[SequenceTarget, ...]:
        current = frames[index]
        result: list[SequenceTarget] = []
        for horizon in self.config.horizons:
            desired = current.received_timestamp + horizon
            future = next(
                (
                    frame
                    for frame in frames[index + 1 :]
                    if frame.received_timestamp >= desired
                    and frame.received_timestamp <= desired + self.config.target_tolerance
                    and frame.received_timestamp < current.window_end
                ),
                None,
            )
            if future is None:
                return ()
            change = (future.midpoint - current.midpoint) / current.midpoint
            direction = (
                1
                if change > self.config.direction_band
                else -1
                if change < -self.config.direction_band
                else 0
            )
            result.append(SequenceTarget(horizon, future.received_timestamp, change, direction))
        return tuple(result)

    @staticmethod
    def _split(examples: tuple[SequenceExample, ...]) -> dict[str, tuple[SequenceExample, ...]]:
        by_event: dict[tuple[str, str, str], list[SequenceExample]] = {}
        event_time: dict[tuple[str, str, str], datetime] = {}
        for example in examples:
            by_event.setdefault(example.event_identity, []).append(example)
            event_time.setdefault(example.event_identity, example.frames[0].window_start)
        events = sorted(by_event, key=lambda key: (event_time[key], key))
        if len(events) < 3:
            raise SequenceDatasetError(
                "sequence dataset needs three chronological event partitions"
            )
        # Reserve at least one complete event for every chronological partition,
        # including the smallest valid three-event dataset.
        train_end = min(max(1, len(events) * 70 // 100), len(events) - 2)
        validation_end = min(max(train_end + 1, len(events) * 85 // 100), len(events) - 1)
        groups = {
            "train": events[:train_end],
            "validation": events[train_end:validation_end],
            "test": events[validation_end:],
        }
        if any(not values for values in groups.values()):
            raise SequenceDatasetError("sequence chronological split is empty")
        output = {
            name: tuple(item for event in event_ids for item in by_event[event])
            for name, event_ids in groups.items()
        }
        latest_train = max(item.decision_timestamp for item in output["train"])
        earliest_validation = min(item.decision_timestamp for item in output["validation"])
        earliest_test = min(item.decision_timestamp for item in output["test"])
        if not latest_train < earliest_validation < earliest_test:
            raise SequenceDatasetError("sequence split is not strictly chronological")
        return output
