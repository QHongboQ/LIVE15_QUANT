"""Causal, offline sequence representation and readiness evidence for HIST-003.

The module is deliberately a representation/readiness layer.  It does not train a model,
touch Dataset v2, or expose a runtime/Paper/Production path.  Every observation is checked
against the decision timestamp and every target is an exact, event-local observation; there
is no interpolation, forward-fill, or future-nearest lookup.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path

SEQUENCE_SCHEMA_VERSION = "1.0.0"
HIST003_DATASET_ID = "historical-research-f2d529adfb95080971becdaf"
DEFAULT_SEQUENCE_LENGTHS = (3, 5, 8, 10)
DEFAULT_HORIZONS_SECONDS = (30, 60, 120, 180, 300)
PURGE_EMBARGO_SECONDS = 600


class SequenceReadinessStatus(StrEnum):
    READY = "SEQUENCE_READY_FOR_BOUNDED_MODEL_TRAINING"
    PARTIAL = "SEQUENCE_PARTIAL_MORE_DATA_OR_REPRESENTATION_NEEDED"
    BLOCKED = "SEQUENCE_BLOCKED_DATA_INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class SequenceEvent:
    event_id: str
    asset: str
    open_time: datetime
    close_time: datetime

    def __post_init__(self) -> None:
        if not self.event_id or not self.asset or self.open_time.tzinfo is None:
            raise ValueError("event identity and timezone-aware bounds are required")
        if self.close_time.tzinfo is None or self.open_time >= self.close_time:
            raise ValueError("event close must be after event open")


@dataclass(frozen=True, slots=True)
class SequenceObservation:
    event_id: str
    timestamp: datetime
    source_timestamp: datetime
    received_timestamp: datetime | None
    value: float
    resolution: str

    def __post_init__(self) -> None:
        if (
            not self.event_id
            or self.timestamp.tzinfo is None
            or self.source_timestamp.tzinfo is None
        ):
            raise ValueError("observation identity and timestamps are required")
        if self.received_timestamp is not None and self.received_timestamp.tzinfo is None:
            raise ValueError("received_timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SequenceExclusion:
    event_id: str
    decision_timestamp: datetime | None
    horizon_seconds: int
    length: int
    reason: str


@dataclass(frozen=True, slots=True)
class SequenceSample:
    sample_id: str
    dataset_id: str
    event_id: str
    asset: str
    resolution: str
    length: int
    horizon_seconds: int
    decision_timestamp: datetime
    sequence_start: datetime
    sequence_end: datetime
    target_timestamp: datetime
    source_timestamps: tuple[datetime, ...]
    normalization_scope: str = "train_fold_only"

    def __post_init__(self) -> None:
        if self.target_timestamp <= self.decision_timestamp:
            raise ValueError("target must be after decision")
        if self.sequence_end != self.decision_timestamp:
            raise ValueError("sequence must end at the decision timestamp")
        if len(self.source_timestamps) != self.length:
            raise ValueError("source timestamp count must equal sequence length")
        if any(value > self.decision_timestamp for value in self.source_timestamps):
            raise ValueError("sequence source timestamp is after decision")
        if self.normalization_scope != "train_fold_only":
            raise ValueError("normalization must be train-fold-only")

    def to_dict(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "dataset_id": self.dataset_id,
            "event_id": self.event_id,
            "asset": self.asset,
            "resolution": self.resolution,
            "length": self.length,
            "horizon_seconds": self.horizon_seconds,
            "decision_timestamp": self.decision_timestamp.astimezone(UTC).isoformat(),
            "sequence_start": self.sequence_start.astimezone(UTC).isoformat(),
            "sequence_end": self.sequence_end.astimezone(UTC).isoformat(),
            "target_timestamp": self.target_timestamp.astimezone(UTC).isoformat(),
            "source_timestamps": [
                item.astimezone(UTC).isoformat() for item in self.source_timestamps
            ],
            "normalization_scope": self.normalization_scope,
        }


@dataclass(frozen=True, slots=True)
class SequenceBuildResult:
    samples: tuple[SequenceSample, ...]
    excluded: tuple[SequenceExclusion, ...]

    @property
    def counts_by_horizon(self) -> dict[str, int]:
        return {
            str(horizon): sum(row.horizon_seconds == horizon for row in self.samples)
            for horizon in DEFAULT_HORIZONS_SECONDS
        }


@dataclass(frozen=True, slots=True)
class SequenceReadinessDecision:
    sequence_status: SequenceReadinessStatus
    microstructure_snapshot_status: str
    microstructure_delta_status: str
    reasons: tuple[str, ...]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_sequence_id(
    *,
    dataset_id: str,
    event_id: str,
    resolution: str,
    length: int,
    horizon_seconds: int,
    decision_timestamp: datetime,
) -> str:
    """Return an immutable identity derived only from causal sample metadata."""

    return (
        "seq-"
        + _hash_payload(
            {
                "schema_version": SEQUENCE_SCHEMA_VERSION,
                "dataset_id": dataset_id,
                "event_id": event_id,
                "resolution": resolution,
                "length": length,
                "horizon_seconds": horizon_seconds,
                "decision_timestamp": _iso(decision_timestamp),
            }
        )[:32]
    )


def build_sequence_samples(
    events: Iterable[SequenceEvent],
    observations: Iterable[SequenceObservation],
    *,
    dataset_id: str = HIST003_DATASET_ID,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS_SECONDS,
    lengths: tuple[int, ...] = DEFAULT_SEQUENCE_LENGTHS,
    cadence_seconds: int | None = 60,
) -> SequenceBuildResult:
    """Build exact-target, event-local sequences from already parsed observations."""

    if not dataset_id or any(h <= 0 for h in horizons) or any(length <= 0 for length in lengths):
        raise ValueError("sequence configuration is invalid")
    event_map = {event.event_id: event for event in events}
    grouped: dict[str, list[SequenceObservation]] = defaultdict(list)
    exclusions: list[SequenceExclusion] = []
    for observation in observations:
        event = event_map.get(observation.event_id)
        if event is None:
            exclusions.append(SequenceExclusion(observation.event_id, None, 0, 0, "UNKNOWN_EVENT"))
            continue
        if observation.timestamp < event.open_time or observation.timestamp > event.close_time:
            exclusions.append(
                SequenceExclusion(
                    observation.event_id, observation.timestamp, 0, 0, "OUTSIDE_EVENT"
                )
            )
            continue
        if observation.source_timestamp > observation.timestamp:
            exclusions.append(
                SequenceExclusion(
                    observation.event_id,
                    observation.timestamp,
                    0,
                    0,
                    "SOURCE_AFTER_DECISION",
                )
            )
            continue
        if (
            observation.received_timestamp is not None
            and observation.received_timestamp > observation.timestamp
        ):
            exclusions.append(
                SequenceExclusion(
                    observation.event_id,
                    observation.timestamp,
                    0,
                    0,
                    "RECEIVED_AFTER_DECISION",
                )
            )
            continue
        grouped[observation.event_id].append(observation)

    samples: list[SequenceSample] = []
    for event_id, values in grouped.items():
        event = event_map[event_id]
        ordered = sorted(values, key=lambda item: item.timestamp)
        by_time = {item.timestamp: item for item in ordered}
        for index, decision_observation in enumerate(ordered):
            decision = decision_observation.timestamp
            for length in lengths:
                history = ordered[max(0, index - length + 1) : index + 1]
                if len(history) != length:
                    for horizon in horizons:
                        exclusions.append(
                            SequenceExclusion(
                                event_id, decision, horizon, length, "INSUFFICIENT_LOOKBACK"
                            )
                        )
                    continue
                if cadence_seconds is not None and any(
                    (right.timestamp - left.timestamp).total_seconds() != cadence_seconds
                    for left, right in pairwise(history)
                ):
                    for horizon in horizons:
                        exclusions.append(
                            SequenceExclusion(
                                event_id, decision, horizon, length, "NON_CONTIGUOUS_LOOKBACK"
                            )
                        )
                    continue
                for horizon in horizons:
                    target_time = decision + timedelta(seconds=horizon)
                    target = by_time.get(target_time)
                    if target is None:
                        exclusions.append(
                            SequenceExclusion(
                                event_id, decision, horizon, length, "TARGET_NOT_EXACT"
                            )
                        )
                        continue
                    if target_time > event.close_time:
                        exclusions.append(
                            SequenceExclusion(
                                event_id, decision, horizon, length, "TARGET_OUTSIDE_EVENT"
                            )
                        )
                        continue
                    sample_id = make_sequence_id(
                        dataset_id=dataset_id,
                        event_id=event_id,
                        resolution=decision_observation.resolution,
                        length=length,
                        horizon_seconds=horizon,
                        decision_timestamp=decision,
                    )
                    samples.append(
                        SequenceSample(
                            sample_id=sample_id,
                            dataset_id=dataset_id,
                            event_id=event_id,
                            asset=event.asset,
                            resolution=decision_observation.resolution,
                            length=length,
                            horizon_seconds=horizon,
                            decision_timestamp=decision,
                            sequence_start=history[0].timestamp,
                            sequence_end=decision,
                            target_timestamp=target.timestamp,
                            source_timestamps=tuple(item.source_timestamp for item in history),
                        )
                    )
    samples.sort(key=lambda item: item.sample_id)
    exclusions.sort(
        key=lambda item: (
            item.event_id,
            item.decision_timestamp or datetime.min.replace(tzinfo=UTC),
            item.length,
            item.horizon_seconds,
            item.reason,
        )
    )
    return SequenceBuildResult(tuple(samples), tuple(exclusions))


def classify_sequence_readiness(
    *,
    independent_utc_days: int,
    independent_events: int,
    sequence_count: int,
    candle_sequence_count: int,
    trade_sequence_count: int,
    snapshot_count: int,
    delta_count: int,
    holdout_accessed: bool,
) -> SequenceReadinessDecision:
    """Classify evidence without turning readiness into a model-promotion decision."""

    if holdout_accessed:
        raise ValueError("holdout access is forbidden")
    reasons: list[str] = []
    if independent_utc_days < 30 or independent_events < 1000:
        reasons.append("HIST003_EVIDENCE_BELOW_SEQUENCE_GATE")
    if not sequence_count:
        reasons.append("NO_CAUSAL_SEQUENCE_SAMPLES")
    if candle_sequence_count and not trade_sequence_count:
        reasons.append("TRADE_DERIVED_SUBMINUTE_REPRESENTATION_UNAVAILABLE")
    if candle_sequence_count and any(horizon < 60 for horizon in DEFAULT_HORIZONS_SECONDS):
        reasons.append("ONE_MINUTE_CADENCE_CANNOT_PROVE_30S_TARGET")
    status = (
        SequenceReadinessStatus.BLOCKED
        if not sequence_count
        else SequenceReadinessStatus.PARTIAL
        if reasons
        else SequenceReadinessStatus.READY
    )
    return SequenceReadinessDecision(
        sequence_status=status,
        microstructure_snapshot_status=(
            "MICROSTRUCTURE_SNAPSHOT_READY_FOR_BOUNDED_BASELINE"
            if snapshot_count
            else "MICROSTRUCTURE_SNAPSHOT_NOT_MATERIALIZED"
        ),
        microstructure_delta_status=(
            "MICROSTRUCTURE_DELTA_READY_FOR_BOUNDED_BASELINE"
            if delta_count
            else "MICROSTRUCTURE_DELTA_BLOCKED"
        ),
        reasons=tuple(sorted(set(reasons))),
    )


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("historical timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def build_hist003_readiness_report(
    database_path: Path,
    *,
    dataset_id: str = HIST003_DATASET_ID,
) -> dict[str, object]:
    """Read the ignored HIST-003 SQLite store and emit a deterministic readiness summary."""

    uri = f"file:{database_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        markets = connection.execute(
            "SELECT ticker,event_ticker,asset,open_time,close_time FROM markets "
            "WHERE provider='kalshi_official' ORDER BY ticker"
        ).fetchall()
        candle_rows = connection.execute(
            "SELECT ticker,end_period_ts,raw_json FROM candles "
            "WHERE provider='kalshi_official' AND interval_seconds=60 ORDER BY ticker,end_period_ts"
        ).fetchall()
        trade_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM trades WHERE provider='kalshi_official'"
            ).fetchone()[0]
        )

    events = tuple(
        SequenceEvent(ticker, asset, _parse_time(open_time), _parse_time(close_time))
        for ticker, _event_ticker, asset, open_time, close_time in markets
    )
    candles_by_ticker: dict[str, list[SequenceObservation]] = defaultdict(list)
    for ticker, end_period_ts, raw_json in candle_rows:
        payload = json.loads(raw_json)
        price = payload.get("price", {})
        value = price.get("close") if isinstance(price, dict) else None
        if value is None:
            continue
        timestamp = datetime.fromtimestamp(int(end_period_ts), tz=UTC)
        candles_by_ticker[ticker].append(
            SequenceObservation(ticker, timestamp, timestamp, timestamp, float(value), "1m_candle")
        )

    candle_result = build_sequence_samples(
        events,
        (item for values in candles_by_ticker.values() for item in values),
        dataset_id=dataset_id,
    )
    # Official trades remain available as provenance and target-support evidence, but the
    # irregular event stream is not silently converted into a fixed sequence representation.
    # A future bounded trade representation must declare its aggregation and completeness
    # contract first; until then it is explicitly unavailable rather than fabricated.
    trade_result = SequenceBuildResult((), ())
    candle_days = {sample.decision_timestamp.date().isoformat() for sample in candle_result.samples}
    trade_days = {sample.decision_timestamp.date().isoformat() for sample in trade_result.samples}
    sequence_days = len(candle_days | trade_days)
    decision = classify_sequence_readiness(
        independent_utc_days=sequence_days,
        independent_events=len(events),
        sequence_count=len(candle_result.samples) + len(trade_result.samples),
        candle_sequence_count=len(candle_result.samples),
        trade_sequence_count=len(trade_result.samples),
        snapshot_count=0,
        delta_count=0,
        holdout_accessed=False,
    )
    excluded = candle_result.excluded + trade_result.excluded
    report = {
        "schema_version": SEQUENCE_SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "dataset_name": "HistoricalResearchDataset",
        "source_database": "data/research/hist003/hist003_official.sqlite3",
        "sequence_lengths": list(DEFAULT_SEQUENCE_LENGTHS),
        "horizons_seconds": list(DEFAULT_HORIZONS_SECONDS),
        "resolutions": ["1m_candle", "trade_event"],
        "normalization": "train_fold_only",
        "purge_embargo_seconds": PURGE_EMBARGO_SECONDS,
        "event_local": True,
        "no_future_nearest": True,
        "no_interpolation_or_fill": True,
        "counts": {
            "markets": len(markets),
            "candles": len(candle_rows),
            "trades": trade_count,
            "candle_sequences": len(candle_result.samples),
            "trade_sequences": len(trade_result.samples),
            "excluded_candidates": len(excluded),
        },
        "horizon_counts": {
            "1m_candle": candle_result.counts_by_horizon,
            "trade_event": {
                str(horizon): sum(row.horizon_seconds == horizon for row in trade_result.samples)
                for horizon in DEFAULT_HORIZONS_SECONDS
            },
        },
        "independent_utc_days": sequence_days,
        "assets": sorted({event.asset for event in events}),
        "per_asset": {
            asset: {
                "candle_sequences": sum(row.asset == asset for row in candle_result.samples),
                "trade_sequences": sum(row.asset == asset for row in trade_result.samples),
            }
            for asset in sorted({event.asset for event in events})
        },
        "exclusion_reasons": dict(sorted(Counter(item.reason for item in excluded).items())),
        "readiness": {
            "sequence_status": decision.sequence_status.value,
            "microstructure_snapshot_status": decision.microstructure_snapshot_status,
            "microstructure_delta_status": decision.microstructure_delta_status,
            "reasons": list(decision.reasons),
        },
        "commodity_status": "HISTORICAL_COMMODITY_SEQUENCE_UNAVAILABLE_IN_CURRENT_HIST003_ARTIFACT",
        "dataset_v2_touched": False,
        "holdout_accessed": False,
        "model_training": False,
    }
    report["readiness_id"] = "sequence-readiness-" + _hash_payload(report)[:24]
    return report
