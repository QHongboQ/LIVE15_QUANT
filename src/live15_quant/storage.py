"""Crash-resilient append-only SQLite storage for recorder observations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from live15_quant.kalshi_lifecycle import (
    KalshiLifecycle,
    KalshiMarket,
    KalshiResult,
    KalshiSettlementTruth,
)
from live15_quant.models import (
    Asset,
    DataRole,
    FifteenMinuteContract,
    KalshiNativeQuote,
    MarketTick,
    PredictionMarketQuote,
    RecorderDiagnosticKind,
)
from live15_quant.records import (
    SCHEMA_VERSION,
    CoinbaseTickRecord,
    KalshiFeatureMarketRecord,
    KalshiMarketRecord,
    KalshiNativeQuoteRecord,
    KalshiSettlementRecord,
    PredictionQuoteRecord,
    RobinhoodDiagnosticRecord,
    RobinhoodSnapshotRecord,
    TrainingLabelExample,
)


class RecorderStorageError(RuntimeError):
    """Raised when persisted recorder data is invalid or incompatible."""


class TrainingDataUnavailableError(RecorderStorageError):
    """Expected absence of a label or decision-time source observation."""


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("persisted timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise RecorderStorageError(f"malformed {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RecorderStorageError(f"malformed {field}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecorderStorageError(f"malformed {field}")
    return parsed.astimezone(UTC)


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _parse_decimal(value: object, field: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise RecorderStorageError(f"malformed {field}")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise RecorderStorageError(f"malformed {field}") from error
    if not parsed.is_finite():
        raise RecorderStorageError(f"malformed {field}")
    return parsed


def _parse_string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise RecorderStorageError(f"malformed {field}")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise RecorderStorageError(f"malformed {field}") from error
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise RecorderStorageError(f"malformed {field}")
    return tuple(parsed)


def _book_levels(value: object, field: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, str):
        raise RecorderStorageError(f"malformed {field}")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise RecorderStorageError(f"malformed {field}") from error
    if not isinstance(parsed, list):
        raise RecorderStorageError(f"malformed {field}")
    result: list[tuple[str, str]] = []
    for item in parsed:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise RecorderStorageError(f"malformed {field}")
        result.append((item[0], item[1]))
    return tuple(result)


def _fingerprint(values: Sequence[object]) -> str:
    encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_DIAGNOSTIC_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS robinhood_diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    asset TEXT NOT NULL,
    event_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    observed_timestamp TEXT NOT NULL,
    event_end_time TEXT NOT NULL,
    related_event_id TEXT,
    source_url TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(kind, asset, event_id, observed_timestamp, content_hash)
) STRICT
"""

_DIAGNOSTIC_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_robinhood_diagnostic_asset
ON robinhood_diagnostics(asset, observed_timestamp, id)
"""

_DIAGNOSTIC_LOGICAL_KEY_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_robinhood_diagnostic_logical_key
ON robinhood_diagnostics(kind, asset, event_id, id)
"""

_QUOTE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS prediction_market_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    asset TEXT NOT NULL,
    robinhood_event_id TEXT NOT NULL,
    robinhood_contract_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    venue_series TEXT NOT NULL,
    venue_ticker TEXT NOT NULL,
    mapping_confidence TEXT NOT NULL,
    source_timestamp TEXT,
    source_timestamp_kind TEXT NOT NULL,
    received_timestamp TEXT NOT NULL,
    yes_bid TEXT,
    yes_ask TEXT,
    no_bid TEXT,
    no_ask TEXT,
    last_trade TEXT,
    volume TEXT,
    yes_bid_depth TEXT NOT NULL,
    no_bid_depth TEXT NOT NULL,
    source TEXT NOT NULL,
    freshness TEXT NOT NULL,
    executability TEXT NOT NULL,
    evidence_urls TEXT NOT NULL,
    data_role TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(robinhood_event_id, venue_ticker, received_timestamp, content_hash)
) STRICT
"""

_QUOTE_EVENT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_prediction_quote_event_replay
ON prediction_market_quotes(robinhood_event_id, received_timestamp, id)
"""

_QUOTE_TICKER_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_prediction_quote_ticker_replay
ON prediction_market_quotes(venue_ticker, received_timestamp, id)
"""

_KALSHI_MARKET_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kalshi_market_lifecycle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    asset TEXT NOT NULL,
    series TEXT NOT NULL,
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    target TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    official_status TEXT NOT NULL,
    fetched_timestamp TEXT NOT NULL,
    source_url TEXT NOT NULL,
    rules_primary TEXT NOT NULL,
    rules_secondary TEXT NOT NULL,
    settlement_timer_seconds INTEGER NOT NULL CHECK (settlement_timer_seconds >= 0),
    determination_result TEXT,
    content_hash TEXT NOT NULL,
    UNIQUE(ticker, fetched_timestamp, content_hash)
) STRICT
"""

_KALSHI_MARKET_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_kalshi_market_replay
ON kalshi_market_lifecycle(ticker, fetched_timestamp, id)
"""

_KALSHI_MARKET_FOLLOWUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_kalshi_market_followup
ON kalshi_market_lifecycle(window_end, lifecycle, ticker, fetched_timestamp, id)
"""

_KALSHI_SETTLEMENT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kalshi_settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    asset TEXT NOT NULL,
    series TEXT NOT NULL,
    ticker TEXT NOT NULL UNIQUE,
    event_ticker TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    target TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('yes', 'no')),
    settlement_timestamp TEXT NOT NULL,
    settlement_value TEXT,
    expiration_value TEXT,
    official_source TEXT NOT NULL,
    fetched_timestamp TEXT NOT NULL,
    data_role TEXT NOT NULL,
    content_hash TEXT NOT NULL
) STRICT
"""

_KALSHI_SETTLEMENT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_kalshi_settlement_window
ON kalshi_settlements(series, window_start, ticker)
"""

_KALSHI_SETTLEMENT_ASSET_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_kalshi_settlement_asset_cursor
ON kalshi_settlements(asset, settlement_timestamp, id)
"""

_KALSHI_NATIVE_QUOTE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kalshi_prediction_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    asset TEXT NOT NULL,
    series TEXT NOT NULL,
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    source_timestamp TEXT,
    source_timestamp_kind TEXT NOT NULL,
    received_timestamp TEXT NOT NULL,
    yes_bid TEXT,
    yes_ask TEXT,
    no_bid TEXT,
    no_ask TEXT,
    last_trade TEXT,
    volume TEXT,
    yes_bid_depth TEXT NOT NULL,
    no_bid_depth TEXT NOT NULL,
    source TEXT NOT NULL,
    freshness TEXT NOT NULL,
    executability TEXT NOT NULL,
    evidence_urls TEXT NOT NULL,
    data_role TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(ticker, received_timestamp, content_hash)
) STRICT
"""

_KALSHI_NATIVE_QUOTE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_kalshi_native_quote_replay
ON kalshi_prediction_quotes(ticker, received_timestamp, id)
"""

_KALSHI_NATIVE_QUOTE_ASSET_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_kalshi_native_quote_asset_cursor
ON kalshi_prediction_quotes(asset, received_timestamp, id)
"""

