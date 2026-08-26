"""Causal trade-derived sequence evidence and bounded L2 readiness contracts.

This module materializes research evidence only.  It never writes Dataset v2, reads a holdout,
trains a model, or treats trades as an order book.  Missing buckets and future targets remain
explicitly typed rather than being filled or interpolated.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

PATH_PARTIAL = "SEQUENCE_PARTIAL_MORE_DATA_OR_REPRESENTATION_NEEDED"
PATH_READY = "SEQUENCE_READY_FOR_BOUNDED_MODEL_TRAINING"
PATH_BLOCKED = "SEQUENCE_BLOCKED_DATA_INSUFFICIENT"
H1_PROVENANCE = "H1_KALSHI_OFFICIAL_HISTORY"
H2_PROVENANCE = "H2_DEPTHFEED_RECORDED_L2"
MAX_DEPTHFEED_DAYS = 7


class SequenceEvidenceError(ValueError):
    """A sequence input or readiness contract is invalid."""


class DepthReadiness:
    READY = "MICROSTRUCTURE_SNAPSHOT_READY_FOR_BOUNDED_BASELINE"
    PARTIAL = "MICROSTRUCTURE_SNAPSHOT_PARTIAL"
    BLOCKED = "MICROSTRUCTURE_SNAPSHOT_BLOCKED"


@dataclass(frozen=True, slots=True)
class SequenceConfig:
    grid_seconds: tuple[int, ...] = (5, 15, 30)
    lookback_seconds: int = 120
    target_horizons: tuple[int, ...] = (30, 60, 120, 180, 300)
    target_tolerance_seconds: int = 15

    def __post_init__(self) -> None:
        if not self.grid_seconds or any(item <= 0 for item in self.grid_seconds):
            raise SequenceEvidenceError("grid resolutions must be positive")
        if tuple(sorted(set(self.grid_seconds))) != self.grid_seconds:
            raise SequenceEvidenceError("grid resolutions must be sorted and unique")
        if self.lookback_seconds <= 0 or self.target_tolerance_seconds < 0:
            raise SequenceEvidenceError("lookback and target tolerance are invalid")
        if not self.target_horizons or any(item <= 0 for item in self.target_horizons):
            raise SequenceEvidenceError("target horizons must be positive")


@dataclass(frozen=True, slots=True)
class TradeObservation:
    ticker: str
    event_id: str
    asset: str
    event_start: datetime
    event_end: datetime
    timestamp: datetime
    trade_id: str
    price: Decimal
    quantity: Decimal
    taker_side: str | None

    def __post_init__(self) -> None:
        if not self.ticker or not self.event_id or not self.asset or not self.trade_id:
            raise SequenceEvidenceError("trade identity is incomplete")
        for value, name in (
            (self.event_start, "event_start"),
            (self.event_end, "event_end"),
            (self.timestamp, "timestamp"),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise SequenceEvidenceError(f"{name} must be timezone-aware")
        if self.event_start >= self.event_end:
            raise SequenceEvidenceError("event window is invalid")
        if not self.event_start <= self.timestamp <= self.event_end:
            raise SequenceEvidenceError("trade timestamp crosses event boundary")
        if self.quantity <= 0 or self.price < 0:
            raise SequenceEvidenceError("trade price or quantity is invalid")


def target_within_tolerance(target: datetime, observed: datetime, tolerance_seconds: int) -> bool:
    """Require a future observation at/after target and inside the declared tolerance."""

    return target <= observed <= target + timedelta(seconds=tolerance_seconds)


def bounded_depth_window(
    end: datetime, days: int = MAX_DEPTHFEED_DAYS
) -> tuple[datetime, datetime]:
    if days <= 0 or days > MAX_DEPTHFEED_DAYS:
        raise SequenceEvidenceError("DepthFeed window exceeds the seven-day research bound")
    end_utc = _utc(end)
    return end_utc - timedelta(days=days), end_utc


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _decimal(value: Decimal) -> str:
    return format(value, "f")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _direction(current: Decimal, future: Decimal) -> str:
    if future > current:
        return "UP"
    if future < current:
        return "DOWN"
    return "FLAT"


def _trade_bucket(trade: TradeObservation, grid_seconds: int) -> int:
    elapsed = (_utc(trade.timestamp) - _utc(trade.event_start)).total_seconds()
    return int(elapsed // grid_seconds)


def _aggregate_bucket(trades: list[TradeObservation], bucket_end: datetime) -> dict[str, object]:
    ordered = sorted(trades, key=lambda item: (item.timestamp, item.trade_id))
    total_quantity = sum((item.quantity for item in ordered), Decimal("0"))
    weighted = sum((item.price * item.quantity for item in ordered), Decimal("0"))
    signed = Decimal("0")
    signed_count = 0
    for item in ordered:
        side = (item.taker_side or "").casefold()
        if side in {"yes", "no"}:
            signed += item.quantity if side == "yes" else -item.quantity
            signed_count += 1
    last = ordered[-1]
    return {
        "bucket_end": _utc(bucket_end).isoformat(),
        "last_trade_timestamp": _utc(last.timestamp).isoformat(),
        "last_price": _decimal(last.price),
        "vwap": _decimal(weighted / total_quantity),
        "trade_count": len(ordered),
        "quantity": _decimal(total_quantity),
        "signed_flow": _decimal(signed) if signed_count == len(ordered) else None,
        "signed_flow_complete": signed_count == len(ordered),
        "source_provenance": H1_PROVENANCE,
    }


def _target(
    *,
    decision: datetime,
    current_price: Decimal,
    trades: list[TradeObservation],
    timestamps: list[datetime],
    horizon: int,
    tolerance: int,
    event_end: datetime,
) -> dict[str, object]:
    target_time = decision + timedelta(seconds=horizon)
    if target_time > event_end:
        return {"available": False, "missing_reason": "event_boundary"}
    index = bisect_left(timestamps, target_time)
    if (
        index >= len(trades)
        or timestamps[index] > event_end
        or not target_within_tolerance(target_time, timestamps[index], tolerance)
    ):
        return {"available": False, "missing_reason": "future_target_unavailable"}
    observed = trades[index]
    change = observed.price - current_price
    return {
        "available": True,
        "target_timestamp": _utc(observed.timestamp).isoformat(),
        "target_price": _decimal(observed.price),
        "price_change": _decimal(change),
        "direction": _direction(current_price, observed.price),
        "tolerance_seconds": tolerance,
        "source_provenance": H1_PROVENANCE,
    }


def _market_sequences(
    trades: list[TradeObservation], config: SequenceConfig
) -> tuple[list[dict[str, object]], Counter[str], Counter[str]]:
    ordered = sorted(trades, key=lambda item: (item.timestamp, item.trade_id))
    event_start = _utc(ordered[0].event_start)
    event_end = _utc(ordered[0].event_end)
    timestamps = [_utc(item.timestamp) for item in ordered]
    buckets_by_grid: dict[int, dict[int, list[TradeObservation]]] = {}
    for grid in config.grid_seconds:
        bucketed: dict[int, list[TradeObservation]] = defaultdict(list)
        for item in ordered:
            bucketed[_trade_bucket(item, grid)].append(item)
        buckets_by_grid[grid] = bucketed
    exclusions: Counter[str] = Counter()
    target_missing: Counter[str] = Counter()
    rows: list[dict[str, object]] = []
    for grid in config.grid_seconds:
        bucketed = buckets_by_grid[grid]
        lookback_buckets = max(1, (config.lookback_seconds + grid - 1) // grid)
        for bucket_index in sorted(bucketed):
            decision = event_start + timedelta(seconds=(bucket_index + 1) * grid)
            if decision > event_end:
                exclusions["event_boundary_crossed"] += 1
                continue
            required = range(bucket_index - lookback_buckets + 1, bucket_index + 1)
            if any(index not in bucketed for index in required):
                reason = (
                    "insufficient_history"
                    if bucket_index < lookback_buckets - 1
                    else "no_trade_in_source_bucket"
                )
                exclusions[reason] += 1
                continue
            features = [
                _aggregate_bucket(
                    bucketed[index], event_start + timedelta(seconds=(index + 1) * grid)
                )
                for index in required
            ]
            current_price = Decimal(str(features[-1]["last_price"]))
            targets = {
                str(horizon): _target(
                    decision=decision,
                    current_price=current_price,
                    trades=ordered,
                    timestamps=timestamps,
                    horizon=horizon,
                    tolerance=config.target_tolerance_seconds,
                    event_end=event_end,
                )
                for horizon in config.target_horizons
            }
            for horizon, target in targets.items():
                if not target["available"]:
                    target_missing[f"{horizon}:{target['missing_reason']}"] += 1
            identity = {
                "ticker": ordered[0].ticker,
                "event_id": ordered[0].event_id,
                "grid_seconds": grid,
                "decision_timestamp": decision.isoformat(),
            }
            row_id = hashlib.sha256(_canonical(identity).encode()).hexdigest()[:24]
            rows.append(
                {
                    "sequence_id": f"seq-{row_id}",
                    **identity,
                    "asset": ordered[0].asset,
                    "event_start": event_start.isoformat(),
                    "event_end": event_end.isoformat(),
                    "lookback_seconds": config.lookback_seconds,
                    "features": features,
                    "targets": targets,
                    "source_provenance": H1_PROVENANCE,
                }
            )
    return rows, exclusions, target_missing


def build_trade_sequences(
    trades: Iterable[TradeObservation], config: SequenceConfig
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Build deterministic event-local sequences without filling missing observations."""

    grouped: dict[str, list[TradeObservation]] = defaultdict(list)
    for trade in trades:
        grouped[trade.ticker].append(trade)
    rows: list[dict[str, object]] = []
    exclusions: Counter[str] = Counter()
    target_missing: Counter[str] = Counter()
    for ticker in sorted(grouped):
        market_rows, market_exclusions, market_missing = _market_sequences(grouped[ticker], config)
        rows.extend(market_rows)
        exclusions.update(market_exclusions)
        target_missing.update(market_missing)
    grid_summary: dict[str, dict[str, object]] = {}
    for grid in config.grid_seconds:
        selected = [row for row in rows if row["grid_seconds"] == grid]
        grid_summary[str(grid)] = {
            "sequence_count": len(selected),
            "independent_days": len({str(row["decision_timestamp"])[:10] for row in selected}),
            "independent_events": len({str(row["event_id"]) for row in selected}),
            "assets": sorted({str(row["asset"]) for row in selected}),
            "target_available": {
                str(horizon): sum(
                    bool(row["targets"][str(horizon)]["available"]) for row in selected
                )
                for horizon in config.target_horizons
            },
        }
    per_asset_sequence_counts = Counter(str(row["asset"]) for row in rows)
    per_day_sequence_counts = Counter(str(row["decision_timestamp"])[:10] for row in rows)
    summary: dict[str, object] = {
        "provenance": H1_PROVENANCE,
        "sequence_count": len(rows),
        "independent_days": len({str(row["decision_timestamp"])[:10] for row in rows}),
        "independent_events": len({str(row["event_id"]) for row in rows}),
        "assets": sorted({str(row["asset"]) for row in rows}),
        "per_asset_sequence_counts": dict(sorted(per_asset_sequence_counts.items())),
        "per_day_sequence_counts": dict(sorted(per_day_sequence_counts.items())),
        "grid_summary": grid_summary,
        "exclusions": dict(sorted(exclusions.items())),
        "target_missing": dict(sorted(target_missing.items())),
    }
    return rows, summary


