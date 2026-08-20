"""Versioned, restartable training-dataset build and SQLite feature store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from live15_quant.feature_registry import (
    FEATURE_REGISTRY,
    FEATURE_SCHEMA_VERSION,
    MissingReason,
)
from live15_quant.features import (
    COINBASE_PRODUCT_BY_ASSET,
    FeatureEngine,
    FeatureInputs,
    FeatureObservation,
    FeatureVector,
    SamplingPolicy,
    decimal_seconds,
)
from live15_quant.kalshi_lifecycle import KalshiResult
from live15_quant.models import Asset
from live15_quant.storage import RecorderStore, TrainingDataUnavailableError

DATASET_SCHEMA_VERSION = 1
DATASET_VERSION = "1.0.0"
ASOF_QUERY_VERSION = "received-and-source-asof-v1"


class DatasetMode(StrEnum):
    POOLED = "pooled"
    PER_ASSET = "per_asset"


@dataclass(frozen=True, slots=True)
class DatasetBuildConfig:
    sampling_policy: SamplingPolicy
    mode: DatasetMode = DatasetMode.POOLED
    assets: tuple[Asset, ...] = tuple(Asset)

    def __post_init__(self) -> None:
        if not self.assets or len(set(self.assets)) != len(self.assets):
            raise ValueError("dataset assets must be non-empty and unique")
        if self.mode is DatasetMode.PER_ASSET and len(self.assets) != 1:
            raise ValueError("per-asset dataset mode requires exactly one asset")

    def payload(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "assets": sorted(asset.value for asset in self.assets),
            "decision_offsets_seconds": sorted(
                decimal_seconds(value).to_eng_string()
                for value in self.sampling_policy.time_remaining
            ),
            "quote_max_age_seconds": decimal_seconds(
                self.sampling_policy.quote_max_age
            ).to_eng_string(),
            "underlying_max_age_seconds": decimal_seconds(
                self.sampling_policy.underlying_max_age
            ).to_eng_string(),
        }


@dataclass(frozen=True, slots=True)
class TrainingRow:
    build_id: str
    asset: Asset
    series: str
    ticker: str
    window_start: datetime
    window_end: datetime
    decision_timestamp: datetime
    time_remaining_seconds: Decimal
    target: Decimal
    label: KalshiResult
    features: FeatureVector
    source_market_row_id: int
    source_quote_row_ids: tuple[int, ...]
    source_tick_row_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        for value in (self.window_start, self.window_end, self.decision_timestamp):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("training row timestamps must be timezone-aware")
        if not self.window_start <= self.decision_timestamp < self.window_end:
            raise ValueError("training decision must be inside its event window")
        if self.features.decision_timestamp != self.decision_timestamp:
            raise ValueError("feature vector belongs to another decision timestamp")


@dataclass(frozen=True, slots=True)
class DatasetBuildSummary:
    build_id: str
    complete: bool
    events: int
    rows: int
    rows_written: int
    skipped_decisions: int
    diagnostics: dict[str, object]


_FEATURE_STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS feature_store_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS feature_definitions (
    feature_schema_version TEXT NOT NULL,
    name TEXT NOT NULL,
    family TEXT NOT NULL,
    unit TEXT NOT NULL,
    formula TEXT NOT NULL,
    lookback_seconds INTEGER NOT NULL,
    missing_policy TEXT NOT NULL,
    timestamp_semantics TEXT NOT NULL,
    PRIMARY KEY(feature_schema_version, name)
) STRICT;

CREATE TABLE IF NOT EXISTS dataset_builds (
    build_id TEXT PRIMARY KEY,
    dataset_version TEXT NOT NULL,
    feature_schema_version TEXT NOT NULL,
    config_json TEXT NOT NULL,
    source_snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('building', 'complete')),
    created_timestamp TEXT NOT NULL,
    completed_timestamp TEXT,
    diagnostics_json TEXT,
    content_hash TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS training_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_schema_version INTEGER NOT NULL,
    dataset_version TEXT NOT NULL,
    feature_schema_version TEXT NOT NULL,
    build_id TEXT NOT NULL REFERENCES dataset_builds(build_id),
    asset TEXT NOT NULL,
    series TEXT NOT NULL,
    ticker TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    decision_timestamp TEXT NOT NULL,
    time_remaining_seconds TEXT NOT NULL,
    target TEXT NOT NULL,
    label TEXT NOT NULL CHECK(label IN ('yes', 'no')),
    features_json TEXT NOT NULL,
    missing_json TEXT NOT NULL,
    feature_timestamps_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(build_id, ticker, decision_timestamp)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_training_examples_replay
ON training_examples(build_id, window_start, ticker, decision_timestamp, id);

CREATE INDEX IF NOT EXISTS idx_training_examples_asset
ON training_examples(build_id, asset, window_start, ticker, decision_timestamp);
"""


