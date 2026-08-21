"""Repeatable, snapshot-consistent model-training data readiness audit."""

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path

from live15_quant.config import Settings
from live15_quant.dataset import DatasetBuildConfig, DatasetBuilder, FeatureStore
from live15_quant.feature_registry import FEATURE_REGISTRY, FeatureFamily
from live15_quant.features import COINBASE_PRODUCT_BY_ASSET, SamplingPolicy
from live15_quant.models import Asset
from live15_quant.providers.pyth import PYTH_FEEDS
from live15_quant.storage import RecorderStore


class ReadinessStatus(StrEnum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    SOURCE_MISSING = "SOURCE_MISSING"
    QUALITY_WARNING = "QUALITY_WARNING"


@dataclass(frozen=True, slots=True)
class SourceQuality:
    observations: int
    source_receive_latency_median_ms: float | None
    source_receive_latency_p95_ms: float | None
    gap_median_seconds: float | None
    gap_p95_seconds: float | None
    gap_max_seconds: float | None
    duplicate_rate: float | None
    out_of_order_observations: int
    negative_latency_observations: int
    severe_clock_skew_observations: int
    gaps_over_15_seconds: int
    stale_duration_seconds: float


@dataclass(frozen=True, slots=True)
class SourceWindowCoverage:
    observations: int
    coverage_percent: float
    stale_free_coverage_percent: float
    max_continuous_gap_seconds: float
    first_received_timestamp: str | None
    last_received_timestamp: str | None


_OBSERVABILITY_WINDOWS = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}