def classify_path_readiness(report: dict[str, object]) -> str:
    count = int(report.get("sequence_count", 0))
    days = int(report.get("independent_days", 0))
    events = int(report.get("independent_events", 0))
    folds = int(report.get("fold_count", 0))
    if count == 0 or events == 0:
        return PATH_BLOCKED
    if days > 1 and events > 10 and folds >= 2:
        return PATH_READY
    return PATH_PARTIAL


def classify_depth_readiness(report: dict[str, object]) -> str:
    snapshots = int(report.get("snapshot_count", 0))
    days = int(report.get("independent_days", 0))
    assets = len(report.get("assets", []))
    events = int(report.get("events", 0))
    if snapshots <= 0:
        return DepthReadiness.BLOCKED
    if days >= 2 and assets >= 2 and events >= 2:
        return DepthReadiness.READY
    return DepthReadiness.PARTIAL


def tlob_eligibility(report: dict[str, object]) -> str:
    if int(report.get("snapshot_count", 0)) <= 0:
        return "TLOB_BLOCKED"
    if not bool(report.get("has_continuous_sequence", False)):
        return "TLOB_ADAPTER_OR_DATA_GAP"
    if report.get("provenance") != H2_PROVENANCE:
        return "TLOB_ADAPTER_OR_DATA_GAP"
    return "TLOB_BOUNDED_TRAINING_ELIGIBLE"