class FeatureStoreError(RuntimeError):
    pass


class FeatureStore:
    """Independent Decimal-safe SQLite store for derived training artifacts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=30.0)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            for marker in ("recorder_metadata", "paper_metadata"):
                exists = self._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (marker,)
                ).fetchone()
                if exists is not None:
                    raise FeatureStoreError(
                        "feature store cannot share raw recorder or paper ledger database"
                    )
            self._connection.executescript(_FEATURE_STORE_SCHEMA)
            self._connection.execute(
                """
                INSERT OR IGNORE INTO feature_store_metadata(key,value)
                VALUES('schema_version',?)
                """,
                (str(DATASET_SCHEMA_VERSION),),
            )
            version = self._connection.execute(
                "SELECT value FROM feature_store_metadata WHERE key='schema_version'"
            ).fetchone()
            if version is None or version["value"] != str(DATASET_SCHEMA_VERSION):
                raise FeatureStoreError("incompatible feature-store schema")
            self._register_features()
            self._connection.commit()
        except Exception:
            self.close()
            raise

    def _register_features(self) -> None:
        for definition in FEATURE_REGISTRY:
            values = (
                FEATURE_SCHEMA_VERSION,
                definition.name,
                definition.family.value,
                definition.unit,
                definition.formula,
                definition.lookback_seconds,
                definition.missing_policy.value,
                definition.timestamp_semantics.value,
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO feature_definitions VALUES(?,?,?,?,?,?,?,?)
                """,
                values,
            )
            row = self._connection.execute(
                "SELECT * FROM feature_definitions WHERE feature_schema_version=? AND name=?",
                (FEATURE_SCHEMA_VERSION, definition.name),
            ).fetchone()
            if row is None or tuple(row) != values:
                raise FeatureStoreError(f"conflicting feature definition: {definition.name}")

    def begin_build(
        self,
        build_id: str,
        config: dict[str, object],
        source_snapshot: dict[str, object],
    ) -> None:
        config_json = _canonical_json(config)
        source_json = _canonical_json(source_snapshot)
        content_hash = _hash(
            {
                "build_id": build_id,
                "dataset_version": DATASET_VERSION,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "config": config,
                "source_snapshot": source_snapshot,
            }
        )
        self._connection.execute(
            """
            INSERT OR IGNORE INTO dataset_builds(
                build_id,dataset_version,feature_schema_version,config_json,
                source_snapshot_json,status,created_timestamp,content_hash
            ) VALUES(?,?,?,?,?,'building',?,?)
            """,
            (
                build_id,
                DATASET_VERSION,
                FEATURE_SCHEMA_VERSION,
                config_json,
                source_json,
                _timestamp(datetime.now(UTC)),
                content_hash,
            ),
        )
        row = self._connection.execute(
            "SELECT content_hash FROM dataset_builds WHERE build_id=?", (build_id,)
        ).fetchone()
        if row is None or row["content_hash"] != content_hash:
            raise FeatureStoreError("dataset build manifest conflict")
        self._connection.commit()

    def append(self, row: TrainingRow) -> bool:
        features = {
            item.name: str(item.value) if item.value is not None else None
            for item in row.features.observations
        }
        missing = {
            item.name: item.missing_reason.value
            for item in row.features.observations
            if item.missing_reason is not None
        }
        timestamps = {
            item.name: _timestamp(item.source_timestamp)
            for item in row.features.observations
            if item.source_timestamp is not None
        }
        provenance = {
            "market_row_id": row.source_market_row_id,
            "quote_row_ids": list(row.source_quote_row_ids),
            "coinbase_tick_row_ids": list(row.source_tick_row_ids),
        }
        payload = (
            DATASET_SCHEMA_VERSION,
            DATASET_VERSION,
            FEATURE_SCHEMA_VERSION,
            row.build_id,
            row.asset.value,
            row.series,
            row.ticker,
            _timestamp(row.window_start),
            _timestamp(row.window_end),
            _timestamp(row.decision_timestamp),
            str(row.time_remaining_seconds),
            str(row.target),
            row.label.value,
            _canonical_json(features),
            _canonical_json(missing),
            _canonical_json(timestamps),
            _canonical_json(provenance),
        )
        content_hash = _hash(payload)
        existing = self._connection.execute(
            """
            SELECT content_hash FROM training_examples
            WHERE build_id=? AND ticker=? AND decision_timestamp=?
            """,
            (row.build_id, row.ticker, _timestamp(row.decision_timestamp)),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != content_hash:
                raise FeatureStoreError("non-deterministic training row conflict")
            return False
        self._connection.execute(
            """
            INSERT INTO training_examples(
                dataset_schema_version,dataset_version,feature_schema_version,build_id,
                asset,series,ticker,window_start,window_end,decision_timestamp,
                time_remaining_seconds,target,label,features_json,missing_json,
                feature_timestamps_json,provenance_json,content_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (*payload, content_hash),
        )
        self._connection.commit()
        return True

    def complete_build(self, build_id: str, diagnostics: dict[str, object]) -> None:
        self._connection.execute(
            """
            UPDATE dataset_builds SET status='complete', completed_timestamp=?, diagnostics_json=?
            WHERE build_id=?
            """,
            (_timestamp(datetime.now(UTC)), _canonical_json(diagnostics), build_id),
        )
        self._connection.commit()

    def build_status(self, build_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT status FROM dataset_builds WHERE build_id=?", (build_id,)
        ).fetchone()
        return None if row is None else str(row["status"])

    def replay(self, build_id: str, *, asset: Asset | None = None) -> tuple[TrainingRow, ...]:
        if asset is None:
            rows = self._connection.execute(
                """
                SELECT * FROM training_examples WHERE build_id=?
                ORDER BY window_start,ticker,decision_timestamp,id
                """,
                (build_id,),
            )
        else:
            rows = self._connection.execute(
                """
                SELECT * FROM training_examples WHERE build_id=? AND asset=?
                ORDER BY window_start,ticker,decision_timestamp,id
                """,
                (build_id, asset.value),
            )
        return tuple(self._training_row(row) for row in rows)

    def _training_row(self, row: sqlite3.Row) -> TrainingRow:
        try:
            stored_payload = tuple(
                row[name]
                for name in (
                    "dataset_schema_version",
                    "dataset_version",
                    "feature_schema_version",
                    "build_id",
                    "asset",
                    "series",
                    "ticker",
                    "window_start",
                    "window_end",
                    "decision_timestamp",
                    "time_remaining_seconds",
                    "target",
                    "label",
                    "features_json",
                    "missing_json",
                    "feature_timestamps_json",
                    "provenance_json",
                )
            )
            if _hash(stored_payload) != row["content_hash"]:
                raise FeatureStoreError("training example content hash mismatch")
            features = _json_object(row["features_json"])
            missing = _json_object(row["missing_json"])
            timestamps = _json_object(row["feature_timestamps_json"])
            provenance = _json_object(row["provenance_json"])
            observations = tuple(
                FeatureObservation(
                    definition.name,
                    Decimal(features[definition.name])
                    if features[definition.name] is not None
                    else None,
                    MissingReason(missing[definition.name]) if definition.name in missing else None,
                    _parse_timestamp(timestamps[definition.name])
                    if definition.name in timestamps
                    else None,
                )
                for definition in FEATURE_REGISTRY
            )
            decision = _parse_timestamp(row["decision_timestamp"])
            return TrainingRow(
                build_id=row["build_id"],
                asset=Asset(row["asset"]),
                series=row["series"],
                ticker=row["ticker"],
                window_start=_parse_timestamp(row["window_start"]),
                window_end=_parse_timestamp(row["window_end"]),
                decision_timestamp=decision,
                time_remaining_seconds=Decimal(row["time_remaining_seconds"]),
                target=Decimal(row["target"]),
                label=KalshiResult(row["label"]),
                features=FeatureVector(decision, observations),
                source_market_row_id=int(provenance["market_row_id"]),
                source_quote_row_ids=tuple(int(value) for value in provenance["quote_row_ids"]),
                source_tick_row_ids=tuple(
                    int(value) for value in provenance["coinbase_tick_row_ids"]
                ),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise FeatureStoreError("malformed training example") from error

    def count_rows(self, build_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM training_examples WHERE build_id=?", (build_id,)
        ).fetchone()
        return 0 if row is None else int(row["count"])

    def integrity_check(self) -> str:
        row = self._connection.execute("PRAGMA integrity_check").fetchone()
        return "missing_result" if row is None else str(row[0])

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> FeatureStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class DatasetBuilder:
    """Build rows idempotently from one immutable raw-store snapshot."""

    def __init__(self, source: RecorderStore, destination: FeatureStore) -> None:
        if source.path.resolve() == destination.path.resolve():
            raise ValueError("raw recorder and feature-store paths must be different")
        self._source = source
        self._destination = destination

    def build(
        self, config: DatasetBuildConfig, *, max_new_rows: int | None = None
    ) -> DatasetBuildSummary:
        if max_new_rows is not None and max_new_rows <= 0:
            raise ValueError("max_new_rows must be positive")
        snapshot = self._source.training_source_snapshot()
        snapshot["dataset_query_metadata"] = {
            "version": ASOF_QUERY_VERSION,
            "market": "latest non-terminal fetched_timestamp <= decision_timestamp",
            "quote": "received_timestamp and available source_timestamp <= decision_timestamp",
            "coinbase": "received_timestamp and available exchange_timestamp <= decision_timestamp",
            "label": "exact ticker/window Kalshi finalized result only",
        }
        table_limits = {
            name: int(snapshot[name]["max_id"])  # type: ignore[index]
            for name in (
                "coinbase_ticks",
                "kalshi_prediction_quotes",
                "kalshi_market_lifecycle",
                "kalshi_settlements",
            )
        }
        manifest = {
            "dataset_version": DATASET_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "config": config.payload(),
            "source_snapshot": snapshot,
        }
        build_id = _hash(manifest)
        self._destination.begin_build(build_id, config.payload(), snapshot)
        if self._destination.build_status(build_id) == "complete":
            rows = self._destination.replay(build_id)
            diagnostics = dataset_diagnostics(rows)
            return DatasetBuildSummary(
                build_id, True, len({row.ticker for row in rows}), len(rows), 0, 0, diagnostics
            )

        settlements = tuple(
            settlement
            for settlement in self._source.replay_kalshi_settlements(
                max_row_id=table_limits["kalshi_settlements"]
            )
            if settlement.asset in config.assets
        )
        engine = FeatureEngine(config.sampling_policy)
        rows_written = skipped = 0
        stopped_early = False
        for settlement in settlements:
            for decision in config.sampling_policy.decision_times(
                settlement.window_start, settlement.window_end
            ):
                try:
                    joined = self._source.join_training_label(
                        settlement.ticker,
                        decision,
                        market_max_row_id=table_limits["kalshi_market_lifecycle"],
                        quote_max_row_id=table_limits["kalshi_prediction_quotes"],
                        settlement_max_row_id=table_limits["kalshi_settlements"],
                    )
                except TrainingDataUnavailableError:
                    skipped += 1
                    continue
                product = COINBASE_PRODUCT_BY_ASSET.get(settlement.asset)
                ticks = (
                    tuple(
                        self._source.replay_coinbase_range(
                            product,
                            start=decision
                            - timedelta(seconds=300)
                            - config.sampling_policy.underlying_max_age,
                            end=decision,
                            max_row_id=table_limits["coinbase_ticks"],
                        )
                    )
                    if product is not None
                    else ()
                )
                safe_ticks = tuple(
                    tick
                    for tick in ticks
                    if tick.received_timestamp <= decision
                    and (tick.exchange_timestamp is None or tick.exchange_timestamp <= decision)
                )
                safe_quotes = tuple(
                    quote
                    for quote in joined.observations
                    if quote.received_timestamp <= decision
                    and (quote.source_timestamp is None or quote.source_timestamp <= decision)
                )
                vector = engine.compute(
                    FeatureInputs(joined.market, safe_quotes, safe_ticks, decision)
                )
                eligible_ticks = tuple(tick.row_id for tick in safe_ticks)
                eligible_quotes = tuple(quote.row_id for quote in safe_quotes)
                written = self._destination.append(
                    TrainingRow(
                        build_id=build_id,
                        asset=joined.market.asset,
                        series=joined.market.series,
                        ticker=joined.ticker,
                        window_start=joined.market.window_start,
                        window_end=joined.market.window_end,
                        decision_timestamp=decision,
                        time_remaining_seconds=decimal_seconds(joined.market.window_end - decision),
                        target=joined.market.target,
                        label=joined.label.result,
                        features=vector,
                        source_market_row_id=joined.market.row_id,
                        source_quote_row_ids=eligible_quotes,
                        source_tick_row_ids=eligible_ticks,
                    )
                )
                rows_written += int(written)
                if max_new_rows is not None and rows_written >= max_new_rows:
                    stopped_early = True
                    break
            if stopped_early:
                break
        rows = self._destination.replay(build_id)
        diagnostics = dataset_diagnostics(rows)
        if not stopped_early:
            self._destination.complete_build(build_id, diagnostics)
        return DatasetBuildSummary(
            build_id=build_id,
            complete=not stopped_early,
            events=len({row.ticker for row in rows}),
            rows=len(rows),
            rows_written=rows_written,
            skipped_decisions=skipped,
            diagnostics=diagnostics,
        )


def dataset_diagnostics(rows: tuple[TrainingRow, ...]) -> dict[str, object]:
    by_asset = Counter(row.asset.value for row in rows)
    labels = Counter(row.label.value for row in rows)
    buckets = Counter(str(row.time_remaining_seconds) for row in rows)
    missing = Counter()
    stale = Counter()
    distributions: dict[str, list[Decimal]] = defaultdict(list)
    dates = Counter()
    hours = Counter()
    tracked = (
        "quote_age_seconds",
        "yes_spread",
        "realized_volatility_60s",
        "realized_volatility_300s",
        "absolute_distance_to_target",
        "normalized_distance_to_target",
    )
    for row in rows:
        dates[row.decision_timestamp.date().isoformat()] += 1
        hours[f"{row.decision_timestamp.hour:02d}"] += 1
        for observation in row.features.observations:
            if observation.missing_reason is not None:
                missing[observation.name] += 1
                if observation.missing_reason is MissingReason.STALE:
                    stale[observation.name] += 1
            if observation.name in tracked and observation.value is not None:
                distributions[observation.name].append(observation.value)
    denominator = Decimal(len(rows)) if rows else None
    return {
        "events_count": len({row.ticker for row in rows}),
        "rows_count": len(rows),
        "rows_per_asset": {asset.value: by_asset[asset.value] for asset in Asset},
        "label_balance": {result.value: labels[result.value] for result in KalshiResult},
        "rows_per_decision_bucket_seconds": dict(sorted(buckets.items())),
        "missing_feature_rates": {
            definition.name: (
                str(Decimal(missing[definition.name]) / denominator)
                if denominator is not None
                else None
            )
            for definition in FEATURE_REGISTRY
        },
        "stale_feature_rates": {
            definition.name: (
                str(Decimal(stale[definition.name]) / denominator)
                if denominator is not None
                else None
            )
            for definition in FEATURE_REGISTRY
        },
        "distributions": {
            name: _distribution(values) for name, values in sorted(distributions.items())
        },
        "coverage_by_utc_date": dict(sorted(dates.items())),
        "coverage_by_utc_hour": dict(sorted(hours.items())),
    }


def _distribution(values: list[Decimal]) -> dict[str, str]:
    ordered = sorted(values)
    if not ordered:
        return {}
    return {
        "min": str(ordered[0]),
        "median": str(ordered[(len(ordered) - 1) // 2]),
        "p95": str(ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)]),
        "max": str(ordered[-1]),
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("persisted dataset timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise FeatureStoreError("malformed feature-store timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FeatureStoreError("malformed feature-store timestamp")
    return parsed.astimezone(UTC)


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        raise FeatureStoreError("malformed feature-store JSON")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise FeatureStoreError("malformed feature-store JSON")
    return parsed