_KALSHI_CONFLICT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kalshi_settlement_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    existing_hash TEXT NOT NULL,
    conflicting_hash TEXT NOT NULL,
    observed_timestamp TEXT NOT NULL,
    source_url TEXT NOT NULL,
    UNIQUE(ticker, existing_hash, conflicting_hash)
) STRICT
"""

_KALSHI_BACKFILL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kalshi_backfill_state (
    series TEXT NOT NULL,
    source_path TEXT NOT NULL,
    range_start TEXT NOT NULL,
    range_end TEXT NOT NULL,
    next_cursor TEXT,
    updated_timestamp TEXT NOT NULL,
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    PRIMARY KEY(series, source_path, range_start, range_end)
) STRICT
"""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS recorder_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS robinhood_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    asset TEXT NOT NULL,
    event_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    fetched_timestamp TEXT NOT NULL,
    seconds_remaining INTEGER NOT NULL CHECK (seconds_remaining >= 0),
    target_price TEXT NOT NULL,
    displayed_yes TEXT,
    displayed_no TEXT,
    quote_availability TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    freshness TEXT NOT NULL,
    venue TEXT,
    settlement_benchmark TEXT NOT NULL,
    settlement_method TEXT NOT NULL,
    settlement_decimal_places INTEGER,
    settlement_source_url TEXT NOT NULL,
    settlement_benchmark_data_url TEXT NOT NULL,
    settlement_data_access TEXT NOT NULL,
    settlement_access_notes TEXT NOT NULL,
    settlement_data_role TEXT NOT NULL,
    source_age_seconds INTEGER,
    venue_candidates TEXT NOT NULL,
    source_url TEXT NOT NULL,
    data_role TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(event_id, fetched_timestamp, content_hash)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_robinhood_event_replay
ON robinhood_snapshots(event_id, fetched_timestamp, id);

{_DIAGNOSTIC_TABLE_SQL};
{_DIAGNOSTIC_INDEX_SQL};
{_DIAGNOSTIC_LOGICAL_KEY_INDEX_SQL};

CREATE TABLE IF NOT EXISTS coinbase_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    exchange_timestamp TEXT,
    received_timestamp TEXT NOT NULL,
    product TEXT NOT NULL,
    price TEXT NOT NULL,
    bid TEXT NOT NULL,
    ask TEXT NOT NULL,
    spread TEXT NOT NULL,
    bid_size TEXT,
    ask_size TEXT,
    last_size TEXT,
    volume_24h TEXT,
    data_role TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(product, received_timestamp, content_hash)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_coinbase_product_replay
ON coinbase_ticks(product, received_timestamp, id);

{_QUOTE_TABLE_SQL};
{_QUOTE_EVENT_INDEX_SQL};
{_QUOTE_TICKER_INDEX_SQL};