def materialize_sequence_manifest(
    *,
    rows: list[dict[str, object]],
    summary: dict[str, object],
    output_dir: Path,
    source_dataset_id: str,
    code_sha: str,
    config: SequenceConfig,
) -> dict[str, object]:
    """Write ignored normalized rows and a deterministic manifest; no raw source is copied."""

    output_dir.mkdir(parents=True, exist_ok=True)
    rows_path = output_dir / "trade_sequence_rows.jsonl"
    with rows_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_canonical(row) + "\n")
    content_hash = hashlib.sha256(rows_path.read_bytes()).hexdigest()
    manifest_payload = {
        "schema_version": "flow005b1-sequence-1.0.0",
        "source_dataset_id": source_dataset_id,
        "code_sha": code_sha,
        "config": {
            "grid_seconds": list(config.grid_seconds),
            "lookback_seconds": config.lookback_seconds,
            "target_horizons": list(config.target_horizons),
            "target_tolerance_seconds": config.target_tolerance_seconds,
        },
        "rows_content_hash": content_hash,
        "summary": summary,
        "event_isolation": True,
        "no_fill_or_interpolation": True,
        "source_provenance": H1_PROVENANCE,
        "dataset_v2_touched": False,
        "holdout_accessed": False,
        "model_training": False,
    }
    manifest_id = (
        "flow005b1-sequence-"
        + hashlib.sha256(_canonical(manifest_payload).encode()).hexdigest()[:24]
    )
    manifest = {"manifest_id": manifest_id, **manifest_payload}
    (output_dir / "trade_sequence_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest
