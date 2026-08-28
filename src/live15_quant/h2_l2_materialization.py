"""Offline, typed H2 L2 snapshot materialization for future microstructure research.

This module is deliberately a conversion boundary, not a data acquisition or training path.
It accepts a typed provider snapshot plus explicit event/as-of provenance, preserves raw ladder
semantics, and refuses future, gapped, cross-event, or holdout-excluded inputs.  Structural test
fixtures can exercise the code, but are never counted as real H2 research evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from live15_quant.historical_providers import (
    DEPTHFEED_KALSHI_L2,
    HistoricalL2Snapshot,
    SnapshotLevel,
)

H2_TIER = "H2_DEPTHFEED_RECORDED_L2"
H2_OVERLAP_VALIDATED = "H2_OVERLAP_VALIDATED"
H2_OVERLAP_PARTIAL = "H2_OVERLAP_PARTIAL"
H2_OVERLAP_FAILED = "H2_OVERLAP_FAILED"
H2_DELTA_SEQUENCE_UNAVAILABLE = "H2_DELTA_SEQUENCE_UNAVAILABLE"
CODE_PIPELINE_READY = "CODE_PIPELINE_READY"
REAL_H2_DATA_READY = "REAL_H2_DATA_READY"
REAL_H2_DATA_NOT_READY = "REAL_H2_DATA_NOT_READY"
SYNTHETIC_TEST_FIXTURE = "SYNTHETIC_TEST_FIXTURE"
REAL_PROVIDER_EVIDENCE = "REAL_PROVIDER_EVIDENCE"


class H2L2MaterializationError(ValueError):
    """A proposed H2 training input violates the causal materialization contract."""


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise H2L2MaterializationError(f"{field}_MUST_BE_TIMEZONE_AWARE")
    return value.astimezone(UTC)


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _canonical(value: object) -> str:
    def default(item: object) -> str:
        if isinstance(item, datetime):
            return _utc(item, "hash_timestamp").isoformat()
        if isinstance(item, Decimal):
            return format(item, "f")
        raise TypeError(f"unsupported canonical value: {type(item)!r}")

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=default)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class L2EventWindow:
    event_id: str
    ticker: str
    window_start: datetime
    window_end: datetime

    def __post_init__(self) -> None:
        if not self.event_id or not self.ticker:
            raise H2L2MaterializationError("EVENT_AND_TICKER_REQUIRED")
        if _utc(self.window_start, "window_start") >= _utc(self.window_end, "window_end"):
            raise H2L2MaterializationError("INVALID_EVENT_WINDOW")


@dataclass(frozen=True, slots=True)
class H2SnapshotEvidence:
    """A provider snapshot coupled to the explicit training-authority metadata it lacks."""

    snapshot: HistoricalL2Snapshot
    event_window: L2EventWindow
    source_timestamp: datetime
    decision_timestamp: datetime
    source_artifact_hash: str
    gap_state: str
    evidence_origin: str
    experiment_cutoff: datetime
    sequence_identity: str | None = None
    overlap_status: str = H2_OVERLAP_PARTIAL
    quality_class: str | None = None
    availability_state: str = "AVAILABLE"
    price_scale: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        source = _utc(self.source_timestamp, "source_timestamp")
        decision = _utc(self.decision_timestamp, "decision_timestamp")
        received = _utc(self.snapshot.received_timestamp, "received_timestamp")
        cutoff = _utc(self.experiment_cutoff, "experiment_cutoff")
        if self.snapshot.provider.provider_id != DEPTHFEED_KALSHI_L2:
            raise H2L2MaterializationError("H2_PROVIDER_ID_REQUIRED")
        if self.snapshot.provider.tier != H2_TIER:
            raise H2L2MaterializationError("H2_PROVENANCE_TIER_REQUIRED")
        if self.snapshot.ticker != self.event_window.ticker:
            raise H2L2MaterializationError("TICKER_EVENT_IDENTITY_MISMATCH")
        if source > decision:
            raise H2L2MaterializationError("SOURCE_AFTER_DECISION")
        if received > decision:
            raise H2L2MaterializationError("RECEIVED_AFTER_DECISION")
        if (
            not _utc(self.event_window.window_start, "window_start")
            <= decision
            <= _utc(self.event_window.window_end, "window_end")
        ):
            raise H2L2MaterializationError("DECISION_OUTSIDE_EVENT_WINDOW")
        if decision > cutoff:
            raise H2L2MaterializationError("EXPERIMENT_CUTOFF_VIOLATION")
        if len(self.source_artifact_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.source_artifact_hash.casefold()
        ):
            raise H2L2MaterializationError("SOURCE_ARTIFACT_HASH_REQUIRED")
        if self.gap_state not in {"NO_GAP", "GAP_DETECTED"}:
            raise H2L2MaterializationError("INVALID_GAP_STATE")
        if self.evidence_origin not in {SYNTHETIC_TEST_FIXTURE, REAL_PROVIDER_EVIDENCE}:
            raise H2L2MaterializationError("INVALID_EVIDENCE_ORIGIN")
        if self.overlap_status not in {
            H2_OVERLAP_VALIDATED,
            H2_OVERLAP_PARTIAL,
            H2_OVERLAP_FAILED,
        }:
            raise H2L2MaterializationError("INVALID_OVERLAP_STATUS")
        if self.price_scale <= 0:
            raise H2L2MaterializationError("INVALID_PRICE_SCALE")


@dataclass(frozen=True, slots=True)
class L2Features:
    yes_best_bid: Decimal | None
    no_best_bid: Decimal | None
    yes_implied_ask: Decimal | None
    yes_spread: Decimal | None
    yes_depth: Decimal
    no_depth: Decimal
    imbalance: Decimal | None
    yes_distance_weighted_depth: Decimal | None
    no_distance_weighted_depth: Decimal | None
    yes_concentration: Decimal | None
    no_concentration: Decimal | None
    yes_slope: Decimal | None
    no_slope: Decimal | None


@dataclass(frozen=True, slots=True)
class MaterializedL2Snapshot:
    example_id: str
    provider_identity: str
    provenance_tier: str
    ticker: str
    event_id: str
    window_start: datetime
    window_end: datetime
    decision_timestamp: datetime
    source_timestamp: datetime
    received_timestamp: datetime
    sequence_identity: str | None
    yes_levels: tuple[SnapshotLevel, ...]
    no_levels: tuple[SnapshotLevel, ...]
    gap_state: str
    quality_class: str
    availability_state: str
    source_artifact_hash: str
    experiment_cutoff: datetime
    evidence_origin: str
    overlap_status: str
    features: L2Features


@dataclass(frozen=True, slots=True)
class H0SnapshotReference:
    """Equivalent native H0 snapshot, kept separate from the H2 materialized type."""

    provider_identity: str
    provenance_tier: str
    ticker: str
    event_id: str
    decision_timestamp: datetime
    yes_levels: tuple[SnapshotLevel, ...]
    no_levels: tuple[SnapshotLevel, ...]
    source_artifact_hash: str

    def __post_init__(self) -> None:
        if self.provenance_tier != "H0_LIVE_NATIVE":
            raise H2L2MaterializationError("H0_PROVENANCE_TIER_REQUIRED")
        _utc(self.decision_timestamp, "h0_decision_timestamp")
        if len(self.source_artifact_hash) != 64:
            raise H2L2MaterializationError("H0_SOURCE_ARTIFACT_HASH_REQUIRED")


@dataclass(frozen=True, slots=True)
class SnapshotSequence:
    sequence_id: str
    event_id: str
    ticker: str
    decision_timestamp: datetime
    source_example_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SequenceExclusion:
    event_id: str
    decision_timestamp: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class SnapshotSequenceBuildResult:
    sequences: tuple[SnapshotSequence, ...]
    exclusions: tuple[SequenceExclusion, ...]


@dataclass(frozen=True, slots=True)
class H2OverlapResult:
    status: str
    matched: tuple[str, ...]
    conflicts: tuple[str, ...]
    reasons: tuple[str, ...]


def _ordered_levels(
    levels: Sequence[SnapshotLevel], *, price_scale: Decimal
) -> tuple[SnapshotLevel, ...]:
    by_price: dict[Decimal, SnapshotLevel] = {}
    for level in levels:
        if level.price > price_scale:
            raise H2L2MaterializationError("PRICE_OUTSIDE_DECLARED_SCALE")
        if level.price in by_price:
            raise H2L2MaterializationError("DUPLICATE_SNAPSHOT_LEVEL")
        by_price[level.price] = level
    return tuple(by_price[price] for price in sorted(by_price, reverse=True))


def _depth_features(
    levels: tuple[SnapshotLevel, ...],
) -> tuple[Decimal, Decimal | None, Decimal | None, Decimal | None]:
    total = sum((level.size for level in levels), Decimal("0"))
    if not levels or total == 0:
        return total, None, None, None
    best = levels[0].price
    weighted = sum(
        (level.size / (Decimal("1") + abs(best - level.price)) for level in levels), Decimal("0")
    )
    concentration = max(level.size for level in levels) / total
    if len(levels) < 2:
        slope = None
    else:
        steps = [
            abs(right.size - left.size) / abs(right.price - left.price)
            for left, right in pairwise(levels)
            if right.price != left.price
        ]
        slope = sum(steps, Decimal("0")) / len(steps) if steps else None
    return total, weighted, concentration, slope


def materialize_snapshot(evidence: H2SnapshotEvidence) -> MaterializedL2Snapshot:
    """Convert one typed provider snapshot into deterministic, provenance-preserving features."""

    yes_levels = _ordered_levels(evidence.snapshot.yes, price_scale=evidence.price_scale)
    no_levels = _ordered_levels(evidence.snapshot.no, price_scale=evidence.price_scale)
    yes_depth, yes_weighted, yes_concentration, yes_slope = _depth_features(yes_levels)
    no_depth, no_weighted, no_concentration, no_slope = _depth_features(no_levels)
    yes_best = yes_levels[0].price if yes_levels else None
    no_best = no_levels[0].price if no_levels else None
    yes_implied_ask = evidence.price_scale - no_best if no_best is not None else None
    yes_spread = (
        yes_implied_ask - yes_best if yes_implied_ask is not None and yes_best is not None else None
    )
    total_depth = yes_depth + no_depth
    features = L2Features(
        yes_best_bid=yes_best,
        no_best_bid=no_best,
        yes_implied_ask=yes_implied_ask,
        yes_spread=yes_spread,
        yes_depth=yes_depth,
        no_depth=no_depth,
        imbalance=(yes_depth - no_depth) / total_depth if total_depth else None,
        yes_distance_weighted_depth=yes_weighted,
        no_distance_weighted_depth=no_weighted,
        yes_concentration=yes_concentration,
        no_concentration=no_concentration,
        yes_slope=yes_slope,
        no_slope=no_slope,
    )
    identity = {
        "provider_identity": evidence.snapshot.provider.provider_id,
        "ticker": evidence.snapshot.ticker,
        "event_id": evidence.event_window.event_id,
        "decision_timestamp": _utc(evidence.decision_timestamp, "decision_timestamp").isoformat(),
        "source_timestamp": _utc(evidence.source_timestamp, "source_timestamp").isoformat(),
        "received_timestamp": _utc(
            evidence.snapshot.received_timestamp, "received_timestamp"
        ).isoformat(),
        "sequence_identity": evidence.sequence_identity,
        "yes_levels": [(_decimal(level.price), _decimal(level.size)) for level in yes_levels],
        "no_levels": [(_decimal(level.price), _decimal(level.size)) for level in no_levels],
        "gap_state": evidence.gap_state,
        "quality_class": evidence.quality_class or evidence.snapshot.quality_class,
        "availability_state": evidence.availability_state,
        "source_artifact_hash": evidence.source_artifact_hash,
        "experiment_cutoff": _utc(evidence.experiment_cutoff, "experiment_cutoff").isoformat(),
    }
    return MaterializedL2Snapshot(
        example_id="h2-l2-" + _hash(identity)[:32],
        provider_identity=evidence.snapshot.provider.provider_id,
        provenance_tier=evidence.snapshot.provider.tier,
        ticker=evidence.snapshot.ticker,
        event_id=evidence.event_window.event_id,
        window_start=_utc(evidence.event_window.window_start, "window_start"),
        window_end=_utc(evidence.event_window.window_end, "window_end"),
        decision_timestamp=_utc(evidence.decision_timestamp, "decision_timestamp"),
        source_timestamp=_utc(evidence.source_timestamp, "source_timestamp"),
        received_timestamp=_utc(evidence.snapshot.received_timestamp, "received_timestamp"),
        sequence_identity=evidence.sequence_identity,
        yes_levels=yes_levels,
        no_levels=no_levels,
        gap_state=evidence.gap_state,
        quality_class=evidence.quality_class or evidence.snapshot.quality_class,
        availability_state=evidence.availability_state,
        source_artifact_hash=evidence.source_artifact_hash,
        experiment_cutoff=_utc(evidence.experiment_cutoff, "experiment_cutoff"),
        evidence_origin=evidence.evidence_origin,
        overlap_status=evidence.overlap_status,
        features=features,
    )


def build_snapshot_sequences(
    examples: Iterable[MaterializedL2Snapshot],
    *,
    lookback: int,
    excluded_event_ids: Sequence[str] | None = None,
) -> SnapshotSequenceBuildResult:
    """Build fixed, event-local snapshot windows; never fill a missing or gapped snapshot."""

    if lookback <= 0:
        raise H2L2MaterializationError("LOOKBACK_MUST_BE_POSITIVE")
    if excluded_event_ids is None:
        raise H2L2MaterializationError("HOLDOUT_IDENTITY_EXCLUSIONS_REQUIRED")
    rows = tuple(examples)
    if not rows:
        return SnapshotSequenceBuildResult((), ())
    event_ids = {row.event_id for row in rows}
    if len(event_ids) != 1:
        raise H2L2MaterializationError("CROSS_EVENT_SEQUENCE_FORBIDDEN")
    event_id = next(iter(event_ids))
    if event_id in set(excluded_event_ids):
        exclusion = SequenceExclusion(
            event_id, min(row.decision_timestamp for row in rows), "HOLDOUT_IDENTITY_EXCLUDED"
        )
        return SnapshotSequenceBuildResult((), (exclusion,))
    ordered = tuple(
        sorted(rows, key=lambda row: (row.decision_timestamp, row.source_timestamp, row.example_id))
    )
    sequences: list[SnapshotSequence] = []
    exclusions: list[SequenceExclusion] = []
    seen_observations: set[tuple[str, str, datetime, datetime]] = set()
    for row in ordered:
        key = (row.event_id, row.ticker, row.decision_timestamp, row.source_timestamp)
        if key in seen_observations:
            exclusions.append(
                SequenceExclusion(event_id, row.decision_timestamp, "DUPLICATE_SNAPSHOT_REJECTED")
            )
        seen_observations.add(key)
    if exclusions:
        return SnapshotSequenceBuildResult((), tuple(exclusions))
    for index in range(lookback - 1, len(ordered)):
        window = ordered[index - lookback + 1 : index + 1]
        decision = window[-1].decision_timestamp
        if any(
            row.gap_state != "NO_GAP" or row.availability_state != "AVAILABLE" for row in window
        ):
            exclusions.append(SequenceExclusion(event_id, decision, "GAP_REJECTED"))
            continue
        if any(
            row.source_timestamp > decision or row.received_timestamp > decision for row in window
        ):
            exclusions.append(SequenceExclusion(event_id, decision, "FUTURE_ROW_REJECTED"))
            continue
        if any(
            row.window_start != window[0].window_start or row.window_end != window[0].window_end
            for row in window
        ):
            exclusions.append(SequenceExclusion(event_id, decision, "EVENT_WINDOW_MISMATCH"))
            continue
        identity = {
            "event_id": event_id,
            "ticker": window[-1].ticker,
            "decision_timestamp": decision.isoformat(),
            "source_example_ids": [row.example_id for row in window],
        }
        sequences.append(
            SnapshotSequence(
                sequence_id="h2-seq-" + _hash(identity)[:32],
                event_id=event_id,
                ticker=window[-1].ticker,
                decision_timestamp=decision,
                source_example_ids=tuple(row.example_id for row in window),
            )
        )
    return SnapshotSequenceBuildResult(
        tuple(sequences),
        tuple(sorted(exclusions, key=lambda item: (item.decision_timestamp, item.reason))),
    )


def _overlap_key(example: MaterializedL2Snapshot) -> tuple[str, str, datetime]:
    return (example.event_id, example.ticker, example.decision_timestamp)


def _book_identity(
    example: MaterializedL2Snapshot,
) -> tuple[tuple[tuple[Decimal, Decimal], ...], tuple[tuple[Decimal, Decimal], ...]]:
    return (
        tuple((level.price, level.size) for level in example.yes_levels),
        tuple((level.price, level.size) for level in example.no_levels),
    )


def evaluate_h2_overlap(
    h2_examples: Iterable[MaterializedL2Snapshot], h0_examples: Iterable[H0SnapshotReference]
) -> H2OverlapResult:
    """Compare bounded equivalent snapshots; conflict is fail-closed and never selects H2."""

    h0_by_key: dict[tuple[str, str, datetime], H0SnapshotReference] = {}
    for item in h0_examples:
        key = (item.event_id, item.ticker, item.decision_timestamp)
        if key in h0_by_key:
            return H2OverlapResult(
                H2_OVERLAP_FAILED,
                (),
                (),
                ("H0_DUPLICATE_OR_CONFLICT_QUARANTINED",),
            )
        h0_by_key[key] = item
    matched: list[str] = []
    conflicts: list[str] = []
    for h2 in h2_examples:
        reference = h0_by_key.get(_overlap_key(h2))
        if reference is None:
            continue
        reference_book = (
            tuple((level.price, level.size) for level in reference.yes_levels),
            tuple((level.price, level.size) for level in reference.no_levels),
        )
        if _book_identity(h2) == reference_book:
            matched.append(h2.example_id)
        else:
            conflicts.append(h2.example_id)
    if conflicts:
        return H2OverlapResult(
            H2_OVERLAP_FAILED,
            tuple(sorted(matched)),
            tuple(sorted(conflicts)),
            ("H0_H2_CONFLICT_QUARANTINED",),
        )
    if not matched:
        return H2OverlapResult(H2_OVERLAP_PARTIAL, (), (), ("NO_EQUIVALENT_H0_SNAPSHOTS",))
    return H2OverlapResult(H2_OVERLAP_VALIDATED, tuple(sorted(matched)), (), ())


def evaluate_h2_overlap_with_tolerance(
    h2_examples: Iterable[MaterializedL2Snapshot],
    h0_examples: Iterable[H0SnapshotReference],
    *,
    timestamp_tolerance: timedelta,
) -> H2OverlapResult:
    """Compare equivalent books at a bounded nearest native-H0 timestamp; H0 wins conflicts."""

    if timestamp_tolerance < timedelta(0):
        raise H2L2MaterializationError("NEGATIVE_OVERLAP_TOLERANCE")
    h2_rows = tuple(h2_examples)
    h0_rows = tuple(h0_examples)
    exact = evaluate_h2_overlap(h2_rows, h0_rows)
    if exact.status != H2_OVERLAP_PARTIAL:
        return exact
    matched: list[str] = []
    conflicts: list[str] = []
    for h2 in h2_rows:
        candidates = [
            item
            for item in h0_rows
            if item.event_id == h2.event_id
            and item.ticker == h2.ticker
            and abs(item.decision_timestamp - h2.decision_timestamp) <= timestamp_tolerance
        ]
        if not candidates:
            continue
        candidate_books = {
            (
                tuple((level.price, level.size) for level in item.yes_levels),
                tuple((level.price, level.size) for level in item.no_levels),
            )
            for item in candidates
        }
        if len(candidate_books) != 1:
            conflicts.append(h2.example_id)
            continue
        reference_book = next(iter(candidate_books))
        if _book_identity(h2) == reference_book:
            matched.append(h2.example_id)
        else:
            conflicts.append(h2.example_id)
    if conflicts:
        return H2OverlapResult(
            H2_OVERLAP_FAILED,
            tuple(sorted(matched)),
            tuple(sorted(conflicts)),
            ("H0_H2_CONFLICT_QUARANTINED",),
        )
    if matched:
        return H2OverlapResult(H2_OVERLAP_VALIDATED, tuple(sorted(matched)), (), ())
    return exact


def summarize_h2_capabilities(
    examples: Iterable[MaterializedL2Snapshot],
    sequence_result: SnapshotSequenceBuildResult,
    *,
    overlap_result: H2OverlapResult | None,
) -> dict[str, object]:
    """Report code and real-data H2 status separately without inventing delta capability."""

    rows = tuple(examples)
    validated_ids = (
        set(overlap_result.matched)
        if overlap_result is not None and overlap_result.status == H2_OVERLAP_VALIDATED
        else set()
    )
    validated_real = tuple(
        item
        for item in rows
        if item.evidence_origin == REAL_PROVIDER_EVIDENCE
        and item.example_id in validated_ids
        and item.gap_state == "NO_GAP"
        and item.availability_state == "AVAILABLE"
    )
    real_ids = {item.example_id for item in validated_real}
    real_sequence_days = tuple(
        sorted(
            {
                sequence.decision_timestamp.date().isoformat()
                for sequence in sequence_result.sequences
                if set(sequence.source_example_ids).issubset(real_ids)
            }
        )
    )
    snapshot_days = tuple(
        sorted({item.decision_timestamp.date().isoformat() for item in validated_real})
    )
    return {
        "code_pipeline_status": CODE_PIPELINE_READY if rows else "CODE_PIPELINE_NOT_EXERCISED",
        "real_h2_data_status": REAL_H2_DATA_READY if snapshot_days else REAL_H2_DATA_NOT_READY,
        "snapshot_days": snapshot_days,
        "delta_days": (),
        "snapshot_sequence_days": real_sequence_days,
        "delta_sequence_days": (),
        "microstructure_training_ready_days": snapshot_days,
        "delta_sequence_status": H2_DELTA_SEQUENCE_UNAVAILABLE,
        "overlap_status": overlap_result.status if overlap_result else H2_OVERLAP_PARTIAL,
        "overlap_artifact_id": (
            _hash(
                {
                    "matched": overlap_result.matched,
                    "conflicts": overlap_result.conflicts,
                    "reasons": overlap_result.reasons,
                }
            )
            if overlap_result is not None and overlap_result.status == H2_OVERLAP_VALIDATED
            else None
        ),
        "synthetic_fixture_count": sum(
            item.evidence_origin == SYNTHETIC_TEST_FIXTURE for item in rows
        ),
        "real_provider_example_count": len(validated_real),
    }


def canonical_microstructure_availability(
    summary: dict[str, object],
) -> dict[str, dict[str, object]]:
    """Convert an H2 capability summary into the explicit CanonicalEvidence capability shape."""

    def days(key: str) -> tuple[str, ...]:
        value = summary.get(key, ())
        if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
            raise H2L2MaterializationError(f"INVALID_CAPABILITY_{key.upper()}")
        return value

    snapshot_days = days("snapshot_days")
    delta_days = days("delta_days")
    snapshot_sequence_days = days("snapshot_sequence_days")
    delta_sequence_days = days("delta_sequence_days")
    training_ready_days = days("microstructure_training_ready_days")
    return {
        "snapshot": {"available": bool(snapshot_days), "days": len(snapshot_days)},
        "delta": {"available": bool(delta_days), "days": len(delta_days)},
        "snapshot_sequence": {
            "available": bool(snapshot_sequence_days),
            "days": len(snapshot_sequence_days),
        },
        "delta_sequence": {
            "available": bool(delta_sequence_days),
            "days": len(delta_sequence_days),
            "status": summary.get("delta_sequence_status"),
        },
        "training_ready": {
            "available": bool(training_ready_days),
            "days": len(training_ready_days),
        },
        "overlap": {
            "validated": summary.get("overlap_status") == H2_OVERLAP_VALIDATED,
            "artifact_id": summary.get("overlap_artifact_id"),
        },
    }