{_KALSHI_MARKET_TABLE_SQL};
{_KALSHI_MARKET_INDEX_SQL};
{_KALSHI_MARKET_FOLLOWUP_INDEX_SQL};
{_KALSHI_SETTLEMENT_TABLE_SQL};
{_KALSHI_SETTLEMENT_INDEX_SQL};
{_KALSHI_SETTLEMENT_ASSET_INDEX_SQL};
{_KALSHI_NATIVE_QUOTE_TABLE_SQL};
{_KALSHI_NATIVE_QUOTE_INDEX_SQL};
{_KALSHI_NATIVE_QUOTE_ASSET_INDEX_SQL};
{_KALSHI_CONFLICT_TABLE_SQL};
{_KALSHI_BACKFILL_TABLE_SQL};
"""


class RecorderStore:
    """Own a SQLite WAL database and expose insert-only recorder operations."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA wal_autocheckpoint=1000")
            self._connection.execute("PRAGMA journal_size_limit=67108864")
            self._initialize_or_migrate_schema()
        except Exception:
            self._connection.close()
            raise

    def _initialize_or_migrate_schema(self) -> None:
        metadata_exists = self._connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'recorder_metadata'
            """
        ).fetchone()
        if metadata_exists is None:
            for marker in ("paper_metadata", "feature_store_metadata"):
                other_store = self._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (marker,)
                ).fetchone()
                if other_store is not None:
                    raise RecorderStorageError(
                        "raw recorder cannot share the paper ledger or feature-store database"
                    )
            self._create_schema()
            return

        row = self._connection.execute(
            "SELECT value FROM recorder_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            raise RecorderStorageError("database schema metadata is missing")
        if row["value"] == str(SCHEMA_VERSION):
            self._ensure_schema_objects()
            return
        if row["value"] == "1":
            self._migrate_v1_to_v2()
            row = self._connection.execute(
                "SELECT value FROM recorder_metadata WHERE key = 'schema_version'"
            ).fetchone()
        if row is not None and row["value"] == "2":
            self._migrate_v2_to_v3()
            row = self._connection.execute(
                "SELECT value FROM recorder_metadata WHERE key = 'schema_version'"
            ).fetchone()
        if row is not None and row["value"] == "3" and SCHEMA_VERSION == 4:
            self._migrate_v3_to_v4()
            return
        raise RecorderStorageError(
            f"database schema {row['value']} is incompatible with {SCHEMA_VERSION}"
        )

    def _create_schema(self) -> None:
        try:
            self._connection.executescript(
                "BEGIN IMMEDIATE;\n"
                f"{_SCHEMA}\n"
                "INSERT INTO recorder_metadata(key, value) "
                f"VALUES ('schema_version', '{SCHEMA_VERSION}');\n"
                "COMMIT;"
            )
        except Exception:
            self._connection.rollback()
            raise

    def _ensure_schema_objects(self) -> None:
        try:
            self._connection.executescript(f"BEGIN IMMEDIATE;\n{_SCHEMA}\nCOMMIT;")
        except Exception:
            self._connection.rollback()
            raise

    def _migrate_v1_to_v2(self) -> None:
        """Atomically add diagnostics and relabel unchanged v1 observation rows."""

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for table in ("robinhood_snapshots", "coinbase_ticks"):
                invalid = self._connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE schema_version != 1"
                ).fetchone()
                if invalid is None or invalid[0] != 0:
                    raise RecorderStorageError(f"mixed schema versions in {table}")
            self._connection.execute(_DIAGNOSTIC_TABLE_SQL)
            self._connection.execute(_DIAGNOSTIC_INDEX_SQL)
            self._connection.execute(_DIAGNOSTIC_LOGICAL_KEY_INDEX_SQL)
            self._connection.execute(
                "UPDATE robinhood_snapshots SET schema_version = ? WHERE schema_version = 1",
                (2,),
            )
            self._connection.execute(
                "UPDATE coinbase_ticks SET schema_version = ? WHERE schema_version = 1",
                (2,),
            )
            self._connection.execute(
                "UPDATE recorder_metadata SET value = ? WHERE key = 'schema_version'",
                ("2",),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _migrate_v2_to_v3(self) -> None:
        """Atomically add the independent official quote stream."""

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for table in ("robinhood_snapshots", "coinbase_ticks", "robinhood_diagnostics"):
                invalid = self._connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE schema_version != 2"
                ).fetchone()
                if invalid is None or invalid[0] != 0:
                    raise RecorderStorageError(f"mixed schema versions in {table}")
            for table in ("robinhood_snapshots", "coinbase_ticks", "robinhood_diagnostics"):
                self._connection.execute(
                    f"UPDATE {table} SET schema_version = ? WHERE schema_version = 2",
                    (3,),
                )
            self._connection.execute(_QUOTE_TABLE_SQL)
            self._connection.execute(_QUOTE_EVENT_INDEX_SQL)
            self._connection.execute(_QUOTE_TICKER_INDEX_SQL)
            self._connection.execute(
                "UPDATE recorder_metadata SET value = ? WHERE key = 'schema_version'",
                ("3",),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _migrate_v3_to_v4(self) -> None:
        """Atomically add Kalshi-native lifecycle, settlement, and backfill state."""

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            old_tables = (
                "robinhood_snapshots",
                "coinbase_ticks",
                "robinhood_diagnostics",
                "prediction_market_quotes",
            )
            for table in old_tables:
                invalid = self._connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE schema_version != 3"
                ).fetchone()
                if invalid is None or invalid[0] != 0:
                    raise RecorderStorageError(f"mixed schema versions in {table}")
            for table in old_tables:
                self._connection.execute(
                    f"UPDATE {table} SET schema_version = ? WHERE schema_version = 3",
                    (SCHEMA_VERSION,),
                )
            for statement in (
                _KALSHI_MARKET_TABLE_SQL,
                _KALSHI_MARKET_INDEX_SQL,
                _KALSHI_MARKET_FOLLOWUP_INDEX_SQL,
                _KALSHI_SETTLEMENT_TABLE_SQL,
                _KALSHI_SETTLEMENT_INDEX_SQL,
                _KALSHI_SETTLEMENT_ASSET_INDEX_SQL,
                _KALSHI_NATIVE_QUOTE_TABLE_SQL,
                _KALSHI_NATIVE_QUOTE_INDEX_SQL,
                _KALSHI_NATIVE_QUOTE_ASSET_INDEX_SQL,
                _KALSHI_CONFLICT_TABLE_SQL,
                _KALSHI_BACKFILL_TABLE_SQL,
            ):
                self._connection.execute(statement)
            self._connection.execute(
                "UPDATE recorder_metadata SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def append_robinhood(self, contract: FifteenMinuteContract) -> bool:
        """Append one event snapshot; return false only for an exact duplicate."""

        if contract.fetched_at >= contract.end_time:
            raise ValueError("post-end observations cannot enter training snapshots")

        values: tuple[object, ...] = (
            SCHEMA_VERSION,
            contract.asset.value,
            contract.event_id,
            contract.contract_id,
            _timestamp(contract.start_time),
            _timestamp(contract.end_time),
            _timestamp(contract.fetched_at),
            max(0, int((contract.end_time - contract.fetched_at).total_seconds())),
            _decimal(contract.target_price),
            _decimal(contract.quote.yes_probability),
            _decimal(contract.quote.no_probability),
            contract.quote.availability.value,
            contract.lifecycle_state.value,
            contract.freshness_state.value,
            contract.venue,
            contract.settlement.benchmark,
            contract.settlement.method,
            contract.settlement.decimal_places,
            contract.settlement.source_url,
            contract.settlement.benchmark_data_url,
            contract.settlement.data_access.value,
            contract.settlement.access_notes,
            contract.settlement.role.value,
            contract.source_age_seconds,
            json.dumps(contract.venue_candidates, separators=(",", ":")),
            contract.source_url,
            contract.quote.role.value,
        )
        content_hash = _fingerprint(values[1:])
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO robinhood_snapshots (
                schema_version, asset, event_id, contract_id, start_time, end_time,
                fetched_timestamp, seconds_remaining, target_price, displayed_yes,
                displayed_no, quote_availability, lifecycle, freshness, venue,
                settlement_benchmark, settlement_method, settlement_decimal_places,
                settlement_source_url, settlement_benchmark_data_url,
                settlement_data_access, settlement_access_notes, settlement_data_role,
                source_age_seconds, venue_candidates, source_url, data_role, content_hash
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )
            """,
            (*values, content_hash),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def append_robinhood_diagnostic(
        self,
        *,
        kind: RecorderDiagnosticKind,
        asset: Asset,
        event_id: str,
        contract_id: str,
        observed_at: datetime,
        event_end_time: datetime,
        source_url: str,
        related_event_id: str | None = None,
    ) -> bool:
        """Append a diagnostic that is explicitly isolated from training snapshots."""

        existing = self._connection.execute(
            """
            SELECT 1 FROM robinhood_diagnostics
            WHERE kind = ? AND asset = ? AND event_id = ? LIMIT 1
            """,
            (kind.value, asset.value, event_id),
        ).fetchone()
        if existing is not None:
            return False

        values: tuple[object, ...] = (
            SCHEMA_VERSION,
            kind.value,
            asset.value,
            event_id,
            contract_id,
            _timestamp(observed_at),
            _timestamp(event_end_time),
            related_event_id,
            source_url,
        )
        content_hash = _fingerprint(values[1:])
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO robinhood_diagnostics (
                schema_version, kind, asset, event_id, contract_id,
                observed_timestamp, event_end_time, related_event_id,
                source_url, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, content_hash),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def append_coinbase(self, tick: MarketTick) -> bool:
        """Append one predictive tick without quantizing any Decimal value."""

        values: tuple[object, ...] = (
            SCHEMA_VERSION,
            _timestamp(tick.exchange_time) if tick.exchange_time is not None else None,
            _timestamp(tick.received_at),
            tick.symbol,
            _decimal(tick.price),
            _decimal(tick.bid),
            _decimal(tick.ask),
            _decimal(tick.spread),
            _decimal(tick.bid_size),
            _decimal(tick.ask_size),
            _decimal(tick.last_size),
            _decimal(tick.volume_24h),
            tick.role.value,
        )
        content_hash = _fingerprint(values[1:])
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO coinbase_ticks (
                schema_version, exchange_timestamp, received_timestamp, product,
                price, bid, ask, spread, bid_size, ask_size, last_size, volume_24h,
                data_role, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, content_hash),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def append_prediction_quote(self, quote: PredictionMarketQuote) -> bool:
        """Append a changed official quote; suppress only consecutive identical states."""

        yes_depth = json.dumps(
            [[str(level.price), str(level.quantity)] for level in quote.yes_bid_depth],
            separators=(",", ":"),
        )
        no_depth = json.dumps(
            [[str(level.price), str(level.quantity)] for level in quote.no_bid_depth],
            separators=(",", ":"),
        )
        evidence = json.dumps(quote.evidence_urls, separators=(",", ":"))
        state_values: tuple[object, ...] = (
            quote.asset.value,
            quote.robinhood_event_id,
            quote.robinhood_contract_id,
            quote.venue.value,
            quote.venue_series,
            quote.venue_ticker,
            quote.mapping_confidence.value,
            _decimal(quote.yes_bid),
            _decimal(quote.yes_ask),
            _decimal(quote.no_bid),
            _decimal(quote.no_ask),
            _decimal(quote.last_trade),
            _decimal(quote.volume),
            yes_depth,
            no_depth,
            quote.source,
            quote.freshness.value,
            quote.executability.value,
            evidence,
            quote.role.value,
        )
        content_hash = _fingerprint(state_values)
        latest = self._connection.execute(
            """
            SELECT content_hash FROM prediction_market_quotes
            WHERE robinhood_event_id = ? AND venue_ticker = ?
            ORDER BY received_timestamp DESC, id DESC LIMIT 1
            """,
            (quote.robinhood_event_id, quote.venue_ticker),
        ).fetchone()
        if latest is not None and latest["content_hash"] == content_hash:
            return False

        values: tuple[object, ...] = (
            SCHEMA_VERSION,
            *state_values[:7],
            _timestamp(quote.source_timestamp) if quote.source_timestamp is not None else None,
            quote.source_timestamp_kind.value,
            _timestamp(quote.received_timestamp),
            *state_values[7:],
        )
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO prediction_market_quotes (
                schema_version, asset, robinhood_event_id, robinhood_contract_id,
                venue, venue_series, venue_ticker, mapping_confidence,
                source_timestamp, source_timestamp_kind, received_timestamp,
                yes_bid, yes_ask, no_bid, no_ask, last_trade, volume,
                yes_bid_depth, no_bid_depth, source, freshness, executability,
                evidence_urls, data_role, content_hash
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (*values, content_hash),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def append_kalshi_market(self, market: KalshiMarket) -> bool:
        """Append one official lifecycle observation and immutable settlement if finalized."""

        identity: tuple[object, ...] = (
            market.asset.value,
            market.series,
            market.ticker,
            market.event_ticker,
            _timestamp(market.window_start),
            _timestamp(market.window_end),
            _decimal(market.target),
        )
        existing_identity = self._connection.execute(
            """
            SELECT asset, series, ticker, event_ticker, window_start, window_end, target
            FROM kalshi_market_lifecycle WHERE ticker=? ORDER BY id ASC LIMIT 1
            """,
            (market.ticker,),
        ).fetchone()
        if existing_identity is not None and tuple(existing_identity) != identity:
            raise RecorderStorageError(f"conflicting official market metadata for {market.ticker}")
        if market.settlement is not None:
            self.append_kalshi_settlement(market.settlement)

        state: tuple[object, ...] = (
            *identity,
            market.lifecycle.value,
            market.official_status,
            market.rules_primary,
            market.rules_secondary,
            market.settlement_timer_seconds,
            market.determination_result.value if market.determination_result else None,
        )
        content_hash = _fingerprint(state)
        latest = self._connection.execute(
            """
            SELECT content_hash FROM kalshi_market_lifecycle
            WHERE ticker=? ORDER BY fetched_timestamp DESC, id DESC LIMIT 1
            """,
            (market.ticker,),
        ).fetchone()
        if latest is not None and latest["content_hash"] == content_hash:
            return False
        values: tuple[object, ...] = (
            SCHEMA_VERSION,
            *identity,
            market.lifecycle.value,
            market.official_status,
            _timestamp(market.fetched_timestamp),
            market.source_url,
            market.rules_primary,
            market.rules_secondary,
            market.settlement_timer_seconds,
            market.determination_result.value if market.determination_result else None,
        )
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO kalshi_market_lifecycle (
                schema_version, asset, series, ticker, event_ticker, window_start,
                window_end, target, lifecycle, official_status, fetched_timestamp,
                source_url, rules_primary, rules_secondary, settlement_timer_seconds,
                determination_result, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, content_hash),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def append_kalshi_quote(self, quote: KalshiNativeQuote) -> bool:
        yes_depth = json.dumps(
            [[str(level.price), str(level.quantity)] for level in quote.yes_bid_depth],
            separators=(",", ":"),
        )
        no_depth = json.dumps(
            [[str(level.price), str(level.quantity)] for level in quote.no_bid_depth],
            separators=(",", ":"),
        )
        evidence = json.dumps(quote.evidence_urls, separators=(",", ":"))
        state: tuple[object, ...] = (
            quote.asset.value,
            quote.series,
            quote.ticker,
            quote.event_ticker,
            _decimal(quote.yes_bid),
            _decimal(quote.yes_ask),
            _decimal(quote.no_bid),
            _decimal(quote.no_ask),
            _decimal(quote.last_trade),
            _decimal(quote.volume),
            yes_depth,
            no_depth,
            quote.source,
            quote.freshness.value,
            quote.executability.value,
            evidence,
            quote.role.value,
        )
        content_hash = _fingerprint(state)
        latest = self._connection.execute(
            """
            SELECT content_hash FROM kalshi_prediction_quotes
            WHERE ticker=? ORDER BY received_timestamp DESC, id DESC LIMIT 1
            """,
            (quote.ticker,),
        ).fetchone()
        if latest is not None and latest["content_hash"] == content_hash:
            return False
        values: tuple[object, ...] = (
            SCHEMA_VERSION,
            *state[:4],
            _timestamp(quote.source_timestamp) if quote.source_timestamp is not None else None,
            quote.source_timestamp_kind.value,
            _timestamp(quote.received_timestamp),
            *state[4:],
        )
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO kalshi_prediction_quotes (
                schema_version, asset, series, ticker, event_ticker, source_timestamp,
                source_timestamp_kind, received_timestamp, yes_bid, yes_ask, no_bid,
                no_ask, last_trade, volume, yes_bid_depth, no_bid_depth, source,
                freshness, executability, evidence_urls, data_role, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, content_hash),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def append_kalshi_settlement(self, truth: KalshiSettlementTruth) -> bool:
        """Insert official truth once; record and loudly reject any later conflict."""

        lifecycle_identity = self._connection.execute(
            """
            SELECT asset, series, ticker, event_ticker, window_start, window_end, target
            FROM kalshi_market_lifecycle WHERE ticker=? ORDER BY id ASC LIMIT 1
            """,
            (truth.ticker,),
        ).fetchone()
        truth_identity: tuple[object, ...] = (
            truth.asset.value,
            truth.series,
            truth.ticker,
            truth.event_ticker,
            _timestamp(truth.window_start),
            _timestamp(truth.window_end),
            _decimal(truth.target),
        )
        if lifecycle_identity is not None and tuple(lifecycle_identity) != truth_identity:
            raise RecorderStorageError(
                f"settlement metadata does not match lifecycle for {truth.ticker}"
            )
        immutable: tuple[object, ...] = (
            *truth_identity,
            truth.result.value,
            _timestamp(truth.settlement_timestamp),
            _decimal(truth.settlement_value),
            truth.expiration_value,
        )
        content_hash = _fingerprint(immutable)
        existing = self._connection.execute(
            "SELECT content_hash FROM kalshi_settlements WHERE ticker = ?", (truth.ticker,)
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] == content_hash:
                return False
            self._connection.execute(
                """
                INSERT OR IGNORE INTO kalshi_settlement_conflicts (
                    schema_version, ticker, existing_hash, conflicting_hash,
                    observed_timestamp, source_url
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    SCHEMA_VERSION,
                    truth.ticker,
                    existing["content_hash"],
                    content_hash,
                    _timestamp(truth.fetched_timestamp),
                    truth.official_source,
                ),
            )
            self._connection.commit()
            raise RecorderStorageError(f"conflicting official settlement for {truth.ticker}")
        self._connection.execute(
            """
            INSERT INTO kalshi_settlements (
                schema_version, asset, series, ticker, event_ticker, window_start,
                window_end, target, result, settlement_timestamp, settlement_value,
                expiration_value, official_source, fetched_timestamp, data_role, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                SCHEMA_VERSION,
                *immutable,
                truth.official_source,
                _timestamp(truth.fetched_timestamp),
                truth.role.value,
                content_hash,
            ),
        )
        self._connection.commit()
        return True

    def save_backfill_state(
        self,
        *,
        series: str,
        source_path: str,
        start: datetime,
        end: datetime,
        next_cursor: str | None,
        complete: bool,
        updated_at: datetime,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO kalshi_backfill_state (
                series, source_path, range_start, range_end, next_cursor,
                updated_timestamp, complete
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(series, source_path, range_start, range_end) DO UPDATE SET
                next_cursor=excluded.next_cursor,
                updated_timestamp=excluded.updated_timestamp,
                complete=excluded.complete
            """,
            (
                series,
                source_path,
                _timestamp(start),
                _timestamp(end),
                next_cursor,
                _timestamp(updated_at),
                int(complete),
            ),
        )
        self._connection.commit()

    def load_backfill_cursor(
        self, *, series: str, source_path: str, start: datetime, end: datetime
    ) -> tuple[str | None, bool] | None:
        row = self._connection.execute(
            """
            SELECT next_cursor, complete FROM kalshi_backfill_state
            WHERE series=? AND source_path=? AND range_start=? AND range_end=?
            """,
            (series, source_path, _timestamp(start), _timestamp(end)),
        ).fetchone()
        return None if row is None else (row["next_cursor"], bool(row["complete"]))

    def replay_kalshi_markets(self, ticker: str) -> Iterator[KalshiMarketRecord]:
        rows = self._connection.execute(
            """
            SELECT * FROM kalshi_market_lifecycle
            WHERE ticker=? ORDER BY fetched_timestamp ASC, id ASC
            """,
            (ticker,),
        )
        for row in rows:
            yield self._kalshi_market_record(row)

    def replay_kalshi_quotes(self, ticker: str) -> Iterator[KalshiNativeQuoteRecord]:
        rows = self._connection.execute(
            """
            SELECT * FROM kalshi_prediction_quotes
            WHERE ticker=? ORDER BY received_timestamp ASC, id ASC
            """,
            (ticker,),
        )
        for row in rows:
            yield self._kalshi_native_quote_record(row)

    def latest_kalshi_states(
        self,
        *,
        window_end_at_or_after: datetime | None = None,
        window_end_before: datetime | None = None,
    ) -> tuple[KalshiMarketRecord, ...]:
        predicates: list[str] = []
        parameters: list[object] = []
        if window_end_at_or_after is not None:
            predicates.append("AND current.window_end >= ?")
            parameters.append(_timestamp(window_end_at_or_after))
        if window_end_before is not None:
            predicates.append("AND current.window_end < ?")
            parameters.append(_timestamp(window_end_before))
        predicate = "\n".join(predicates)
        rows = self._connection.execute(
            f"""
            SELECT current.* FROM kalshi_market_lifecycle AS current
            WHERE current.id = (
                SELECT latest.id FROM kalshi_market_lifecycle AS latest
                WHERE latest.ticker = current.ticker
                ORDER BY latest.fetched_timestamp DESC, latest.id DESC LIMIT 1
            )
            {predicate}
            ORDER BY current.ticker ASC
            """,
            tuple(parameters),
        )
        return tuple(self._kalshi_market_record(row) for row in rows)

    def latest_kalshi_state(self, ticker: str) -> KalshiMarketRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM kalshi_market_lifecycle WHERE ticker=?
            ORDER BY fetched_timestamp DESC, id DESC LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        return None if row is None else self._kalshi_market_record(row)

    def unsettled_kalshi_markets(
        self,
        *,
        now: datetime,
        asset: Asset | None = None,
        after_ticker: str | None = None,
        limit: int = 25,
    ) -> tuple[KalshiMarketRecord, ...]:
        """Return a bounded deterministic batch of closed markets without official truth."""

        if limit <= 0:
            raise ValueError("limit must be positive")
        rows = self._connection.execute(
            """
            SELECT current.* FROM kalshi_market_lifecycle AS current
            WHERE current.id = (
                SELECT latest.id FROM kalshi_market_lifecycle AS latest
                WHERE latest.ticker = current.ticker
                ORDER BY latest.fetched_timestamp DESC, latest.id DESC LIMIT 1
            )
              AND current.window_end <= ?
              AND current.lifecycle != 'invalid'
              AND NOT (
                  current.lifecycle IN ('settled_yes', 'settled_no')
                  AND EXISTS (
                      SELECT 1 FROM kalshi_settlements AS settled
                      WHERE settled.ticker = current.ticker
                  )
              )
              AND (? IS NULL OR current.asset = ?)
              AND (? IS NULL OR current.ticker > ?)
            ORDER BY current.ticker ASC LIMIT ?
            """,
            (
                _timestamp(now),
                asset.value if asset is not None else None,
                asset.value if asset is not None else None,
                after_ticker,
                after_ticker,
                limit,
            ),
        )
        return tuple(self._kalshi_market_record(row) for row in rows)

    def unsettled_kalshi_count(self, *, now: datetime) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) FROM kalshi_market_lifecycle AS current
            WHERE current.id = (
                SELECT latest.id FROM kalshi_market_lifecycle AS latest
                WHERE latest.ticker = current.ticker
                ORDER BY latest.fetched_timestamp DESC, latest.id DESC LIMIT 1
            )
              AND current.window_end <= ?
              AND current.lifecycle != 'invalid'
              AND NOT (
                  current.lifecycle IN ('settled_yes', 'settled_no')
                  AND EXISTS (
                      SELECT 1 FROM kalshi_settlements AS settled
                      WHERE settled.ticker = current.ticker
                  )
              )
            """,
            (_timestamp(now),),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def latest_native_cursors(self) -> tuple[dict[Asset, datetime], dict[str, datetime]]:
        """Recover last persisted quote and underlying receive timestamps."""

        quote_rows = self._connection.execute(
            """
            SELECT asset, MAX(received_timestamp) AS received_timestamp
            FROM kalshi_prediction_quotes GROUP BY asset
            """
        )
        tick_rows = self._connection.execute(
            """
            SELECT product, MAX(received_timestamp) AS received_timestamp
            FROM coinbase_ticks GROUP BY product
            """
        )
        quotes = {
            Asset(row["asset"]): _parse_timestamp(row["received_timestamp"], "received_timestamp")
            for row in quote_rows
        }
        ticks = {
            str(row["product"]): _parse_timestamp(row["received_timestamp"], "received_timestamp")
            for row in tick_rows
        }
        return quotes, ticks

    def latest_finalized_by_asset(self) -> dict[Asset, KalshiSettlementRecord]:
        rows = self._connection.execute(
            """
            SELECT settlement.* FROM kalshi_settlements AS settlement
            WHERE settlement.id = (
                SELECT latest.id FROM kalshi_settlements AS latest
                WHERE latest.asset = settlement.asset
                ORDER BY latest.settlement_timestamp DESC, latest.id DESC LIMIT 1
            )
            """
        )
        return {Asset(row["asset"]): self._kalshi_settlement_record(row) for row in rows}

    def settlement_counts_by_asset(self) -> dict[Asset, int]:
        counts = {asset: 0 for asset in Asset}
        for row in self._connection.execute(
            "SELECT asset, COUNT(*) AS count FROM kalshi_settlements GROUP BY asset"
        ):
            counts[Asset(row["asset"])] = int(row["count"])
        return counts

    def replay_kalshi_settlements(
        self, *, series: str | None = None, max_row_id: int | None = None
    ) -> Iterator[KalshiSettlementRecord]:
        if max_row_id is not None and max_row_id < 0:
            raise ValueError("max_row_id must be non-negative")
        if series is None:
            rows = self._connection.execute(
                """
                SELECT * FROM kalshi_settlements WHERE (? IS NULL OR id <= ?)
                ORDER BY window_start ASC, ticker ASC, id ASC
                """,
                (max_row_id, max_row_id),
            )
        else:
            rows = self._connection.execute(
                """
                SELECT * FROM kalshi_settlements
                WHERE series=? AND (? IS NULL OR id <= ?)
                ORDER BY window_start ASC, ticker ASC, id ASC
                """,
                (series, max_row_id, max_row_id),
            )
        for row in rows:
            yield self._kalshi_settlement_record(row)

    def join_training_label(
        self,
        ticker: str,
        decision_timestamp: datetime,
        *,
        market_max_row_id: int | None = None,
        quote_max_row_id: int | None = None,
        settlement_max_row_id: int | None = None,
    ) -> TrainingLabelExample:
        """Join only observations known at decision time; return truth solely as a label."""

        if decision_timestamp.tzinfo is None or decision_timestamp.utcoffset() is None:
            raise ValueError("decision timestamp must be timezone-aware")
        settlement_row = self._connection.execute(
            """
            SELECT * FROM kalshi_settlements
            WHERE ticker=? AND (? IS NULL OR id <= ?)
            """,
            (ticker, settlement_max_row_id, settlement_max_row_id),
        ).fetchone()
        if settlement_row is None:
            raise TrainingDataUnavailableError("official settlement label is unavailable")
        label = self._kalshi_settlement_record(settlement_row)
        decision = decision_timestamp.astimezone(UTC)
        if (
            decision < label.window_start
            or decision >= label.window_end
            or decision >= label.settlement_timestamp
        ):
            raise RecorderStorageError(
                "decision timestamp is not within the pre-settlement market window"
            )
        market_row = self._connection.execute(
            """
            SELECT * FROM kalshi_market_lifecycle
            WHERE ticker=? AND fetched_timestamp <= ?
              AND (? IS NULL OR id <= ?)
              AND determination_result IS NULL
              AND lifecycle NOT IN ('settled_yes', 'settled_no')
            ORDER BY fetched_timestamp DESC, id DESC LIMIT 1
            """,
            (ticker, _timestamp(decision), market_max_row_id, market_max_row_id),
        ).fetchone()
        if market_row is None:
            raise TrainingDataUnavailableError("no official metadata existed at decision time")
        market = self._kalshi_feature_market_record(market_row)
        if (
            market.asset is not label.asset
            or market.series != label.series
            or market.event_ticker != label.event_ticker
            or market.window_start != label.window_start
            or market.window_end != label.window_end
            or market.target != label.target
        ):
            raise RecorderStorageError("training metadata does not match settlement label")
        quote_rows = self._connection.execute(
            """
            SELECT * FROM kalshi_prediction_quotes
            WHERE ticker=? AND received_timestamp >= ? AND received_timestamp <= ?
              AND received_timestamp < ?
              AND (? IS NULL OR id <= ?)
            ORDER BY received_timestamp ASC, id ASC
            """,
            (
                ticker,
                _timestamp(label.window_start),
                _timestamp(decision),
                _timestamp(label.window_end),
                quote_max_row_id,
                quote_max_row_id,
            ),
        )
        observations = tuple(self._kalshi_native_quote_record(row) for row in quote_rows)
        if any(
            quote.asset is not label.asset
            or quote.series != label.series
            or quote.event_ticker != label.event_ticker
            for quote in observations
        ):
            raise RecorderStorageError("training quote does not match settlement label")
        return TrainingLabelExample(
            ticker=ticker,
            decision_timestamp=decision,
            market=market,
            observations=observations,
            label=label,
        )

    def replay_robinhood(self, event_id: str) -> Iterator[RobinhoodSnapshotRecord]:
        """Yield one event deterministically by timestamp and insertion id."""

        rows = self._connection.execute(
            """
            SELECT * FROM robinhood_snapshots
            WHERE event_id = ? AND fetched_timestamp < end_time
            ORDER BY fetched_timestamp ASC, id ASC
            """,
            (event_id,),
        )
        for row in rows:
            record = self._snapshot_record(row)
            if record.fetched_timestamp < record.end_time:
                yield record

    def replay_robinhood_diagnostics(self, event_id: str) -> Iterator[RobinhoodDiagnosticRecord]:
        """Yield non-training diagnostics for one event in deterministic order."""

        rows = self._connection.execute(
            """
            SELECT * FROM robinhood_diagnostics
            WHERE event_id = ? ORDER BY observed_timestamp ASC, id ASC
            """,
            (event_id,),
        )
        for row in rows:
            yield self._diagnostic_record(row)

    def open_rollover_gaps(self) -> dict[Asset, RobinhoodDiagnosticRecord]:
        """Reconstruct durable gaps that have no later matching end diagnostic."""

        rows = self._connection.execute(
            """
            SELECT started.* FROM robinhood_diagnostics AS started
            WHERE started.kind = ?
              AND NOT EXISTS (
                  SELECT 1 FROM robinhood_diagnostics AS ended
                  WHERE ended.kind = ?
                    AND ended.asset = started.asset
                    AND ended.event_id = started.event_id
                    AND ended.id > started.id
              )
            ORDER BY started.observed_timestamp ASC, started.id ASC
            """,
            (
                RecorderDiagnosticKind.ROLLOVER_GAP_STARTED.value,
                RecorderDiagnosticKind.ROLLOVER_GAP_ENDED.value,
            ),
        )
        result: dict[Asset, RobinhoodDiagnosticRecord] = {}
        for row in rows:
            record = self._diagnostic_record(row)
            result[record.asset] = record
        return result

    def replay_coinbase(
        self, product: str, *, max_row_id: int | None = None
    ) -> Iterator[CoinbaseTickRecord]:
        """Yield one Coinbase product deterministically by receive time and id."""

        rows = self._connection.execute(
            """
            SELECT * FROM coinbase_ticks
            WHERE product = ? AND (? IS NULL OR id <= ?)
            ORDER BY received_timestamp ASC, id ASC
            """,
            (product, max_row_id, max_row_id),
        )
        for row in rows:
            yield self._tick_record(row)

    def replay_coinbase_range(
        self,
        product: str,
        *,
        start: datetime,
        end: datetime,
        max_row_id: int | None = None,
    ) -> Iterator[CoinbaseTickRecord]:
        """Yield a bounded receive-time range under an immutable source snapshot."""

        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("Coinbase range start must be timezone-aware")
        if end.tzinfo is None or end.utcoffset() is None:
            raise ValueError("Coinbase range end must be timezone-aware")
        if start > end:
            raise ValueError("Coinbase range start must not follow end")
        rows = self._connection.execute(
            """
            SELECT * FROM coinbase_ticks
            WHERE product=? AND received_timestamp>=? AND received_timestamp<=?
              AND (? IS NULL OR id<=?)
            ORDER BY received_timestamp ASC,id ASC
            """,
            (
                product,
                _timestamp(start),
                _timestamp(end),
                max_row_id,
                max_row_id,
            ),
        )
        for row in rows:
            yield self._tick_record(row)

    def replay_prediction_quotes(self, event_id: str) -> Iterator[PredictionQuoteRecord]:
        """Yield official quotes deterministically by receive time and insertion id."""

        rows = self._connection.execute(
            """
            SELECT * FROM prediction_market_quotes
            WHERE robinhood_event_id = ? ORDER BY received_timestamp ASC, id ASC
            """,
            (event_id,),
        )
        for row in rows:
            yield self._prediction_quote_record(row)

    @staticmethod
    def _snapshot_record(row: sqlite3.Row) -> RobinhoodSnapshotRecord:
        from live15_quant.models import (  # avoid a long module-level enum list
            Asset,
            DataRole,
            FreshnessState,
            LifecycleState,
            SupportLevel,
        )

        try:
            target = _parse_decimal(row["target_price"], "target_price")
            if target is None or row["schema_version"] != SCHEMA_VERSION:
                raise RecorderStorageError("malformed Robinhood snapshot record")
            return RobinhoodSnapshotRecord(
                row_id=row["id"],
                schema_version=row["schema_version"],
                asset=Asset(row["asset"]),
                event_id=row["event_id"],
                contract_id=row["contract_id"],
                start_time=_parse_timestamp(row["start_time"], "start_time"),
                end_time=_parse_timestamp(row["end_time"], "end_time"),
                fetched_timestamp=_parse_timestamp(row["fetched_timestamp"], "fetched_timestamp"),
                seconds_remaining=row["seconds_remaining"],
                target_price=target,
                displayed_yes=_parse_decimal(row["displayed_yes"], "displayed_yes", optional=True),
                displayed_no=_parse_decimal(row["displayed_no"], "displayed_no", optional=True),
                quote_availability=SupportLevel(row["quote_availability"]),
                lifecycle=LifecycleState(row["lifecycle"]),
                freshness=FreshnessState(row["freshness"]),
                venue=row["venue"],
                settlement_benchmark=row["settlement_benchmark"],
                settlement_method=row["settlement_method"],
                settlement_decimal_places=row["settlement_decimal_places"],
                settlement_source_url=row["settlement_source_url"],
                settlement_benchmark_data_url=row["settlement_benchmark_data_url"],
                settlement_data_access=SupportLevel(row["settlement_data_access"]),
                settlement_access_notes=row["settlement_access_notes"],
                settlement_role=DataRole(row["settlement_data_role"]),
                source_age_seconds=row["source_age_seconds"],
                venue_candidates=_parse_string_tuple(row["venue_candidates"], "venue_candidates"),
                source_url=row["source_url"],
                role=DataRole(row["data_role"]),
            )
        except (ValueError, TypeError, AssertionError) as error:
            raise RecorderStorageError("malformed Robinhood snapshot record") from error

    @staticmethod
    def _tick_record(row: sqlite3.Row) -> CoinbaseTickRecord:
        from live15_quant.models import DataRole

        try:
            price = _parse_decimal(row["price"], "price")
            bid = _parse_decimal(row["bid"], "bid")
            ask = _parse_decimal(row["ask"], "ask")
            spread = _parse_decimal(row["spread"], "spread")
            if (
                price is None
                or bid is None
                or ask is None
                or spread is None
                or row["schema_version"] != SCHEMA_VERSION
            ):
                raise RecorderStorageError("malformed Coinbase tick record")
            return CoinbaseTickRecord(
                row_id=row["id"],
                schema_version=row["schema_version"],
                exchange_timestamp=(
                    _parse_timestamp(row["exchange_timestamp"], "exchange_timestamp")
                    if row["exchange_timestamp"] is not None
                    else None
                ),
                received_timestamp=_parse_timestamp(
                    row["received_timestamp"], "received_timestamp"
                ),
                product=row["product"],
                price=price,
                bid=bid,
                ask=ask,
                spread=spread,
                bid_size=_parse_decimal(row["bid_size"], "bid_size", optional=True),
                ask_size=_parse_decimal(row["ask_size"], "ask_size", optional=True),
                last_size=_parse_decimal(row["last_size"], "last_size", optional=True),
                volume_24h=_parse_decimal(row["volume_24h"], "volume_24h", optional=True),
                role=DataRole(row["data_role"]),
            )
        except (ValueError, TypeError, AssertionError) as error:
            raise RecorderStorageError("malformed Coinbase tick record") from error

    @staticmethod
    def _diagnostic_record(row: sqlite3.Row) -> RobinhoodDiagnosticRecord:
        try:
            if row["schema_version"] != SCHEMA_VERSION:
                raise RecorderStorageError("malformed Robinhood diagnostic record")
            return RobinhoodDiagnosticRecord(
                row_id=row["id"],
                schema_version=row["schema_version"],
                kind=RecorderDiagnosticKind(row["kind"]),
                asset=Asset(row["asset"]),
                event_id=row["event_id"],
                contract_id=row["contract_id"],
                observed_timestamp=_parse_timestamp(
                    row["observed_timestamp"], "observed_timestamp"
                ),
                event_end_time=_parse_timestamp(row["event_end_time"], "event_end_time"),
                related_event_id=row["related_event_id"],
                source_url=row["source_url"],
            )
        except (ValueError, TypeError, AssertionError) as error:
            raise RecorderStorageError("malformed Robinhood diagnostic record") from error

    @staticmethod
    def _prediction_quote_record(row: sqlite3.Row) -> PredictionQuoteRecord:
        from live15_quant.models import (
            DataRole,
            ExecutabilityClassification,
            FreshnessState,
            MappingConfidence,
            OrderBookLevel,
            SourceTimestampKind,
            Venue,
        )

        try:
            yes_depth = tuple(
                OrderBookLevel(
                    price=Decimal(price),
                    quantity=Decimal(quantity),
                )
                for price, quantity in _book_levels(row["yes_bid_depth"], "yes_bid_depth")
            )
            no_depth = tuple(
                OrderBookLevel(
                    price=Decimal(price),
                    quantity=Decimal(quantity),
                )
                for price, quantity in _book_levels(row["no_bid_depth"], "no_bid_depth")
            )
            if row["schema_version"] != SCHEMA_VERSION:
                raise RecorderStorageError("malformed prediction quote record")
            quote = PredictionMarketQuote(
                asset=Asset(row["asset"]),
                robinhood_event_id=row["robinhood_event_id"],
                robinhood_contract_id=row["robinhood_contract_id"],
                venue=Venue(row["venue"]),
                venue_series=row["venue_series"],
                venue_ticker=row["venue_ticker"],
                mapping_confidence=MappingConfidence(row["mapping_confidence"]),
                source_timestamp=(
                    _parse_timestamp(row["source_timestamp"], "source_timestamp")
                    if row["source_timestamp"] is not None
                    else None
                ),
                source_timestamp_kind=SourceTimestampKind(row["source_timestamp_kind"]),
                received_timestamp=_parse_timestamp(
                    row["received_timestamp"], "received_timestamp"
                ),
                yes_bid=_parse_decimal(row["yes_bid"], "yes_bid", optional=True),
                yes_ask=_parse_decimal(row["yes_ask"], "yes_ask", optional=True),
                no_bid=_parse_decimal(row["no_bid"], "no_bid", optional=True),
                no_ask=_parse_decimal(row["no_ask"], "no_ask", optional=True),
                last_trade=_parse_decimal(row["last_trade"], "last_trade", optional=True),
                volume=_parse_decimal(row["volume"], "volume", optional=True),
                yes_bid_depth=yes_depth,
                no_bid_depth=no_depth,
                source=row["source"],
                freshness=FreshnessState(row["freshness"]),
                executability=ExecutabilityClassification(row["executability"]),
                evidence_urls=_parse_string_tuple(row["evidence_urls"], "evidence_urls"),
            )
            role = DataRole(row["data_role"])
            if role is not quote.role:
                raise RecorderStorageError("malformed prediction quote record")
            return PredictionQuoteRecord(
                row_id=row["id"],
                schema_version=row["schema_version"],
                asset=quote.asset,
                robinhood_event_id=quote.robinhood_event_id,
                robinhood_contract_id=quote.robinhood_contract_id,
                venue=quote.venue,
                venue_series=quote.venue_series,
                venue_ticker=quote.venue_ticker,
                mapping_confidence=quote.mapping_confidence,
                source_timestamp=quote.source_timestamp,
                source_timestamp_kind=quote.source_timestamp_kind,
                received_timestamp=quote.received_timestamp,
                yes_bid=quote.yes_bid,
                yes_ask=quote.yes_ask,
                no_bid=quote.no_bid,
                no_ask=quote.no_ask,
                last_trade=quote.last_trade,
                volume=quote.volume,
                yes_bid_depth=quote.yes_bid_depth,
                no_bid_depth=quote.no_bid_depth,
                source=quote.source,
                freshness=quote.freshness,
                executability=quote.executability,
                evidence_urls=quote.evidence_urls,
                role=role,
            )
        except (ValueError, TypeError, AssertionError) as error:
            raise RecorderStorageError("malformed prediction quote record") from error

    @staticmethod
    def _kalshi_native_quote_record(row: sqlite3.Row) -> KalshiNativeQuoteRecord:
        from live15_quant.models import (
            ExecutabilityClassification,
            FreshnessState,
            OrderBookLevel,
            SourceTimestampKind,
        )

        try:
            yes_depth = tuple(
                OrderBookLevel(price=Decimal(price), quantity=Decimal(quantity))
                for price, quantity in _book_levels(row["yes_bid_depth"], "yes_bid_depth")
            )
            no_depth = tuple(
                OrderBookLevel(price=Decimal(price), quantity=Decimal(quantity))
                for price, quantity in _book_levels(row["no_bid_depth"], "no_bid_depth")
            )
            if row["schema_version"] != SCHEMA_VERSION:
                raise RecorderStorageError("malformed Kalshi-native quote record")
            quote = KalshiNativeQuote(
                asset=Asset(row["asset"]),
                series=row["series"],
                ticker=row["ticker"],
                event_ticker=row["event_ticker"],
                source_timestamp=(
                    _parse_timestamp(row["source_timestamp"], "source_timestamp")
                    if row["source_timestamp"] is not None
                    else None
                ),
                source_timestamp_kind=SourceTimestampKind(row["source_timestamp_kind"]),
                received_timestamp=_parse_timestamp(
                    row["received_timestamp"], "received_timestamp"
                ),
                yes_bid=_parse_decimal(row["yes_bid"], "yes_bid", optional=True),
                yes_ask=_parse_decimal(row["yes_ask"], "yes_ask", optional=True),
                no_bid=_parse_decimal(row["no_bid"], "no_bid", optional=True),
                no_ask=_parse_decimal(row["no_ask"], "no_ask", optional=True),
                last_trade=_parse_decimal(row["last_trade"], "last_trade", optional=True),
                volume=_parse_decimal(row["volume"], "volume", optional=True),
                yes_bid_depth=yes_depth,
                no_bid_depth=no_depth,
                source=row["source"],
                freshness=FreshnessState(row["freshness"]),
                executability=ExecutabilityClassification(row["executability"]),
                evidence_urls=_parse_string_tuple(row["evidence_urls"], "evidence_urls"),
            )
            role = DataRole(row["data_role"])
            if role is not quote.role:
                raise RecorderStorageError("malformed Kalshi-native quote record")
            return KalshiNativeQuoteRecord(
                row_id=row["id"],
                schema_version=row["schema_version"],
                asset=quote.asset,
                series=quote.series,
                ticker=quote.ticker,
                event_ticker=quote.event_ticker,
                source_timestamp=quote.source_timestamp,
                source_timestamp_kind=quote.source_timestamp_kind,
                received_timestamp=quote.received_timestamp,
                yes_bid=quote.yes_bid,
                yes_ask=quote.yes_ask,
                no_bid=quote.no_bid,
                no_ask=quote.no_ask,
                last_trade=quote.last_trade,
                volume=quote.volume,
                yes_bid_depth=quote.yes_bid_depth,
                no_bid_depth=quote.no_bid_depth,
                source=quote.source,
                freshness=quote.freshness,
                executability=quote.executability,
                evidence_urls=quote.evidence_urls,
                role=role,
            )
        except (ValueError, TypeError, AssertionError) as error:
            raise RecorderStorageError("malformed Kalshi-native quote record") from error

    @staticmethod
    def _kalshi_market_record(row: sqlite3.Row) -> KalshiMarketRecord:
        from live15_quant.kalshi_lifecycle import KalshiLifecycle, KalshiResult

        try:
            target = _parse_decimal(row["target"], "target")
            if target is None or row["schema_version"] != SCHEMA_VERSION:
                raise RecorderStorageError("malformed Kalshi market record")
            determination = row["determination_result"]
            market = KalshiMarket(
                asset=Asset(row["asset"]),
                series=row["series"],
                ticker=row["ticker"],
                event_ticker=row["event_ticker"],
                window_start=_parse_timestamp(row["window_start"], "window_start"),
                window_end=_parse_timestamp(row["window_end"], "window_end"),
                target=target,
                lifecycle=KalshiLifecycle(row["lifecycle"]),
                official_status=row["official_status"],
                fetched_timestamp=_parse_timestamp(row["fetched_timestamp"], "fetched_timestamp"),
                source_url=row["source_url"],
                rules_primary=row["rules_primary"],
                rules_secondary=row["rules_secondary"],
                settlement_timer_seconds=row["settlement_timer_seconds"],
                determination_result=(
                    KalshiResult(determination) if determination is not None else None
                ),
            )
            return KalshiMarketRecord(
                row_id=row["id"],
                schema_version=row["schema_version"],
                asset=market.asset,
                series=market.series,
                ticker=market.ticker,
                event_ticker=market.event_ticker,
                window_start=market.window_start,
                window_end=market.window_end,
                target=market.target,
                lifecycle=market.lifecycle,
                official_status=market.official_status,
                fetched_timestamp=market.fetched_timestamp,
                source_url=market.source_url,
                rules_primary=market.rules_primary,
                rules_secondary=market.rules_secondary,
                settlement_timer_seconds=market.settlement_timer_seconds,
                determination_result=market.determination_result,
            )
        except (ValueError, TypeError, AssertionError) as error:
            raise RecorderStorageError("malformed Kalshi market record") from error

    @staticmethod
    def _kalshi_feature_market_record(row: sqlite3.Row) -> KalshiFeatureMarketRecord:
        record = RecorderStore._kalshi_market_record(row)
        if record.determination_result is not None or record.lifecycle in {
            KalshiLifecycle.SETTLED_YES,
            KalshiLifecycle.SETTLED_NO,
        }:
            raise RecorderStorageError("result-bearing metadata cannot enter training features")
        return KalshiFeatureMarketRecord(
            row_id=record.row_id,
            schema_version=record.schema_version,
            asset=record.asset,
            series=record.series,
            ticker=record.ticker,
            event_ticker=record.event_ticker,
            window_start=record.window_start,
            window_end=record.window_end,
            target=record.target,
            lifecycle=record.lifecycle,
            official_status=record.official_status,
            fetched_timestamp=record.fetched_timestamp,
            source_url=record.source_url,
            rules_primary=record.rules_primary,
            rules_secondary=record.rules_secondary,
            settlement_timer_seconds=record.settlement_timer_seconds,
        )

    @staticmethod
    def _kalshi_settlement_record(row: sqlite3.Row) -> KalshiSettlementRecord:
        try:
            target = _parse_decimal(row["target"], "target")
            if target is None or row["schema_version"] != SCHEMA_VERSION:
                raise RecorderStorageError("malformed Kalshi settlement record")
            truth = KalshiSettlementTruth(
                asset=Asset(row["asset"]),
                series=row["series"],
                ticker=row["ticker"],
                event_ticker=row["event_ticker"],
                window_start=_parse_timestamp(row["window_start"], "window_start"),
                window_end=_parse_timestamp(row["window_end"], "window_end"),
                target=target,
                result=KalshiResult(row["result"]),
                settlement_timestamp=_parse_timestamp(
                    row["settlement_timestamp"], "settlement_timestamp"
                ),
                settlement_value=_parse_decimal(
                    row["settlement_value"], "settlement_value", optional=True
                ),
                expiration_value=row["expiration_value"],
                official_source=row["official_source"],
                fetched_timestamp=_parse_timestamp(row["fetched_timestamp"], "fetched_timestamp"),
            )
            role = DataRole(row["data_role"])
            if role is not truth.role:
                raise RecorderStorageError("malformed Kalshi settlement record")
            return KalshiSettlementRecord(
                row_id=row["id"],
                schema_version=row["schema_version"],
                asset=truth.asset,
                series=truth.series,
                ticker=truth.ticker,
                event_ticker=truth.event_ticker,
                window_start=truth.window_start,
                window_end=truth.window_end,
                target=truth.target,
                result=truth.result,
                settlement_timestamp=truth.settlement_timestamp,
                settlement_value=truth.settlement_value,
                expiration_value=truth.expiration_value,
                official_source=truth.official_source,
                fetched_timestamp=truth.fetched_timestamp,
                role=role,
            )
        except (ValueError, TypeError, AssertionError) as error:
            raise RecorderStorageError("malformed Kalshi settlement record") from error

    def count(self, table: str) -> int:
        if table not in {
            "robinhood_snapshots",
            "robinhood_diagnostics",
            "coinbase_ticks",
            "prediction_market_quotes",
            "kalshi_prediction_quotes",
            "kalshi_market_lifecycle",
            "kalshi_settlements",
            "kalshi_settlement_conflicts",
            "kalshi_backfill_state",
        }:
            raise ValueError("unknown recorder table")
        row = self._connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        if row is None:
            raise RecorderStorageError(f"could not count {table}")
        return int(row["count"])

    def row_counts(self) -> dict[str, int]:
        """Return bounded health counters for the training source-of-truth tables."""

        return {
            table: self.count(table)
            for table in (
                "kalshi_market_lifecycle",
                "kalshi_prediction_quotes",
                "coinbase_ticks",
                "kalshi_settlements",
                "kalshi_settlement_conflicts",
            )
        }

    def checkpoint(self) -> tuple[int, int, int]:
        """Run a non-blocking passive WAL checkpoint and return SQLite counters."""

        row = self._connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        if row is None:
            raise RecorderStorageError("WAL checkpoint returned no result")
        return int(row[0]), int(row[1]), int(row[2])

    def database_sizes(self) -> tuple[int, int]:
        database = self.path.stat().st_size if self.path.exists() else 0
        wal = self.path.with_name(f"{self.path.name}-wal")
        return database, wal.stat().st_size if wal.exists() else 0

    def training_source_snapshot(self) -> dict[str, object]:
        """Return path-free immutable row boundaries for a reproducible dataset build."""

        tables = (
            "coinbase_ticks",
            "kalshi_prediction_quotes",
            "kalshi_market_lifecycle",
            "kalshi_settlements",
        )
        snapshot: dict[str, object] = {"recorder_schema_version": SCHEMA_VERSION}
        for table in tables:
            row = self._connection.execute(
                f"SELECT COUNT(*) AS count, COALESCE(MAX(id), 0) AS max_id FROM {table}"
            ).fetchone()
            if row is None:
                raise RecorderStorageError(f"could not snapshot {table}")
            digest = hashlib.sha256()
            for content in self._connection.execute(
                f"SELECT id, content_hash FROM {table} WHERE id <= ? ORDER BY id ASC",
                (int(row["max_id"]),),
            ):
                digest.update(f"{content['id']}:{content['content_hash']}\n".encode())
            snapshot[table] = {
                "count": int(row["count"]),
                "max_id": int(row["max_id"]),
                "content_sha256": digest.hexdigest(),
            }
        return snapshot

    def integrity_check(self) -> str:
        row = self._connection.execute("PRAGMA integrity_check").fetchone()
        return "missing_result" if row is None else str(row[0])

    def quick_check(self) -> str:
        """Run SQLite's bounded-error structural check for periodic health monitoring."""

        row = self._connection.execute("PRAGMA quick_check(1)").fetchone()
        return "missing_result" if row is None else str(row[0])

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> RecorderStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