def _windowed_coverage(
    timestamps: Iterator[datetime],
    *,
    snapshot_at: datetime,
    bucket_seconds: float,
    stale_seconds: float,
) -> dict[str, SourceWindowCoverage]:
    if bucket_seconds <= 0 or stale_seconds <= 0:
        raise ValueError("coverage bucket and stale threshold must be positive")
    states = {
        label: {
            "start": snapshot_at - duration,
            "first": None,
            "last": None,
            "previous": None,
            "observations": 0,
            "buckets": set(),
            "stale_free_seconds": 0.0,
            "max_internal_gap": 0.0,
        }
        for label, duration in _OBSERVABILITY_WINDOWS.items()
    }
    earliest = snapshot_at - max(_OBSERVABILITY_WINDOWS.values())
    for received in timestamps:
        received = received.astimezone(UTC)
        if received < earliest or received > snapshot_at:
            continue
        for state in states.values():
            start = state["start"]
            assert isinstance(start, datetime)
            if received < start:
                continue
            previous = state["previous"]
            if isinstance(previous, datetime):
                gap = max(0.0, (received - previous).total_seconds())
                state["stale_free_seconds"] = float(state["stale_free_seconds"]) + min(
                    gap, stale_seconds
                )
                state["max_internal_gap"] = max(float(state["max_internal_gap"]), gap)
            else:
                state["first"] = received
            state["previous"] = received
            state["last"] = received
            state["observations"] = int(state["observations"]) + 1
            bucket = int((received - start).total_seconds() // bucket_seconds)
            buckets = state["buckets"]
            assert isinstance(buckets, set)
            buckets.add(bucket)

    result: dict[str, SourceWindowCoverage] = {}
    for label, duration in _OBSERVABILITY_WINDOWS.items():
        state = states[label]
        start = state["start"]
        first = state["first"]
        last = state["last"]
        assert isinstance(start, datetime)
        window_seconds = duration.total_seconds()
        total_buckets = max(1, math.ceil(window_seconds / bucket_seconds))
        buckets = state["buckets"]
        assert isinstance(buckets, set)
        if isinstance(last, datetime):
            tail = max(0.0, (snapshot_at - last).total_seconds())
            stale_free = float(state["stale_free_seconds"]) + min(tail, stale_seconds)
            boundary_start = (
                max(0.0, (first - start).total_seconds())
                if isinstance(first, datetime)
                else window_seconds
            )
            max_gap = max(boundary_start, float(state["max_internal_gap"]), tail)
        else:
            stale_free = 0.0
            max_gap = window_seconds
        result[label] = SourceWindowCoverage(
            observations=int(state["observations"]),
            coverage_percent=round(min(100.0, len(buckets) / total_buckets * 100), 6),
            stale_free_coverage_percent=round(min(100.0, stale_free / window_seconds * 100), 6),
            max_continuous_gap_seconds=max_gap,
            first_received_timestamp=(first.isoformat() if isinstance(first, datetime) else None),
            last_received_timestamp=(last.isoformat() if isinstance(last, datetime) else None),
        )
    return result


def build_source_observability(
    connection: sqlite3.Connection,
    settings: Settings,
    *,
    snapshot_at: datetime,
) -> dict[str, object]:
    """Compute bounded-window metrics on a read-only snapshot, never the active DB."""

    specs: list[tuple[str, str, str, tuple[object, ...], float, float]] = []
    for asset in Asset:
        specs.append(
            (
                f"kalshi_quote:{asset.value}",
                "kalshi_prediction_quotes",
                "asset=?",
                (asset.value,),
                max(1.0, settings.official_quote_poll_interval_seconds),
                settings.official_quote_max_source_age_seconds,
            )
        )
    for asset, product in COINBASE_PRODUCT_BY_ASSET.items():
        specs.append(
            (
                f"coinbase:{asset.value}",
                "coinbase_ticks",
                "product=?",
                (product,),
                5.0,
                settings.recorder_coinbase_stale_seconds,
            )
        )
    for asset in PYTH_FEEDS:
        specs.append(
            (
                f"pyth:{asset.value}",
                "underlying_observations",
                "asset=? AND provider=?",
                (asset.value, "pyth_hermes"),
                5.0,
                settings.recorder_pyth_stale_seconds,
            )
        )
    for asset, provider in (
        (Asset.BNB, "binance_spot"),
        (Asset.HYPE, "hyperliquid_perp"),
    ):
        specs.append(
            (
                f"secondary:{asset.value}",
                "secondary_underlying_observations",
                "asset=? AND provider=?",
                (asset.value, provider),
                5.0,
                settings.recorder_secondary_stale_seconds,
            )
        )

    earliest = snapshot_at - max(_OBSERVABILITY_WINDOWS.values())
    report: dict[str, object] = {}
    for name, table, predicate, parameters, bucket, stale in specs:
        rows = connection.execute(
            f"SELECT received_timestamp FROM {table} WHERE {predicate} "
            "AND received_timestamp>=? AND received_timestamp<=? "
            "ORDER BY received_timestamp,id",
            (*parameters, earliest.isoformat(), snapshot_at.isoformat()),
        )
        windows = _windowed_coverage(
            (_parse(str(row["received_timestamp"])) for row in rows),
            snapshot_at=snapshot_at,
            bucket_seconds=bucket,
            stale_seconds=stale,
        )
        report[name] = {
            "bucket_seconds": bucket,
            "stale_threshold_seconds": stale,
            "windows": {label: asdict(value) for label, value in windows.items()},
        }
    return report


class SnapshotTimeoutError(TimeoutError):
    """A throttled active-database snapshot exceeded its absolute budget."""


def snapshot_database(
    source: Path,
    destination: Path,
    *,
    max_seconds: float = 120.0,
    pages_per_step: int = 2048,
    throttle_seconds: float = 0.002,
) -> None:
    """Take a throttled, deadline-bounded, transactionally consistent backup."""

    if source.resolve() == destination.resolve():
        raise ValueError("readiness snapshot destination must differ from raw truth")
    if max_seconds <= 0 or pages_per_step <= 0 or throttle_seconds < 0:
        raise ValueError("snapshot budget, page batch, and throttle must be bounded")
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    destination.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max_seconds

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline:
            raise SnapshotTimeoutError(
                f"read-only SQLite snapshot exceeded {max_seconds:g} seconds"
            )
        if throttle_seconds:
            time.sleep(throttle_seconds)

    reader: sqlite3.Connection | None = None
    writer: sqlite3.Connection | None = None
    try:
        try:
            reader = sqlite3.connect(source_uri, uri=True)
            writer = sqlite3.connect(destination)
            reader.execute("PRAGMA query_only=ON")
            reader.execute("PRAGMA busy_timeout=2000")
            # Pin one WAL snapshot before the incremental backup starts. Without
            # this explicit read transaction, a continuously-writing source can
            # keep moving the backup target and exhaust the absolute deadline.
            reader.execute("BEGIN")
            reader.execute("SELECT rootpage FROM sqlite_schema LIMIT 1").fetchone()
            reader.backup(writer, pages=pages_per_step, progress=progress, sleep=0.05)
        finally:
            if writer is not None:
                writer.close()
            if reader is not None:
                reader.close()
    except Exception:
        for path in (
            destination,
            destination.with_name(f"{destination.name}-wal"),
            destination.with_name(f"{destination.name}-shm"),
        ):
            path.unlink(missing_ok=True)
        raise


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _quality(rows: list[tuple[datetime, datetime, str]]) -> SourceQuality:
    ordered = sorted(rows, key=lambda item: item[1])
    latencies = [(received - source).total_seconds() * 1000 for source, received, _ in ordered]
    gaps = [(current[1] - previous[1]).total_seconds() for previous, current in pairwise(ordered)]
    fingerprints = Counter(item[2] for item in ordered)
    duplicates = sum(count - 1 for count in fingerprints.values())
    out_of_order = sum(current[0] < previous[0] for previous, current in pairwise(ordered))
    return SourceQuality(
        observations=len(ordered),
        source_receive_latency_median_ms=_percentile(latencies, 0.5),
        source_receive_latency_p95_ms=_percentile(latencies, 0.95),
        gap_median_seconds=_percentile(gaps, 0.5),
        gap_p95_seconds=_percentile(gaps, 0.95),
        gap_max_seconds=max(gaps) if gaps else None,
        duplicate_rate=(duplicates / len(ordered)) if ordered else None,
        out_of_order_observations=out_of_order,
        negative_latency_observations=sum(value < 0 for value in latencies),
        severe_clock_skew_observations=sum(value < -1000 for value in latencies),
        gaps_over_15_seconds=sum(value > 15 for value in gaps),
        stale_duration_seconds=sum(max(0.0, value - 15.0) for value in gaps),
    )


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("database timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def build_readiness_report(settings: Settings) -> dict[str, object]:
    """Snapshot raw truth, migrate only the copy, and build a temporary feature store."""

    snapshot_at = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="live15-readiness-") as directory:
        root = Path(directory)
        raw_snapshot = root / "raw.sqlite3"
        feature_store = root / "features.sqlite3"
        snapshot_database(settings.recorder_data_path, raw_snapshot)
        policy = SamplingPolicy(
            tuple(timedelta(seconds=value) for value in settings.dataset_decision_offsets_seconds),
            quote_max_age=timedelta(seconds=settings.dataset_quote_max_age_seconds),
            underlying_max_age=timedelta(seconds=settings.dataset_underlying_max_age_seconds),
        )
        with RecorderStore(raw_snapshot) as source, FeatureStore(feature_store) as destination:
            summary = DatasetBuilder(source, destination).build(DatasetBuildConfig(policy))
            finalized_by_asset = source.settlement_counts_by_asset()
            trainable_by_asset = destination.coverage_by_asset(summary.build_id)
            underlying_feature_rows = Counter(
                row.asset
                for row in destination.replay(summary.build_id)
                if row.features.by_name()["underlying_price"].value is not None
            )
            integrity = source.integrity_check()
        connection = sqlite3.connect(raw_snapshot)
        connection.row_factory = sqlite3.Row
        try:
            quality_by_asset = _source_quality_by_asset(connection)
            quote_quality_by_asset = _quote_quality_by_asset(connection)
            live_source_ready = _live_source_ready_by_asset(
                connection,
                snapshot_at=snapshot_at,
                max_age_seconds=settings.dataset_underlying_max_age_seconds,
            )
            quote_counts = {
                Asset(row["asset"]): int(row["count"])
                for row in connection.execute(
                    "SELECT asset,COUNT(*) AS count FROM kalshi_prediction_quotes GROUP BY asset"
                )
            }
            source_observability = build_source_observability(
                connection, settings, snapshot_at=snapshot_at
            )
        finally:
            connection.close()

    diagnostics = summary.diagnostics
    asset_rows = diagnostics.get("rows_per_asset", {})
    label_balance = diagnostics.get("label_balance", {})
    assets: dict[str, object] = {}
    for asset in Asset:
        provider = (
            "Coinbase Exchange WebSocket" if asset in COINBASE_PRODUCT_BY_ASSET else "Pyth Hermes"
        )
        empty_quality = SourceQuality(0, None, None, None, None, None, None, 0, 0, 0, 0, 0.0)
        source_quality = quality_by_asset.get(asset, empty_quality)
        underlying_count = source_quality.observations
        finalized = finalized_by_asset[asset]
        trainable, rows = trainable_by_asset[asset]
        historical_underlying_rows = underlying_feature_rows[asset]
        historical_underlying_coverage = historical_underlying_rows / rows if rows else None
        status = _readiness_status(
            quality=source_quality,
            live_ready=live_source_ready[asset],
            finalized=finalized,
            trainable=trainable,
            training_rows=rows,
            historical_underlying_rows=historical_underlying_rows,
        )
        assets[asset.value] = {
            "status": status.value,
            "kalshi_quote_observations": quote_counts.get(asset, 0),
            "underlying_provider": provider,
            "underlying_symbol": (
                COINBASE_PRODUCT_BY_ASSET[asset]
                if asset in COINBASE_PRODUCT_BY_ASSET
                else PYTH_FEEDS[asset][0]
            ),
            "underlying_observations": underlying_count,
            "historical_underlying_feature_rows": historical_underlying_rows,
            "historical_underlying_feature_coverage": historical_underlying_coverage,
            "live_underlying_source_ready": live_source_ready[asset],
            "source_quality": asdict(source_quality),
            "kalshi_quote_quality": asdict(quote_quality_by_asset.get(asset, empty_quality)),
            "finalized_events": finalized,
            "trainable_events": trainable,
            "training_rows": rows,
            "label_balance": diagnostics.get("label_balance_by_asset", {}).get(
                asset.value, {"yes": 0, "no": 0}
            ),
            "decision_bucket_coverage": diagnostics.get(
                "rows_per_decision_bucket_by_asset", {}
            ).get(asset.value, {}),
            "active_quality_issues": _quality_issues(source_quality)
            + ([] if live_source_ready[asset] else ["live underlying is unavailable or stale"])
            + [
                f"Kalshi quote: {issue}"
                for issue in _quality_issues(quote_quality_by_asset.get(asset, empty_quality))
            ],
        }
    missing_rates = diagnostics.get("missing_feature_rates", {})
    stale_rates = diagnostics.get("stale_feature_rates", {})
    feature_readiness = [
        {
            "name": item.name,
            "family": item.family.value,
            "required_source": _required_source(item.family),
            "unit": item.unit,
            "formula": item.formula,
            "lookback_seconds": item.lookback_seconds,
            "missing_policy": item.missing_policy.value,
            "timestamp_semantics": item.timestamp_semantics.value,
            "available_live": all(live_source_ready[asset] for asset in Asset)
            if _uses_underlying(item.name, item.family)
            else True,
            "available_live_by_asset": {
                asset.value: (
                    live_source_ready[asset] if _uses_underlying(item.name, item.family) else True
                )
                for asset in Asset
            },
            "cross_asset_applicability": "all_10_when_primary_underlying_available",
            "leakage_safe": True,
            "missing_rate": missing_rates.get(item.name),
            "stale_rate": stale_rates.get(item.name),
        }
        for item in FEATURE_REGISTRY
    ]
    return {
        "report_version": "1.0.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "snapshot_integrity": integrity,
        "source_observability": source_observability,
        "assets": assets,
        "dataset": {
            "build_id": summary.build_id,
            "evaluated_finalized_events": diagnostics.get("evaluated_finalized_events", 0),
            "trainable_events": summary.events,
            "training_rows": summary.rows,
            "label_balance": label_balance,
            "rows_per_asset": asset_rows,
            "decision_bucket_coverage": diagnostics.get("rows_per_decision_bucket_seconds", {}),
            "missing_feature_rates": missing_rates,
            "stale_feature_rates": stale_rates,
            "rejection_reasons": diagnostics.get("trainability_rejections", {}),
            "missing_reason_counts": diagnostics.get("missing_reason_counts", {}),
            "missing_reason_counts_by_asset": diagnostics.get("missing_reason_counts_by_asset", {}),
        },
        "feature_count": len(feature_readiness),
        "features": feature_readiness,
        "feature_audit": {
            "features_with_any_historical_value": sorted(
                name for name, rate in missing_rates.items() if rate != "1"
            ),
            "features_complete_for_all_historical_rows": sorted(
                name for name, rate in missing_rates.items() if rate == "0"
            ),
            "potentially_redundant_groups": [
                ["yes_bid", "market_probability_lower"],
                ["yes_ask", "market_probability_upper"],
                ["yes_midpoint", "market_probability_midpoint"],
                ["yes_spread", "market_probability_width"],
                [
                    "absolute_distance_to_target",
                    "signed_distance_to_target",
                    "normalized_distance_to_target",
                ],
            ],
            "always_missing_features": sorted(
                name for name, rate in missing_rates.items() if rate == "1"
            ),
            "only_offline_features": [],
            "live_unreproducible_features": [],
            "removal_policy": "report_only_no_features_removed",
        },
        "clock_safety": {
            "asof_requires_source_and_receive_not_after_decision": True,
            "negative_latency_is_reported_not_repaired": True,
            "future_timestamps_are_never_backdated": True,
        },
    }


def _required_source(family: FeatureFamily) -> str:
    if family in {FeatureFamily.UNDERLYING_RETURN, FeatureFamily.VOLATILITY}:
        return "primary predictive underlying"
    if family is FeatureFamily.CONTRACT_GEOMETRY:
        return "Kalshi contract metadata + primary predictive underlying"
    return "official Kalshi quote/orderbook"


def _readiness_status(
    *,
    quality: SourceQuality,
    live_ready: bool,
    finalized: int,
    trainable: int,
    training_rows: int,
    historical_underlying_rows: int,
) -> ReadinessStatus:
    """Keep live source availability distinct from historical feature completeness."""

    if quality.observations == 0:
        return ReadinessStatus.SOURCE_MISSING
    if finalized == 0 or trainable == 0:
        return ReadinessStatus.INSUFFICIENT_DATA
    if (
        not live_ready
        or quality.severe_clock_skew_observations
        or (quality.gap_max_seconds is not None and quality.gap_max_seconds > 60)
    ):
        return ReadinessStatus.QUALITY_WARNING
    if historical_underlying_rows < training_rows:
        return ReadinessStatus.PARTIAL
    return ReadinessStatus.READY


def _live_source_ready_by_asset(
    connection: sqlite3.Connection,
    *,
    snapshot_at: datetime,
    max_age_seconds: float,
) -> dict[Asset, bool]:
    """Report live inference availability at snapshot time, not historical existence."""

    ready = {asset: False for asset in Asset}
    cutoff = snapshot_at - timedelta(seconds=max_age_seconds)
    product_assets = {product: asset for asset, product in COINBASE_PRODUCT_BY_ASSET.items()}
    for product, asset in product_assets.items():
        row = connection.execute(
            """
            SELECT exchange_timestamp,received_timestamp FROM coinbase_ticks
            WHERE product=? AND received_timestamp<=?
              AND (exchange_timestamp IS NULL OR exchange_timestamp<=?)
            ORDER BY received_timestamp DESC,id DESC LIMIT 1
            """,
            (product, snapshot_at.isoformat(), snapshot_at.isoformat()),
        ).fetchone()
        if row is None:
            continue
        received = _parse(row["received_timestamp"])
        source = _parse(row["exchange_timestamp"]) if row["exchange_timestamp"] else received
        ready[asset] = received >= cutoff and source >= cutoff
    for asset in PYTH_FEEDS:
        row = connection.execute(
            """
            SELECT source_timestamp,received_timestamp,freshness
            FROM underlying_observations
            WHERE asset=? AND provider=? AND received_timestamp<=? AND source_timestamp<=?
            ORDER BY received_timestamp DESC,id DESC LIMIT 1
            """,
            (
                asset.value,
                "pyth_hermes",
                snapshot_at.isoformat(),
                snapshot_at.isoformat(),
            ),
        ).fetchone()
        if row is None:
            continue
        ready[asset] = (
            row["freshness"] == "fresh"
            and _parse(row["received_timestamp"]) >= cutoff
            and _parse(row["source_timestamp"]) >= cutoff
        )
    return ready


def _uses_underlying(name: str, family: FeatureFamily) -> bool:
    return family in {FeatureFamily.UNDERLYING_RETURN, FeatureFamily.VOLATILITY} or name in {
        "underlying_price",
        "absolute_distance_to_target",
        "signed_distance_to_target",
        "normalized_distance_to_target",
        "distance_volatility_ratio",
    }


def _source_quality_by_asset(connection: sqlite3.Connection) -> dict[Asset, SourceQuality]:
    grouped: dict[Asset, list[tuple[datetime, datetime, str]]] = defaultdict(list)
    product_assets = {product: asset for asset, product in COINBASE_PRODUCT_BY_ASSET.items()}
    for row in connection.execute(
        "SELECT product,exchange_timestamp,received_timestamp,price,bid,ask,"
        "bid_size,ask_size,last_size,volume_24h FROM coinbase_ticks"
    ):
        asset = product_assets.get(row["product"])
        if asset is None:
            continue
        received = _parse(row["received_timestamp"])
        source = _parse(row["exchange_timestamp"]) if row["exchange_timestamp"] else received
        fingerprint = ":".join(
            str(row[field])
            for field in (
                "exchange_timestamp",
                "price",
                "bid",
                "ask",
                "bid_size",
                "ask_size",
                "last_size",
                "volume_24h",
            )
        )
        grouped[asset].append((source, received, fingerprint))
    for row in connection.execute(
        "SELECT asset,source_timestamp,received_timestamp,price,confidence "
        "FROM underlying_observations"
    ):
        grouped[Asset(row["asset"])].append(
            (
                _parse(row["source_timestamp"]),
                _parse(row["received_timestamp"]),
                f"{row['source_timestamp']}:{row['price']}:{row['confidence']}",
            )
        )
    return {asset: _quality(rows) for asset, rows in grouped.items()}


def _quote_quality_by_asset(connection: sqlite3.Connection) -> dict[Asset, SourceQuality]:
    grouped: dict[Asset, list[tuple[datetime, datetime, str]]] = defaultdict(list)
    for row in connection.execute(
        "SELECT asset,source_timestamp,received_timestamp,content_hash "
        "FROM kalshi_prediction_quotes"
    ):
        received = _parse(row["received_timestamp"])
        source = _parse(row["source_timestamp"]) if row["source_timestamp"] else received
        grouped[Asset(row["asset"])].append(
            (source, received, f"{row['source_timestamp']}:{row['content_hash']}")
        )
    return {asset: _quality(rows) for asset, rows in grouped.items()}


def _quality_issues(quality: SourceQuality) -> list[str]:
    if quality.observations == 0:
        return ["source has no recorded observations"]
    issues = []
    if quality.gap_max_seconds is not None and quality.gap_max_seconds > 60:
        issues.append("historical underlying gap exceeded 60 seconds")
    if quality.severe_clock_skew_observations:
        issues.append("source timestamp exceeded local receive time by more than one second")
    return issues


def write_report_atomic(report: dict[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
