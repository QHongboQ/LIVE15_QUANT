"""Bounded, read-only projections for the local Control Center."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from live15_quant.dataset import DATASET_VERSION
from live15_quant.feature_registry import FEATURE_SCHEMA_VERSION
from live15_quant.features import (
    COINBASE_PRODUCT_BY_ASSET,
    FeatureEngine,
    FeatureInputs,
    SamplingPolicy,
)
from live15_quant.models import Asset
from live15_quant.storage import RecorderStorageError, RecorderStore


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("dashboard timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _decimal(value: object) -> str | None:
    return None if value is None else str(value)


def _json(value: object, default: object) -> object:
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class DashboardReadStore:
    """Open SQLite in mode=ro and issue only indexed, bounded queries."""

    def __init__(self, raw_path: Path, feature_path: Path) -> None:
        self.raw_path = raw_path
        self.feature_path = feature_path

    @staticmethod
    def _open(path: Path) -> sqlite3.Connection | None:
        try:
            exists = path.is_file()
        except OSError:
            return None
        if not exists:
            return None
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=2
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=2000")
            return connection
        except (OSError, sqlite3.Error):
            if connection is not None:
                connection.close()
            return None

    def asset(
        self, asset: Asset, now: datetime, current_ticker: str | None = None
    ) -> dict[str, Any]:
        connection = self._open(self.raw_path)
        if connection is None:
            return self._missing_asset(asset, "raw_store_unavailable")
        try:
            market = self._market_row(connection, asset, now, current_ticker)
            if market is None:
                return self._missing_asset(asset, "market_missing")
            ticker = str(market["ticker"])
            quote = connection.execute(
                """SELECT * FROM kalshi_prediction_quotes WHERE ticker=?
                ORDER BY received_timestamp DESC, id DESC LIMIT 1""",
                (ticker,),
            ).fetchone()
            product = COINBASE_PRODUCT_BY_ASSET.get(asset)
            tick = None
            if product is not None:
                tick = connection.execute(
                    """SELECT * FROM coinbase_ticks WHERE product=?
                    ORDER BY received_timestamp DESC, id DESC LIMIT 1""",
                    (product,),
                ).fetchone()
            end = datetime.fromisoformat(str(market["window_end"])).astimezone(UTC)
            quote_received = (
                datetime.fromisoformat(str(quote["received_timestamp"])).astimezone(UTC)
                if quote is not None
                else None
            )
            tick_received = (
                datetime.fromisoformat(str(tick["received_timestamp"])).astimezone(UTC)
                if tick is not None
                else None
            )
            quote_age = (
                None if quote_received is None else max(0, (now - quote_received).total_seconds())
            )
            tick_age = (
                None if tick_received is None else max(0, (now - tick_received).total_seconds())
            )
            yes_bid = _decimal(quote["yes_bid"]) if quote is not None else None
            yes_ask = _decimal(quote["yes_ask"]) if quote is not None else None
            spread = None
            if yes_bid is not None and yes_ask is not None:
                spread = str(Decimal(yes_ask) - Decimal(yes_bid))
            return {
                "asset": asset.value,
                "ticker": ticker,
                "series": str(market["series"]),
                "target": str(market["target"]),
                "window_start": str(market["window_start"]),
                "window_end": str(market["window_end"]),
                "seconds_remaining": max(0, (end - now).total_seconds()),
                "lifecycle": str(market["lifecycle"]),
                "official_status": str(market["official_status"]),
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "no_bid": _decimal(quote["no_bid"]) if quote is not None else None,
                "no_ask": _decimal(quote["no_ask"]) if quote is not None else None,
                "last_trade": _decimal(quote["last_trade"]) if quote is not None else None,
                "spread": spread,
                "quote_age_seconds": quote_age,
                "quote_status": self._age_status(quote_age, 15),
                "quote_source_timestamp": (
                    _decimal(quote["source_timestamp"]) if quote is not None else None
                ),
                "quote_received_timestamp": (
                    str(quote["received_timestamp"]) if quote is not None else None
                ),
                "orderbook_status": "available" if quote is not None else "missing",
                "yes_bid_depth": _json(quote["yes_bid_depth"], []) if quote is not None else [],
                "no_bid_depth": _json(quote["no_bid_depth"], []) if quote is not None else [],
                "underlying_product": product,
                "underlying_price": _decimal(tick["price"]) if tick is not None else None,
                "underlying_age_seconds": tick_age,
                "underlying_status": (
                    "unsupported" if product is None else self._age_status(tick_age, 30)
                ),
                "settlement_followup": "pending" if now >= end else "not_due",
                "features": self._features(connection, market, now),
                "availability": "available",
            }
        except (sqlite3.Error, ValueError, RecorderStorageError):
            return self._missing_asset(asset, "store_error")
        finally:
            connection.close()

    @staticmethod
    def _market_row(
        connection: sqlite3.Connection,
        asset: Asset,
        now: datetime,
        current_ticker: str | None,
    ) -> sqlite3.Row | None:
        if current_ticker:
            return connection.execute(
                """SELECT * FROM kalshi_market_lifecycle WHERE ticker=? AND asset=?
                ORDER BY fetched_timestamp DESC, id DESC LIMIT 1""",
                (current_ticker, asset.value),
            ).fetchone()
        return connection.execute(
            """SELECT * FROM kalshi_market_lifecycle
            WHERE asset=? AND window_start<=? AND window_end>?
            AND lifecycle IN ('open','paused','upcoming')
            ORDER BY fetched_timestamp DESC, id DESC LIMIT 1""",
            (asset.value, _timestamp(now), _timestamp(now)),
        ).fetchone()

    @staticmethod
    def _age_status(age: float | None, stale_after: float) -> str:
        if age is None:
            return "missing"
        return "stale" if age > stale_after else "healthy"

    @staticmethod
    def _missing_asset(asset: Asset, reason: str) -> dict[str, Any]:
        return {
            "asset": asset.value,
            "ticker": None,
            "lifecycle": "unavailable",
            "availability": reason,
            "quote_status": "missing",
            "orderbook_status": "missing",
            "underlying_status": (
                "missing" if asset in COINBASE_PRODUCT_BY_ASSET else "unsupported"
            ),
            "settlement_followup": "unavailable",
            "features": {},
        }

    @staticmethod
    def _features(
        connection: sqlite3.Connection, market_row: sqlite3.Row, now: datetime
    ) -> dict[str, dict[str, str | None]]:
        try:
            decision = min(
                now,
                datetime.fromisoformat(str(market_row["window_end"])).astimezone(UTC)
                - timedelta(microseconds=1),
            )
            market = RecorderStore._kalshi_feature_market_record(market_row)
            quote_rows = connection.execute(
                """SELECT * FROM kalshi_prediction_quotes
                WHERE ticker=? AND received_timestamp<=? AND received_timestamp>=?
                ORDER BY received_timestamp ASC, id ASC LIMIT 1000""",
                (market.ticker, _timestamp(decision), _timestamp(decision - timedelta(minutes=6))),
            )
            product = COINBASE_PRODUCT_BY_ASSET.get(market.asset)
            tick_rows: tuple[sqlite3.Row, ...] = ()
            if product is not None:
                tick_rows = tuple(
                    connection.execute(
                        """SELECT * FROM coinbase_ticks
                        WHERE product=? AND received_timestamp<=? AND received_timestamp>=?
                        ORDER BY received_timestamp ASC, id ASC LIMIT 5000""",
                        (
                            product,
                            _timestamp(decision),
                            _timestamp(decision - timedelta(minutes=6)),
                        ),
                    )
                )
            inputs = FeatureInputs(
                market=market,
                quotes=tuple(RecorderStore._kalshi_native_quote_record(row) for row in quote_rows),
                ticks=tuple(RecorderStore._tick_record(row) for row in tick_rows),
                decision_timestamp=decision,
            )
            policy = SamplingPolicy((timedelta(seconds=60),))
            vector = FeatureEngine(policy).compute(inputs)
            wanted = {
                "return_60s",
                "return_300s",
                "realized_volatility_60s",
                "realized_volatility_300s",
                "signed_distance_to_target",
                "normalized_distance_to_target",
                "top_depth_imbalance",
                "yes_cumulative_depth",
                "no_cumulative_depth",
            }
            return {
                item.name: {
                    "value": None if item.value is None else str(item.value),
                    "status": (
                        "available" if item.missing_reason is None else item.missing_reason.value
                    ),
                }
                for item in vector.observations
                if item.name in wanted
            }
        except (sqlite3.Error, ValueError, RecorderStorageError):
            return {}

    def previous_events(self, asset: Asset, limit: int = 8) -> list[dict[str, Any]]:
        connection = self._open(self.raw_path)
        if connection is None:
            return []
        try:
            rows = connection.execute(
                """SELECT ticker,target,result,settlement_timestamp,window_end
                FROM kalshi_settlements WHERE asset=?
                ORDER BY settlement_timestamp DESC,id DESC LIMIT ?""",
                (asset.value, min(max(limit, 1), 25)),
            )
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []
        finally:
            connection.close()

    def coverage(self) -> dict[str, Any]:
        finalized = self._finalized_counts()
        connection = self._open(self.feature_path)
        if connection is None:
            return self._empty_coverage("not_enough_training_data", finalized)
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                """SELECT * FROM dataset_builds WHERE status='complete'
                ORDER BY completed_timestamp DESC,created_timestamp DESC LIMIT 1"""
            ).fetchone()
            if row is None:
                return self._empty_coverage("not_enough_training_data", finalized)
            diagnostics = _json(row["diagnostics_json"], {})
            diagnostic_map = diagnostics if isinstance(diagnostics, dict) else {}
            source_snapshot = _json(row["source_snapshot_json"], {})
            snapshot_settlements = (
                source_snapshot.get("kalshi_settlements")
                if isinstance(source_snapshot, dict)
                else None
            )
            snapshot_max_id = (
                snapshot_settlements.get("max_id")
                if isinstance(snapshot_settlements, dict)
                else None
            )
            snapshot_counts = (
                self._finalized_counts(max_row_id=snapshot_max_id)
                if isinstance(snapshot_max_id, int) and not isinstance(snapshot_max_id, bool)
                else None
            )
            counts = {
                asset.value: {
                    "finalized_events": finalized[asset.value],
                    "evaluated_finalized_events": (
                        snapshot_counts[asset.value] if snapshot_counts is not None else 0
                    ),
                    "unevaluated_finalized_events": (
                        finalized[asset.value] - snapshot_counts[asset.value]
                        if snapshot_counts is not None
                        else finalized[asset.value]
                    ),
                    "trainable_events": 0,
                    "training_rows": 0,
                }
                for asset in Asset
            }
            for item in connection.execute(
                """SELECT asset,COUNT(DISTINCT ticker) events,COUNT(*) rows
                FROM training_examples WHERE build_id=? GROUP BY asset""",
                (row["build_id"],),
            ):
                counts[str(item["asset"])] = {
                    "finalized_events": finalized[str(item["asset"])],
                    "evaluated_finalized_events": (
                        snapshot_counts[str(item["asset"])] if snapshot_counts is not None else 0
                    ),
                    "unevaluated_finalized_events": (
                        finalized[str(item["asset"])] - snapshot_counts[str(item["asset"])]
                        if snapshot_counts is not None
                        else finalized[str(item["asset"])]
                    ),
                    "trainable_events": int(item["events"]),
                    "training_rows": int(item["rows"]),
                }
            trainable_events = sum(item["trainable_events"] for item in counts.values())
            training_rows = sum(item["training_rows"] for item in counts.values())
            snapshot_finalized = (
                sum(snapshot_counts.values()) if snapshot_counts is not None else None
            )
            unevaluated = (
                sum(finalized.values()) - snapshot_finalized
                if snapshot_finalized is not None
                else None
            )
            return {
                "status": "available",
                "finalized_events": sum(finalized.values()),
                "trainable_events": trainable_events,
                "training_rows": training_rows,
                "build_id": str(row["build_id"]),
                "dataset_version": str(row["dataset_version"]),
                "feature_schema_version": str(row["feature_schema_version"]),
                "completed_timestamp": row["completed_timestamp"],
                "snapshot_status": (
                    "unknown"
                    if snapshot_finalized is None
                    else "current"
                    if unevaluated == 0
                    else "outdated"
                ),
                "snapshot_finalized_events": snapshot_finalized,
                "unevaluated_finalized_events": unevaluated,
                "skipped_decisions": _optional_nonnegative_int(
                    diagnostic_map.get("skipped_decisions")
                ),
                "events_without_training_rows": _optional_nonnegative_int(
                    diagnostic_map.get("events_without_training_rows")
                ),
                "trainability_rejections": _int_dict(diagnostic_map.get("trainability_rejections")),
                "per_asset": counts,
                "label_balance": diagnostic_map.get("label_balance"),
                "decision_time_bucket_coverage": diagnostic_map.get(
                    "rows_per_decision_bucket_seconds"
                ),
                "missing_feature_rates": diagnostic_map.get("missing_feature_rates"),
                "stale_feature_rates": diagnostic_map.get("stale_feature_rates"),
            }
        except sqlite3.Error:
            return self._empty_coverage("feature_store_error", finalized)
        finally:
            connection.close()

    @staticmethod
    def _empty_coverage(reason: str, finalized: dict[str, int]) -> dict[str, Any]:
        return {
            "status": reason,
            "finalized_events": sum(finalized.values()),
            "trainable_events": 0,
            "training_rows": 0,
            "dataset_version": DATASET_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "build_id": None,
            "completed_timestamp": None,
            "snapshot_status": "not_built",
            "snapshot_finalized_events": None,
            "unevaluated_finalized_events": sum(finalized.values()),
            "skipped_decisions": None,
            "events_without_training_rows": None,
            "trainability_rejections": None,
            "label_balance": None,
            "decision_time_bucket_coverage": None,
            "missing_feature_rates": None,
            "stale_feature_rates": None,
            "per_asset": {
                asset.value: {
                    "finalized_events": finalized[asset.value],
                    "evaluated_finalized_events": 0,
                    "unevaluated_finalized_events": finalized[asset.value],
                    "trainable_events": 0,
                    "training_rows": 0,
                }
                for asset in Asset
            },
        }

    def _finalized_counts(self, *, max_row_id: int | None = None) -> dict[str, int]:
        counts = {asset.value: 0 for asset in Asset}
        raw = self._open(self.raw_path)
        if raw is None:
            return counts
        try:
            raw.execute("BEGIN")
            query = "SELECT asset,COUNT(*) count FROM kalshi_settlements"
            parameters: tuple[object, ...] = ()
            if max_row_id is not None:
                query += " WHERE id<=?"
                parameters = (max_row_id,)
            query += " GROUP BY asset"
            for item in raw.execute(query, parameters):
                asset = str(item["asset"])
                if asset in counts:
                    counts[asset] = int(item["count"])
        except sqlite3.Error:
            return {asset.value: 0 for asset in Asset}
        finally:
            raw.close()
        return counts

    def recorder_events(
        self,
        *,
        limit: int,
        severity: str | None,
        asset: Asset | None,
        source: str | None,
        since: datetime | None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("event limit must be in 1..200")
        if source is not None and (not source or len(source) > 160):
            raise ValueError("invalid event source filter")
        connection = self._open(self.raw_path)
        if connection is None:
            return []
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recorder_events'"
            ).fetchone()
            if exists is None:
                return []
            clauses: list[str] = []
            parameters: list[object] = []
            if severity is not None:
                clauses.append("severity=?")
                parameters.append(severity)
            if asset is not None:
                clauses.append("asset=?")
                parameters.append(asset.value)
            if source is not None:
                clauses.append("source=?")
                parameters.append(source)
            if since is not None:
                clauses.append("observed_timestamp>=?")
                parameters.append(_timestamp(since))
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            parameters.append(limit)
            rows = connection.execute(
                f"""SELECT observed_timestamp,severity,event_type,asset,source,error_type,message
                FROM recorder_events {where}
                ORDER BY observed_timestamp DESC,id DESC LIMIT ?""",
                parameters,
            )
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []
        finally:
            connection.close()


def _optional_nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _int_dict(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    result = {
        key: item
        for key, item in value.items()
        if isinstance(key, str)
        and isinstance(item, int)
        and not isinstance(item, bool)
        and item >= 0
    }
    return result if len(result) == len(value) else None
