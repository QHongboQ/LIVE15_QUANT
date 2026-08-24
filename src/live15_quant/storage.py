"""Crash-resilient append-only SQLite storage for recorder observations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from live15_quant.gaps import DataGap, GapReason, GapSource, GapStream
from live15_quant.kalshi_lifecycle import (
    KalshiLifecycle,
    KalshiMarket,
    KalshiResult,
    KalshiSettlementTruth,
)
from live15_quant.kalshi_ws import (
    KalshiBookSide,
    KalshiBookSyncStatus,
    KalshiCommandAcknowledged,
    KalshiOrderBookDelta,
    KalshiOrderBookMessage,
    KalshiOrderBookSnapshot,
    KalshiWsEventKind,
    SynchronizedKalshiOrderBook,
)
from live15_quant.models import (
    Asset,
    DataRole,
    FifteenMinuteContract,
    FreshnessState,
    KalshiNativeQuote,
    MarketTick,
    OrderBookLevel,
    PredictionMarketQuote,
    RecorderDiagnosticKind,
    RecorderEventSeverity,
    RecorderEventType,
    SecondaryPriceSemantics,
    SecondaryUnderlyingObservation,
    UnderlyingObservation,
    UnderlyingProvider,
)
from live15_quant.recorder_control import process_alive
from live15_quant.records import (
    SCHEMA_VERSION,
    CoinbaseTickRecord,
    KalshiFeatureMarketRecord,
    KalshiMarketRecord,
    KalshiNativeQuoteRecord,
    KalshiSettlementRecord,
    KalshiWsBookCheckpointRecord,
    KalshiWsOrderBookEventRecord,
    PredictionQuoteRecord,
    RecorderEventRecord,
    RobinhoodDiagnosticRecord,
    RobinhoodSnapshotRecord,
    SecondaryUnderlyingObservationRecord,
    TrainingLabelExample,
    UnderlyingObservationRecord,
)


class RecorderStorageError(RuntimeError):
    """Raised when persisted recorder data is invalid or incompatible."""


class ActiveRecorderAnalysisError(RecorderStorageError):
    """An unbounded analysis was attempted against the active writer database."""


class SettlementConflictError(RecorderStorageError):
    """Raised after preserving evidence of contradictory official settlement truth."""


class MarketIdentityConflictError(RecorderStorageError):
    """Raised when one official event/window is presented under conflicting identity."""


class DataGapConflictError(RecorderStorageError):
    """Raised when one logical gap is presented with contradictory facts."""


# Startup may only inspect a fixed, indexed recovery window per configured
# stream.  A larger unresolved backlog is an explicit operator-facing failure,
# never an unbounded scan or a silent loss of gap facts.
_MAX_ACTIVE_GAPS_PER_STREAM = 1_024


class TrainingDataUnavailableReason(StrEnum):
    OFFICIAL_SETTLEMENT_UNAVAILABLE = "official_settlement_unavailable"
    MISSING_DECISION_TIME_METADATA = "missing_decision_time_metadata"
    SOURCE_GAP_OVERLAP = "source_gap_overlap"
    RESTART_GAP = "restart_gap"
    RUNTIME_STALL_GAP = "runtime_stall_gap"
    STALE_SOURCE = "stale_source"
    INSUFFICIENT_LOOKBACK = "insufficient_lookback"
    SOURCE_UNAVAILABLE = "source_unavailable"
    MARKET_CLOSED = "market_closed"
    MARKET_SIDE_UNAVAILABLE = "market_side_unavailable"


class TrainingDataUnavailableError(RecorderStorageError):
    """Expected, machine-classified absence of required training truth."""

    def __init__(self, reason: TrainingDataUnavailableReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _compatible_record_version(value: object) -> bool:
    """v6-v10 only add tables/nullable timing; immutable v5 rows remain valid."""

    return value in {5, 6, 7, 8, 9, SCHEMA_VERSION}


class SecondaryAppendStatus(StrEnum):
    INSERTED = "inserted"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"


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


def _duration_milliseconds(start: datetime, end: datetime) -> Decimal:
    delta = end - start
    microseconds = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    return Decimal(microseconds) / Decimal(1000)


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

_KALSHI_SETTLEMENT_COUNT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kalshi_settlement_counts (
    asset TEXT PRIMARY KEY,
    count INTEGER NOT NULL CHECK (count >= 0)
) STRICT
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

_RECORDER_EVENT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS recorder_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    observed_timestamp TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info','warning','error','fatal')),
    event_type TEXT NOT NULL,
    asset TEXT,
    source TEXT,
    error_type TEXT,
    message TEXT NOT NULL,
    dedup_key TEXT UNIQUE
) STRICT
"""

_RECORDER_EVENT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_recorder_event_query
ON recorder_events(observed_timestamp DESC, severity, asset, source, id DESC)
"""

_UNDERLYING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS underlying_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    asset TEXT NOT NULL,
    provider TEXT NOT NULL,
    symbol TEXT NOT NULL,
    feed_id TEXT NOT NULL,
    price TEXT NOT NULL,
    source_timestamp TEXT NOT NULL,
    received_timestamp TEXT NOT NULL,
    confidence TEXT,
    provenance TEXT NOT NULL,
    freshness TEXT NOT NULL,
    data_role TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(provider, feed_id, source_timestamp, content_hash)
) STRICT
"""

_UNDERLYING_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_underlying_asset_provider_replay
ON underlying_observations(asset, provider, received_timestamp, id)
"""

_SECONDARY_UNDERLYING_LATEST_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_secondary_underlying_latest_source
ON secondary_underlying_observations(provider, instrument, source_timestamp DESC, id DESC)
"""

_SECONDARY_UNDERLYING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS secondary_underlying_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    asset TEXT NOT NULL,
    provider TEXT NOT NULL,
    instrument TEXT NOT NULL,
    price TEXT NOT NULL,
    price_semantics TEXT NOT NULL,
    bid TEXT,
    ask TEXT,
    source_timestamp TEXT NOT NULL,
    received_timestamp TEXT NOT NULL,
    persisted_timestamp TEXT,
    source_receive_latency_ms TEXT NOT NULL,
    receive_persist_latency_ms TEXT,
    provenance TEXT NOT NULL,
    freshness TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    data_role TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(provider, instrument, source_event_id)
) STRICT
"""

_SECONDARY_UNDERLYING_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_secondary_underlying_replay
ON secondary_underlying_observations(asset, provider, received_timestamp, id)
"""

_KALSHI_WS_EVENT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kalshi_ws_orderbook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    connection_id TEXT NOT NULL,
    subscription_id INTEGER NOT NULL CHECK (subscription_id > 0),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_kind TEXT NOT NULL,
    ticker TEXT,
    market_id TEXT,
    market_tickers TEXT NOT NULL,
    side TEXT,
    price TEXT,
    quantity_delta TEXT,
    yes_bids TEXT NOT NULL,
    no_bids TEXT NOT NULL,
    source_timestamp TEXT,
    socket_received_timestamp TEXT NOT NULL,
    enqueue_timestamp TEXT,
    parse_timestamp TEXT NOT NULL,
    persisted_timestamp TEXT,
    receive_enqueue_latency_ms TEXT,
    receive_persist_latency_ms TEXT,
    sync_status_after TEXT NOT NULL,
    provenance TEXT NOT NULL,
    data_role TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(connection_id, subscription_id, sequence)
) STRICT
"""

_KALSHI_WS_EVENT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_kalshi_ws_event_replay
ON kalshi_ws_orderbook_events(connection_id, subscription_id, id)
"""

_KALSHI_WS_EVENT_TICKER_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_kalshi_ws_event_ticker
ON kalshi_ws_orderbook_events(ticker, socket_received_timestamp, id)
"""

_KALSHI_WS_CHECKPOINT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kalshi_ws_book_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    connection_id TEXT NOT NULL,
    subscription_id INTEGER NOT NULL CHECK (subscription_id > 0),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    ticker TEXT NOT NULL,
    market_id TEXT NOT NULL,
    yes_bids TEXT NOT NULL,
    no_bids TEXT NOT NULL,
    source_timestamp TEXT,
    received_timestamp TEXT NOT NULL,
    persisted_timestamp TEXT NOT NULL,
    provenance TEXT NOT NULL,
    data_role TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(connection_id, subscription_id, sequence, ticker)
) STRICT
"""

_KALSHI_WS_CHECKPOINT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_kalshi_ws_checkpoint_replay
ON kalshi_ws_book_checkpoints(ticker, received_timestamp, id)
"""

_DATA_GAP_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS data_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    source TEXT NOT NULL,
    asset TEXT NOT NULL,
    instrument TEXT NOT NULL,
    gap_start TEXT NOT NULL,
    gap_end TEXT,
    duration_seconds TEXT,
    detected_at TEXT NOT NULL,
    threshold_seconds TEXT NOT NULL,
    reason TEXT NOT NULL,
    error_type TEXT,
    recovered INTEGER NOT NULL CHECK (recovered IN (0, 1)),
    recorder_session_id TEXT,
    incident_id TEXT,
    content_hash TEXT NOT NULL,
    CHECK ((recovered = 0 AND gap_end IS NULL AND duration_seconds IS NULL)
        OR (recovered = 1 AND gap_end IS NOT NULL AND duration_seconds IS NOT NULL)),
    UNIQUE(source, asset, instrument, gap_start, recovered)
) STRICT
"""

_DATA_GAP_REPLAY_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_data_gap_replay
ON data_gaps(source, asset, instrument, gap_start, recovered, gap_end, id)
"""

_DATA_GAP_OVERLAP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_data_gap_overlap
ON data_gaps(asset, source, recovered, gap_end, gap_start, id)
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
{_KALSHI_SETTLEMENT_COUNT_TABLE_SQL};
{_KALSHI_NATIVE_QUOTE_TABLE_SQL};
{_KALSHI_NATIVE_QUOTE_INDEX_SQL};
{_KALSHI_NATIVE_QUOTE_ASSET_INDEX_SQL};
{_KALSHI_CONFLICT_TABLE_SQL};
{_KALSHI_BACKFILL_TABLE_SQL};
{_RECORDER_EVENT_TABLE_SQL};
{_RECORDER_EVENT_INDEX_SQL};
{_UNDERLYING_TABLE_SQL};
{_UNDERLYING_INDEX_SQL};
{_SECONDARY_UNDERLYING_TABLE_SQL};
{_SECONDARY_UNDERLYING_INDEX_SQL};
{_SECONDARY_UNDERLYING_LATEST_INDEX_SQL};
{_KALSHI_WS_EVENT_TABLE_SQL};
{_KALSHI_WS_EVENT_INDEX_SQL};
{_KALSHI_WS_EVENT_TICKER_INDEX_SQL};
{_KALSHI_WS_CHECKPOINT_TABLE_SQL};
{_KALSHI_WS_CHECKPOINT_INDEX_SQL};
{_DATA_GAP_TABLE_SQL};
{_DATA_GAP_REPLAY_INDEX_SQL};
{_DATA_GAP_OVERLAP_INDEX_SQL};
"""


@lru_cache(maxsize=1)
def _expected_schema_objects() -> frozenset[tuple[str, str]]:
    """Parse the fixed DDL catalogue without replaying it during normal startup."""

    return frozenset(
        (kind.lower(), name)
        for kind, name in re.findall(
            r"CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX)\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_]\w*)",
            _SCHEMA,
            flags=re.IGNORECASE,
        )
    )


class RecorderStore:
    """Own a SQLite WAL database and expose insert-only recorder operations."""

    def __init__(
        self,
        path: Path,
        *,
        startup_phase_observer: Callable[[str, float], None] | None = None,
    ) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        phase_started = time.perf_counter()
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        try:
            if startup_phase_observer is not None:
                startup_phase_observer("db_open", time.perf_counter() - phase_started)
            phase_started = time.perf_counter()
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA wal_autocheckpoint=1000")
            self._connection.execute("PRAGMA journal_size_limit=67108864")
            if startup_phase_observer is not None:
                startup_phase_observer("wal_recovery", time.perf_counter() - phase_started)
            phase_started = time.perf_counter()
            self._initialize_or_migrate_schema()
            if startup_phase_observer is not None:
                startup_phase_observer("schema_check", time.perf_counter() - phase_started)
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
        if row is not None and row["value"] == "3":
            self._migrate_v3_to_v4()
            row = self._connection.execute(
                "SELECT value FROM recorder_metadata WHERE key = 'schema_version'"
            ).fetchone()
        if row is not None and row["value"] == "4":
            self._migrate_v4_to_v5()
            row = self._connection.execute(
                "SELECT value FROM recorder_metadata WHERE key = 'schema_version'"
            ).fetchone()
        if row is not None and row["value"] == "5":
            self._migrate_v5_to_v6()
            row = self._connection.execute(
                "SELECT value FROM recorder_metadata WHERE key = 'schema_version'"
            ).fetchone()
        if row is not None and row["value"] == "6":
            self._migrate_v6_to_v7()
            row = self._connection.execute(
                "SELECT value FROM recorder_metadata WHERE key = 'schema_version'"
            ).fetchone()
        if row is not None and row["value"] == "7":
            self._migrate_v7_to_v8()
            row = self._connection.execute(
                "SELECT value FROM recorder_metadata WHERE key = 'schema_version'"
            ).fetchone()
        if row is not None and row["value"] == "8":
            self._migrate_v8_to_v9()
            row = self._connection.execute(
                "SELECT value FROM recorder_metadata WHERE key = 'schema_version'"
            ).fetchone()
        if row is not None and row["value"] == "9" and SCHEMA_VERSION == 10:
            self._migrate_v9_to_v10()
            self._ensure_schema_objects()
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
        actual_objects = frozenset(
            (str(row[0]), str(row[1]))
            for row in self._connection.execute(
                "SELECT type,name FROM sqlite_master "
                "WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'"
            )
        )
        missing = _expected_schema_objects() - actual_objects
        if missing:
            names = ",".join(sorted(name for _kind, name in missing))
            raise RecorderStorageError(
                f"database schema objects are missing ({names}); offline repair is required"
            )

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
                    (4,),
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
                ("4",),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _migrate_v4_to_v5(self) -> None:
        """Atomically add bounded operational diagnostics without changing raw truth."""

        versioned_tables = (
            "robinhood_snapshots",
            "coinbase_ticks",
            "robinhood_diagnostics",
            "prediction_market_quotes",
            "kalshi_market_lifecycle",
            "kalshi_settlements",
            "kalshi_prediction_quotes",
            "kalshi_settlement_conflicts",
        )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for table in versioned_tables:
                invalid = self._connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE schema_version != 4"
                ).fetchone()
                if invalid is None or invalid[0] != 0:
                    raise RecorderStorageError(f"mixed schema versions in {table}")
            for table in versioned_tables:
                self._connection.execute(
                    f"UPDATE {table} SET schema_version = ? WHERE schema_version = 4",
                    (5,),
                )
            self._connection.execute(_RECORDER_EVENT_TABLE_SQL)
            self._connection.execute(_RECORDER_EVENT_INDEX_SQL)
            self._connection.execute(
                "UPDATE recorder_metadata SET value = ? WHERE key = 'schema_version'",
                ("5",),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _migrate_v5_to_v6(self) -> None:
        """Atomically add provider-neutral observations without rewriting raw truth."""

        versioned_tables = (
            "robinhood_snapshots",
            "coinbase_ticks",
            "robinhood_diagnostics",
            "prediction_market_quotes",
            "kalshi_market_lifecycle",
            "kalshi_settlements",
            "kalshi_prediction_quotes",
            "kalshi_settlement_conflicts",
            "recorder_events",
        )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for table in versioned_tables:
                invalid = self._connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE schema_version != 5"
                ).fetchone()
                if invalid is None or invalid[0] != 0:
                    raise RecorderStorageError(f"mixed schema versions in {table}")
            self._connection.execute(_UNDERLYING_TABLE_SQL)
            self._connection.execute(_UNDERLYING_INDEX_SQL)
            self._connection.execute(
                "UPDATE recorder_metadata SET value=? WHERE key='schema_version'",
                ("6",),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _migrate_v6_to_v7(self) -> None:
        """Atomically add secondary predictive observations without rewriting truth."""

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(_SECONDARY_UNDERLYING_TABLE_SQL)
            self._connection.execute(_SECONDARY_UNDERLYING_INDEX_SQL)
            self._connection.execute(_SECONDARY_UNDERLYING_LATEST_INDEX_SQL)
            self._connection.execute(
                "UPDATE recorder_metadata SET value=? WHERE key='schema_version'",
                ("7",),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _migrate_v7_to_v8(self) -> None:
        """Atomically add raw WS events and sparse synchronized checkpoints."""

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for statement in (
                _KALSHI_WS_EVENT_TABLE_SQL,
                _KALSHI_WS_EVENT_INDEX_SQL,
                _KALSHI_WS_EVENT_TICKER_INDEX_SQL,
                _KALSHI_WS_CHECKPOINT_TABLE_SQL,
                _KALSHI_WS_CHECKPOINT_INDEX_SQL,
            ):
                self._connection.execute(statement)
            self._connection.execute(
                "UPDATE recorder_metadata SET value=? WHERE key='schema_version'",
                ("8",),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _migrate_v8_to_v9(self) -> None:
        """Atomically add immutable data-gap facts without rewriting raw observations."""

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for statement in (
                _DATA_GAP_TABLE_SQL,
                _DATA_GAP_REPLAY_INDEX_SQL,
                _DATA_GAP_OVERLAP_INDEX_SQL,
            ):
                self._connection.execute(statement)
            self._connection.execute(
                "UPDATE recorder_metadata SET value=? WHERE key='schema_version'",
                ("9",),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _migrate_v9_to_v10(self) -> None:
        """Add optional local queue timing without inventing values for old rows."""

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(kalshi_ws_orderbook_events)")
            }
            if "enqueue_timestamp" not in columns:
                self._connection.execute(
                    "ALTER TABLE kalshi_ws_orderbook_events ADD COLUMN enqueue_timestamp TEXT"
                )
            if "receive_enqueue_latency_ms" not in columns:
                self._connection.execute(
                    "ALTER TABLE kalshi_ws_orderbook_events "
                    "ADD COLUMN receive_enqueue_latency_ms TEXT"
                )
            self._connection.execute(
                "UPDATE recorder_metadata SET value='10' WHERE key='schema_version'"
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def append_data_gap(self, gap: DataGap) -> bool:
        """Append one gap-state fact idempotently; contradictory facts fail loudly."""

        try:
            inserted = self._append_data_gap_uncommitted(gap)
            self._connection.commit()
            return inserted
        except Exception:
            self._connection.rollback()
            raise

    def append_data_gaps(self, gaps: Sequence[DataGap]) -> int:
        """Materialize a deterministic detector result in one atomic transaction."""

        ordered = sorted(
            gaps,
            key=lambda gap: (
                gap.gap_start,
                gap.source.value,
                gap.asset.value,
                gap.instrument,
                gap.recovered,
                gap.gap_end or datetime.max.replace(tzinfo=UTC),
            ),
        )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            inserted = sum(self._append_data_gap_uncommitted(gap) for gap in ordered)
            self._connection.commit()
            return inserted
        except Exception:
            self._connection.rollback()
            raise

    def _append_data_gap_uncommitted(self, gap: DataGap) -> bool:
        """Insert one gap inside the caller's transaction."""

        values: tuple[object, ...] = (
            SCHEMA_VERSION,
            gap.source.value,
            gap.asset.value,
            gap.instrument,
            _timestamp(gap.gap_start),
            None if gap.gap_end is None else _timestamp(gap.gap_end),
            None if gap.duration_seconds is None else str(gap.duration_seconds),
            _timestamp(gap.detected_at),
            str(gap.threshold_seconds),
            gap.reason.value,
            gap.error_type,
            int(gap.recovered),
            gap.recorder_session_id,
            gap.incident_id,
        )
        # Detection time is provenance for the first materialization, not part
        # of the immutable interval identity. Re-running the detector later is
        # therefore idempotent while contradictory reason/threshold facts fail.
        content_hash = _fingerprint((*values[1:7], *values[8:]))
        existing = self._connection.execute(
            """SELECT content_hash FROM data_gaps
            WHERE source=? AND asset=? AND instrument=? AND gap_start=? AND recovered=?""",
            (values[1], values[2], values[3], values[4], values[11]),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] == content_hash:
                return False
            raise DataGapConflictError("conflicting facts for an existing data gap")
        self._connection.execute(
            """INSERT INTO data_gaps(
            schema_version,source,asset,instrument,gap_start,gap_end,duration_seconds,
            detected_at,threshold_seconds,reason,error_type,recovered,recorder_session_id,
            incident_id,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (*values, content_hash),
        )
        return True

    def replay_data_gaps(
        self,
        *,
        source: GapSource | None = None,
        asset: Asset | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        max_row_id: int | None = None,
    ) -> tuple[DataGap, ...]:
        """Replay gaps deterministically, optionally restricted to one overlap range."""

        clauses: list[str] = []
        parameters: list[object] = []
        if source is not None:
            clauses.append("source=?")
            parameters.append(source.value)
        if asset is not None:
            clauses.append("asset=?")
            parameters.append(asset.value)
        if start is not None:
            visible_recovery = ""
            if max_row_id is not None:
                visible_recovery = " AND closed.id<=?"
            clauses.append(
                "(gap_end>? OR (gap_end IS NULL AND NOT EXISTS ("
                "SELECT 1 FROM data_gaps AS closed "
                "WHERE closed.source=data_gaps.source "
                "AND closed.asset=data_gaps.asset "
                "AND closed.instrument=data_gaps.instrument "
                "AND closed.gap_start=data_gaps.gap_start "
                f"AND closed.recovered=1{visible_recovery})))"
            )
            parameters.append(_timestamp(start))
            if max_row_id is not None:
                parameters.append(max_row_id)
        if end is not None:
            clauses.append("gap_start<?")
            parameters.append(_timestamp(end))
        if max_row_id is not None:
            clauses.append("id<=?")
            parameters.append(max_row_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM data_gaps {where} "
            "ORDER BY gap_start,source,asset,instrument,recovered,gap_end,id",
            tuple(parameters),
        )
        return tuple(self._data_gap(row) for row in rows)

    def latest_gap_stream_timestamp(
        self, source: GapSource, asset: Asset, instrument: str
    ) -> datetime | None:
        """Return one indexed cursor used to recover live gap tracking after restart."""

        if source is GapSource.KALSHI_REST:
            row = self._connection.execute(
                "SELECT received_timestamp FROM kalshi_prediction_quotes WHERE asset=? "
                "ORDER BY received_timestamp DESC,id DESC LIMIT 1",
                (asset.value,),
            ).fetchone()
        elif source is GapSource.KALSHI_WS:
            latest_window = self._connection.execute(
                """SELECT window_end FROM kalshi_market_lifecycle
                ORDER BY window_end DESC,id DESC LIMIT 1"""
            ).fetchone()
            if latest_window is None:
                return None
            window_end = _parse_timestamp(latest_window["window_end"], "window_end")
            ticker_rows = self._connection.execute(
                """SELECT ticker,MAX(window_end) AS latest_window,MAX(id) AS latest_id
                FROM kalshi_market_lifecycle
                WHERE window_end>=? AND window_end<=? AND asset=? GROUP BY ticker
                ORDER BY latest_window DESC,latest_id DESC LIMIT 4""",
                (
                    _timestamp(window_end - timedelta(hours=2)),
                    _timestamp(window_end),
                    asset.value,
                ),
            )
            candidates = []
            for ticker_row in ticker_rows:
                candidate = self._connection.execute(
                    """SELECT socket_received_timestamp AS received_timestamp
                    FROM kalshi_ws_orderbook_events WHERE ticker=?
                    ORDER BY socket_received_timestamp DESC,id DESC LIMIT 1""",
                    (ticker_row["ticker"],),
                ).fetchone()
                if candidate is not None:
                    candidates.append(candidate)
            row = (
                None
                if not candidates
                else max(candidates, key=lambda item: str(item["received_timestamp"]))
            )
        elif source is GapSource.COINBASE:
            row = self._connection.execute(
                "SELECT received_timestamp FROM coinbase_ticks WHERE product=? "
                "ORDER BY received_timestamp DESC,id DESC LIMIT 1",
                (instrument,),
            ).fetchone()
        elif source is GapSource.PYTH:
            row = self._connection.execute(
                "SELECT received_timestamp FROM underlying_observations "
                "WHERE asset=? AND provider=? ORDER BY received_timestamp DESC,id DESC LIMIT 1",
                (asset.value, UnderlyingProvider.PYTH_HERMES.value),
            ).fetchone()
        else:
            provider = (
                UnderlyingProvider.BINANCE_SPOT
                if source is GapSource.BINANCE
                else UnderlyingProvider.HYPERLIQUID_PERP
            )
            row = self._connection.execute(
                "SELECT received_timestamp FROM secondary_underlying_observations "
                "WHERE asset=? AND provider=? ORDER BY received_timestamp DESC,id DESC LIMIT 1",
                (asset.value, provider.value),
            ).fetchone()
        return (
            None
            if row is None
            else _parse_timestamp(row["received_timestamp"], "received_timestamp")
        )

    def active_data_gaps(self, streams: Sequence[GapStream] | None = None) -> tuple[DataGap, ...]:
        """Return bounded append-only OPEN facts not followed by recovery.

        More than one historical OPEN fact can legitimately remain for a stream
        after repeated interrupted recoveries.  They are distinct immutable
        facts, not a conflict to collapse or overwrite.  Callers must recover
        each fact from a later real observation.  The per-stream cap keeps
        startup recovery bounded and fails loudly if an operator intervention is
        required.
        """

        if streams is not None:
            active: list[DataGap] = []
            for stream in streams:
                rows = tuple(
                    self._connection.execute(
                        """SELECT open.* FROM data_gaps AS open
                        WHERE open.source=? AND open.asset=? AND open.instrument=?
                          AND open.recovered=0 AND NOT EXISTS (
                            SELECT 1 FROM data_gaps AS closed
                            WHERE closed.source=open.source AND closed.asset=open.asset
                              AND closed.instrument=open.instrument
                              AND closed.gap_start=open.gap_start AND closed.recovered=1
                        )
                        ORDER BY open.gap_start DESC,open.id DESC LIMIT ?""",
                        (
                            stream.source.value,
                            stream.asset.value,
                            stream.instrument,
                            _MAX_ACTIVE_GAPS_PER_STREAM + 1,
                        ),
                    )
                )
                if len(rows) > _MAX_ACTIVE_GAPS_PER_STREAM:
                    raise RecorderStorageError("active gap recovery backlog exceeds bounded limit")
                active.extend(self._data_gap(row) for row in rows)
            return tuple(
                sorted(
                    active,
                    key=lambda gap: (gap.gap_start, gap.source.value, gap.asset.value),
                )
            )
        rows = self._connection.execute(
            """SELECT open.* FROM data_gaps AS open
            WHERE open.recovered=0 AND NOT EXISTS (
                SELECT 1 FROM data_gaps AS closed
                WHERE closed.source=open.source AND closed.asset=open.asset
                  AND closed.instrument=open.instrument AND closed.gap_start=open.gap_start
                  AND closed.recovered=1
            )
            ORDER BY open.gap_start,open.source,open.asset,open.instrument,open.id"""
        )
        return tuple(self._data_gap(row) for row in rows)

    @staticmethod
    def _data_gap(row: sqlite3.Row) -> DataGap:
        try:
            duration = _parse_decimal(row["duration_seconds"], "duration_seconds", optional=True)
            threshold = _parse_decimal(row["threshold_seconds"], "threshold_seconds")
            assert threshold is not None
            gap = DataGap(
                source=GapSource(row["source"]),
                asset=Asset(row["asset"]),
                instrument=str(row["instrument"]),
                gap_start=_parse_timestamp(row["gap_start"], "gap_start"),
                gap_end=(
                    None if row["gap_end"] is None else _parse_timestamp(row["gap_end"], "gap_end")
                ),
                detected_at=_parse_timestamp(row["detected_at"], "detected_at"),
                threshold_seconds=threshold,
                reason=GapReason(row["reason"]),
                error_type=None if row["error_type"] is None else str(row["error_type"]),
                recovered=bool(row["recovered"]),
                recorder_session_id=(
                    None if row["recorder_session_id"] is None else str(row["recorder_session_id"])
                ),
                incident_id=None if row["incident_id"] is None else str(row["incident_id"]),
            )
            if gap.duration_seconds != duration:
                raise RecorderStorageError("data-gap duration does not match its timestamps")
            return gap
        except (ValueError, TypeError, AssertionError) as error:
            raise RecorderStorageError("malformed data-gap record") from error

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

    def append_underlying(self, observation: UnderlyingObservation) -> bool:
        """Append one provider-specific observation without Decimal quantization."""

        values: tuple[object, ...] = (
            SCHEMA_VERSION,
            observation.asset.value,
            observation.provider.value,
            observation.symbol,
            observation.feed_id,
            _decimal(observation.price),
            _timestamp(observation.source_timestamp),
            _timestamp(observation.received_timestamp),
            _decimal(observation.confidence),
            observation.provenance,
            observation.freshness.value,
            observation.role.value,
        )
        # Receive time, derived freshness, and delivery endpoint are not a new provider
        # fact. The same SSE update later seen through REST fallback remains idempotent,
        # while a changed price/confidence in the same publish second is retained.
        content_hash = _fingerprint(
            (
                observation.asset.value,
                observation.provider.value,
                observation.symbol,
                observation.feed_id,
                _decimal(observation.price),
                _timestamp(observation.source_timestamp),
                _decimal(observation.confidence),
                observation.role.value,
            )
        )
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO underlying_observations (
                schema_version,asset,provider,symbol,feed_id,price,
                source_timestamp,received_timestamp,confidence,provenance,
                freshness,data_role,content_hash
            ) SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?
            WHERE NOT EXISTS (
                SELECT 1 FROM underlying_observations
                WHERE provider=? AND feed_id=? AND source_timestamp=? AND price=?
                  AND confidence IS ?
            )
            """,
            (
                *values,
                content_hash,
                observation.provider.value,
                observation.feed_id,
                _timestamp(observation.source_timestamp),
                _decimal(observation.price),
                _decimal(observation.confidence),
            ),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def append_secondary_underlying(
        self, observation: SecondaryUnderlyingObservation
    ) -> SecondaryAppendStatus:
        """Append one venue-native secondary observation without blending primaries."""

        source_timestamp = _timestamp(observation.source_timestamp)
        received_timestamp = _timestamp(observation.received_timestamp)
        immutable = (
            observation.asset.value,
            observation.provider.value,
            observation.instrument,
            _decimal(observation.price),
            observation.price_semantics.value,
            _decimal(observation.bid),
            _decimal(observation.ask),
            source_timestamp,
            observation.source_event_id,
            observation.role.value,
        )
        content_hash = _fingerprint(immutable)
        existing = self._connection.execute(
            """SELECT content_hash FROM secondary_underlying_observations
            WHERE provider=? AND instrument=? AND source_event_id=?""",
            (
                observation.provider.value,
                observation.instrument,
                observation.source_event_id,
            ),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != content_hash:
                raise RecorderStorageError(
                    "conflicting secondary observation for provider event identity"
                )
            return SecondaryAppendStatus.DUPLICATE
        latest = self._connection.execute(
            """SELECT source_timestamp FROM secondary_underlying_observations
            WHERE provider=? AND instrument=?
            ORDER BY source_timestamp DESC,id DESC LIMIT 1""",
            (observation.provider.value, observation.instrument),
        ).fetchone()
        if latest is not None and source_timestamp < latest["source_timestamp"]:
            return SecondaryAppendStatus.OUT_OF_ORDER

        source_receive_ms = _duration_milliseconds(
            observation.source_timestamp, observation.received_timestamp
        )
        started = time.perf_counter_ns()
        cursor = self._connection.execute(
            """INSERT INTO secondary_underlying_observations(
                schema_version,asset,provider,instrument,price,price_semantics,bid,ask,
                source_timestamp,received_timestamp,persisted_timestamp,
                source_receive_latency_ms,receive_persist_latency_ms,provenance,
                freshness,source_event_id,data_role,content_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                SCHEMA_VERSION,
                observation.asset.value,
                observation.provider.value,
                observation.instrument,
                _decimal(observation.price),
                observation.price_semantics.value,
                _decimal(observation.bid),
                _decimal(observation.ask),
                source_timestamp,
                received_timestamp,
                None,
                str(source_receive_ms),
                None,
                observation.provenance,
                observation.freshness.value,
                observation.source_event_id,
                observation.role.value,
                content_hash,
            ),
        )
        self._connection.commit()
        persisted = datetime.now(UTC)
        persist_latency_ms = Decimal(time.perf_counter_ns() - started) / Decimal(1_000_000)
        self._connection.execute(
            """UPDATE secondary_underlying_observations
            SET persisted_timestamp=?,receive_persist_latency_ms=? WHERE id=?""",
            (_timestamp(persisted), str(persist_latency_ms), cursor.lastrowid),
        )
        self._connection.commit()
        return SecondaryAppendStatus.INSERTED

    def append_kalshi_ws_orderbook_event(
        self,
        message: KalshiOrderBookMessage | KalshiCommandAcknowledged,
        *,
        sync_status_after: KalshiBookSyncStatus,
    ) -> bool:
        """Persist one raw WS message; conflicting sequence facts fail loudly."""

        row_id = self.stage_kalshi_ws_orderbook_event(message, sync_status_after=sync_status_after)
        if row_id is None:
            return False
        self.finalize_kalshi_ws_orderbook_events(((row_id, message.socket_received_monotonic_ns),))
        return True

    def append_kalshi_ws_orderbook_event_batch(
        self,
        events: Sequence[
            tuple[KalshiOrderBookMessage | KalshiCommandAcknowledged, KalshiBookSyncStatus]
        ],
    ) -> tuple[int, Decimal | None]:
        """Persist a bounded verified batch with one duplicate lookup and one transaction."""

        if not events:
            return 0, None
        if len(events) > 1024:
            raise RecorderStorageError("Kalshi WS persistence batch exceeds bounded capacity")
        prepared: dict[tuple[str, int, int], tuple[tuple[object, ...], str, int | None]] = {}
        for message, sync_status_after in events:
            acknowledgement = message if isinstance(message, KalshiCommandAcknowledged) else None
            if acknowledgement is not None and (
                acknowledgement.subscription_id is None or acknowledgement.sequence is None
            ):
                raise RecorderStorageError(
                    "unsequenced Kalshi WS acknowledgement is not replay state"
                )
            snapshot = message if isinstance(message, KalshiOrderBookSnapshot) else None
            delta = message if isinstance(message, KalshiOrderBookDelta) else None
            event_kind = (
                KalshiWsEventKind.SUBSCRIPTION_ACK
                if acknowledgement is not None
                else KalshiWsEventKind.SNAPSHOT
                if snapshot is not None
                else KalshiWsEventKind.DELTA
            )
            yes_bids = json.dumps(
                [[str(level.price), str(level.quantity)] for level in snapshot.yes_bids]
                if snapshot is not None
                else [],
                separators=(",", ":"),
            )
            no_bids = json.dumps(
                [[str(level.price), str(level.quantity)] for level in snapshot.no_bids]
                if snapshot is not None
                else [],
                separators=(",", ":"),
            )
            market_tickers = json.dumps(
                acknowledgement.market_tickers if acknowledgement is not None else (),
                separators=(",", ":"),
            )
            immutable: tuple[object, ...] = (
                message.connection_id,
                message.subscription_id,
                message.sequence,
                event_kind.value,
                message.ticker if acknowledgement is None else None,
                message.market_id if acknowledgement is None else None,
                market_tickers,
                delta.side.value if delta is not None else None,
                _decimal(delta.price) if delta is not None else None,
                _decimal(delta.quantity_delta) if delta is not None else None,
                yes_bids,
                no_bids,
                (
                    _timestamp(message.source_timestamp)
                    if acknowledgement is None and message.source_timestamp is not None
                    else None
                ),
                message.provenance,
                message.role.value,
            )
            content_hash = _fingerprint(immutable)
            enqueue_latency = (
                Decimal(message.enqueue_monotonic_ns - message.socket_received_monotonic_ns)
                / Decimal(1_000_000)
                if message.enqueue_monotonic_ns is not None
                and message.socket_received_monotonic_ns is not None
                else None
            )
            if enqueue_latency is not None and enqueue_latency < 0:
                raise RecorderStorageError("Kalshi WS enqueue timing is not monotonic")
            key = (
                message.connection_id,
                int(message.subscription_id),
                int(message.sequence),
            )
            values: tuple[object, ...] = (
                SCHEMA_VERSION,
                *immutable[:13],
                _timestamp(message.socket_received_timestamp),
                (
                    _timestamp(message.enqueue_timestamp)
                    if message.enqueue_timestamp is not None
                    else None
                ),
                _timestamp(message.parse_timestamp),
                None,
                str(enqueue_latency) if enqueue_latency is not None else None,
                None,
                sync_status_after.value,
                *immutable[13:],
                content_hash,
            )
            prior = prepared.get(key)
            if prior is not None:
                if prior[1] != content_hash:
                    raise RecorderStorageError(
                        "conflicting Kalshi WS fact inside persistence batch"
                    )
                continue
            prepared[key] = (values, content_hash, message.socket_received_monotonic_ns)

        keys = tuple(prepared)
        placeholders = ",".join("(?,?,?)" for _ in keys)
        existing = {
            (str(row[0]), int(row[1]), int(row[2])): str(row[3])
            for row in self._connection.execute(
                "SELECT connection_id,subscription_id,sequence,content_hash "
                f"FROM kalshi_ws_orderbook_events WHERE "
                f"(connection_id,subscription_id,sequence) IN ({placeholders})",
                tuple(value for key in keys for value in key),
            )
        }
        inserts: list[tuple[object, ...]] = []
        inserted_keys: list[tuple[str, int, int]] = []
        received_values: list[int] = []
        for key, (values, content_hash, received_ns) in prepared.items():
            existing_hash = existing.get(key)
            if existing_hash is not None:
                if existing_hash != content_hash:
                    raise RecorderStorageError(
                        "conflicting Kalshi WS fact for subscription sequence"
                    )
                continue
            inserts.append(values)
            inserted_keys.append(key)
            if received_ns is not None:
                received_values.append(received_ns)
        if not inserts:
            return 0, None
        with self._connection:
            self._connection.executemany(
                """INSERT INTO kalshi_ws_orderbook_events(
                    schema_version,connection_id,subscription_id,sequence,event_kind,ticker,
                    market_id,market_tickers,side,price,quantity_delta,yes_bids,no_bids,
                    source_timestamp,socket_received_timestamp,enqueue_timestamp,parse_timestamp,
                    persisted_timestamp,receive_enqueue_latency_ms,receive_persist_latency_ms,
                    sync_status_after,provenance,data_role,content_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                inserts,
            )
            completed_ns = time.perf_counter_ns()
            persisted = _timestamp(datetime.now(UTC))
            latency_by_key = {
                key: (
                    Decimal(completed_ns - prepared[key][2]) / Decimal(1_000_000)
                    if prepared[key][2] is not None
                    else None
                )
                for key in inserted_keys
            }
            if any(value is not None and value < 0 for value in latency_by_key.values()):
                raise RecorderStorageError("Kalshi WS persistence timing is not monotonic")
            self._connection.executemany(
                """UPDATE kalshi_ws_orderbook_events
                SET persisted_timestamp=?,receive_persist_latency_ms=?
                WHERE connection_id=? AND subscription_id=? AND sequence=?""",
                (
                    (
                        persisted,
                        str(latency_by_key[key]) if latency_by_key[key] is not None else None,
                        *key,
                    )
                    for key in inserted_keys
                ),
            )
        maximum = (
            max(value for value in latency_by_key.values() if value is not None)
            if received_values
            else None
        )
        return len(inserts), maximum

    def stage_kalshi_ws_orderbook_event(
        self,
        message: KalshiOrderBookMessage | KalshiCommandAcknowledged,
        *,
        sync_status_after: KalshiBookSyncStatus,
    ) -> int | None:
        """Stage one immutable raw event for a bounded group commit."""

        acknowledgement = message if isinstance(message, KalshiCommandAcknowledged) else None
        if acknowledgement is not None and (
            acknowledgement.subscription_id is None or acknowledgement.sequence is None
        ):
            raise RecorderStorageError("unsequenced Kalshi WS acknowledgement is not replay state")
        snapshot = message if isinstance(message, KalshiOrderBookSnapshot) else None
        delta = message if isinstance(message, KalshiOrderBookDelta) else None
        event_kind = (
            KalshiWsEventKind.SUBSCRIPTION_ACK
            if acknowledgement is not None
            else KalshiWsEventKind.SNAPSHOT
            if snapshot is not None
            else KalshiWsEventKind.DELTA
        )
        yes_bids = json.dumps(
            [[str(level.price), str(level.quantity)] for level in snapshot.yes_bids]
            if snapshot is not None
            else [],
            separators=(",", ":"),
        )
        no_bids = json.dumps(
            [[str(level.price), str(level.quantity)] for level in snapshot.no_bids]
            if snapshot is not None
            else [],
            separators=(",", ":"),
        )
        immutable: tuple[object, ...] = (
            message.connection_id,
            message.subscription_id,
            message.sequence,
            event_kind.value,
            message.ticker if acknowledgement is None else None,
            message.market_id if acknowledgement is None else None,
            json.dumps(
                acknowledgement.market_tickers if acknowledgement is not None else (),
                separators=(",", ":"),
            ),
            delta.side.value if delta is not None else None,
            _decimal(delta.price) if delta is not None else None,
            _decimal(delta.quantity_delta) if delta is not None else None,
            yes_bids,
            no_bids,
            (
                _timestamp(message.source_timestamp)
                if acknowledgement is None and message.source_timestamp is not None
                else None
            ),
            message.provenance,
            message.role.value,
        )
        content_hash = _fingerprint(immutable)
        enqueue_latency = (
            Decimal(message.enqueue_monotonic_ns - message.socket_received_monotonic_ns)
            / Decimal(1_000_000)
            if message.enqueue_monotonic_ns is not None
            and message.socket_received_monotonic_ns is not None
            else None
        )
        if enqueue_latency is not None and enqueue_latency < 0:
            raise RecorderStorageError("Kalshi WS enqueue timing is not monotonic")
        cursor = self._connection.execute(
            """INSERT OR IGNORE INTO kalshi_ws_orderbook_events(
                schema_version,connection_id,subscription_id,sequence,event_kind,ticker,
                market_id,market_tickers,side,price,quantity_delta,yes_bids,no_bids,
                source_timestamp,socket_received_timestamp,enqueue_timestamp,parse_timestamp,
                persisted_timestamp,receive_enqueue_latency_ms,receive_persist_latency_ms,
                sync_status_after,provenance,data_role,content_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                SCHEMA_VERSION,
                *immutable[:13],
                _timestamp(message.socket_received_timestamp),
                (
                    _timestamp(message.enqueue_timestamp)
                    if message.enqueue_timestamp is not None
                    else None
                ),
                _timestamp(message.parse_timestamp),
                None,
                str(enqueue_latency) if enqueue_latency is not None else None,
                None,
                sync_status_after.value,
                *immutable[13:],
                content_hash,
            ),
        )
        if cursor.rowcount == 0:
            existing = self._connection.execute(
                """SELECT content_hash FROM kalshi_ws_orderbook_events
                WHERE connection_id=? AND subscription_id=? AND sequence=?""",
                (message.connection_id, message.subscription_id, message.sequence),
            ).fetchone()
            if existing is None or existing["content_hash"] != content_hash:
                raise RecorderStorageError("conflicting Kalshi WS fact for subscription sequence")
            return None
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def finalize_kalshi_ws_orderbook_events(
        self, events: Sequence[tuple[int, int | None]]
    ) -> Decimal | None:
        """Atomically mark a bounded staged batch durable and return its worst latency."""

        if not events:
            return None
        persisted = datetime.now(UTC)
        completed_ns = time.perf_counter_ns()
        values: list[tuple[str, str | None, int]] = []
        latencies: list[Decimal] = []
        for row_id, received_ns in events:
            if row_id < 1 or (received_ns is not None and received_ns < 0):
                raise RecorderStorageError("invalid staged Kalshi WS persistence identity")
            latency = (
                Decimal(completed_ns - received_ns) / Decimal(1_000_000)
                if received_ns is not None
                else None
            )
            if latency is not None and latency < 0:
                raise RecorderStorageError("Kalshi WS persistence timing is not monotonic")
            if latency is not None:
                latencies.append(latency)
            values.append(
                (
                    _timestamp(persisted),
                    str(latency) if latency is not None else None,
                    row_id,
                )
            )
        with self._connection:
            self._connection.executemany(
                """UPDATE kalshi_ws_orderbook_events
                SET persisted_timestamp=?,receive_persist_latency_ms=? WHERE id=?""",
                values,
            )
        return max(latencies) if latencies else None

    def append_kalshi_ws_checkpoint(self, book: SynchronizedKalshiOrderBook) -> bool:
        """Store a sparse synchronized book, normally only after snapshot/resync."""

        if book.status is not KalshiBookSyncStatus.SYNCHRONIZED:
            raise RecorderStorageError("unsynchronized Kalshi WS book cannot be checkpointed")
        yes_bids = json.dumps(
            [[str(level.price), str(level.quantity)] for level in book.yes_bids],
            separators=(",", ":"),
        )
        no_bids = json.dumps(
            [[str(level.price), str(level.quantity)] for level in book.no_bids],
            separators=(",", ":"),
        )
        immutable: tuple[object, ...] = (
            book.connection_id,
            book.subscription_id,
            book.sequence,
            book.ticker,
            book.market_id,
            yes_bids,
            no_bids,
            _timestamp(book.source_timestamp) if book.source_timestamp is not None else None,
            _timestamp(book.received_timestamp),
            book.provenance,
            DataRole.CONTRACT_MARKET_QUOTE.value,
        )
        content_hash = _fingerprint(immutable)
        existing = self._connection.execute(
            """SELECT content_hash FROM kalshi_ws_book_checkpoints
            WHERE connection_id=? AND subscription_id=? AND sequence=? AND ticker=?""",
            (book.connection_id, book.subscription_id, book.sequence, book.ticker),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != content_hash:
                raise RecorderStorageError("conflicting Kalshi WS book checkpoint")
            return False
        self._connection.execute(
            """INSERT INTO kalshi_ws_book_checkpoints(
                schema_version,connection_id,subscription_id,sequence,ticker,market_id,
                yes_bids,no_bids,source_timestamp,received_timestamp,persisted_timestamp,
                provenance,data_role,content_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                SCHEMA_VERSION,
                *immutable[:9],
                _timestamp(datetime.now(UTC)),
                *immutable[9:],
                content_hash,
            ),
        )
        self._connection.commit()
        return True

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
            raise MarketIdentityConflictError(
                f"conflicting official market metadata for {market.ticker}"
            )
        conflicting_ticker = self._connection.execute(
            """
            SELECT ticker FROM kalshi_market_lifecycle
            WHERE ticker != ? AND (
                event_ticker = ? OR
                (asset = ? AND series = ? AND window_start = ? AND window_end = ?)
            )
            ORDER BY id ASC LIMIT 1
            """,
            (
                market.ticker,
                market.event_ticker,
                market.asset.value,
                market.series,
                _timestamp(market.window_start),
                _timestamp(market.window_end),
            ),
        ).fetchone()
        if conflicting_ticker is not None:
            raise MarketIdentityConflictError(
                "conflicting official ticker for the same event/window"
            )
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
        existing = self._connection.execute(
            """
            SELECT content_hash FROM kalshi_prediction_quotes
            WHERE ticker=? AND received_timestamp=?
            ORDER BY id LIMIT 1
            """,
            (quote.ticker, _timestamp(quote.received_timestamp)),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != content_hash:
                raise RecorderStorageError(
                    "conflicting Kalshi quote fact for ticker receive timestamp"
                )
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
            raise SettlementConflictError(f"conflicting official settlement for {truth.ticker}")
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
        self._connection.execute(
            """INSERT INTO kalshi_settlement_counts(asset,count) VALUES (?,1)
            ON CONFLICT(asset) DO UPDATE SET count=count+1""",
            (truth.asset.value,),
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

    def append_recorder_event(
        self,
        *,
        observed_timestamp: datetime,
        severity: RecorderEventSeverity,
        event_type: RecorderEventType,
        message: str,
        asset: Asset | None = None,
        source: str | None = None,
        error_type: str | None = None,
        dedup_key: str | None = None,
        retain: int = 5000,
    ) -> bool:
        """Append one secret-free operational event and prune only the bounded event table."""

        if not 100 <= retain <= 100_000:
            raise ValueError("recorder event retention must be in 100..100000")
        safe_message = " ".join(message.split())
        if not safe_message or len(safe_message) > 240:
            raise ValueError("recorder event message must contain 1..240 safe characters")
        for value, field in ((source, "source"), (error_type, "error_type"), (dedup_key, "key")):
            if value is not None and (not value or len(value) > 160 or "\n" in value):
                raise ValueError(f"invalid recorder event {field}")
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO recorder_events(
                schema_version,observed_timestamp,severity,event_type,asset,source,
                error_type,message,dedup_key
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                SCHEMA_VERSION,
                _timestamp(observed_timestamp),
                severity.value,
                event_type.value,
                asset.value if asset is not None else None,
                source,
                error_type,
                safe_message,
                dedup_key,
            ),
        )
        if cursor.rowcount == 1:
            self._connection.execute(
                """
                DELETE FROM recorder_events WHERE id <= COALESCE(
                    (SELECT id FROM recorder_events ORDER BY id DESC LIMIT 1 OFFSET ?), 0
                )
                """,
                (retain,),
            )
        self._connection.commit()
        return cursor.rowcount == 1

    def replay_recorder_events(
        self,
        *,
        limit: int = 100,
        severity: RecorderEventSeverity | None = None,
        asset: Asset | None = None,
        source: str | None = None,
        since: datetime | None = None,
    ) -> tuple[RecorderEventRecord, ...]:
        """Return a bounded newest-first operational diagnostic projection."""

        if not 1 <= limit <= 200:
            raise ValueError("recorder event query limit must be in 1..200")
        clauses: list[str] = []
        parameters: list[object] = []
        if severity is not None:
            clauses.append("severity=?")
            parameters.append(severity.value)
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
        rows = self._connection.execute(
            f"""SELECT * FROM recorder_events {where}
            ORDER BY observed_timestamp DESC,id DESC LIMIT ?""",
            parameters,
        )
        return tuple(self._recorder_event_record(row) for row in rows)

    @staticmethod
    def _recorder_event_record(row: sqlite3.Row) -> RecorderEventRecord:
        try:
            if not _compatible_record_version(row["schema_version"]):
                raise RecorderStorageError("malformed recorder event record")
            return RecorderEventRecord(
                row_id=int(row["id"]),
                schema_version=int(row["schema_version"]),
                observed_timestamp=_parse_timestamp(
                    row["observed_timestamp"], "observed_timestamp"
                ),
                severity=RecorderEventSeverity(row["severity"]),
                event_type=RecorderEventType(row["event_type"]),
                asset=Asset(row["asset"]) if row["asset"] is not None else None,
                source=str(row["source"]) if row["source"] is not None else None,
                error_type=str(row["error_type"]) if row["error_type"] is not None else None,
                message=str(row["message"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RecorderStorageError("malformed recorder event record") from error

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
        if window_end_at_or_after is not None and window_end_before is not None:
            rows = self._connection.execute(
                """WITH bounded AS (
                    SELECT * FROM kalshi_market_lifecycle
                    WHERE window_end>=? AND window_end<?
                ), ranked AS (
                    SELECT bounded.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY ticker
                               ORDER BY fetched_timestamp DESC,id DESC
                           ) AS recency
                    FROM bounded
                )
                SELECT * FROM ranked WHERE recency=1 ORDER BY ticker ASC""",
                tuple(parameters),
            )
        else:
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

    def latest_native_cursors(
        self, products: Sequence[str]
    ) -> tuple[dict[Asset, datetime], dict[str, datetime]]:
        """Recover cursors with bounded right-edge index lookups, never GROUP BY scans."""

        quotes: dict[Asset, datetime] = {}
        for asset in Asset:
            row = self._connection.execute(
                """SELECT received_timestamp FROM kalshi_prediction_quotes
                WHERE asset=? ORDER BY received_timestamp DESC,id DESC LIMIT 1""",
                (asset.value,),
            ).fetchone()
            if row is not None:
                quotes[asset] = _parse_timestamp(row["received_timestamp"], "received_timestamp")
        ticks: dict[str, datetime] = {}
        for product in products:
            row = self._connection.execute(
                """SELECT received_timestamp FROM coinbase_ticks
                WHERE product=? ORDER BY received_timestamp DESC,id DESC LIMIT 1""",
                (product,),
            ).fetchone()
            if row is not None:
                ticks[product] = _parse_timestamp(row["received_timestamp"], "received_timestamp")
        return quotes, ticks

    def latest_finalized_by_asset(self) -> dict[Asset, KalshiSettlementRecord]:
        finalized: dict[Asset, KalshiSettlementRecord] = {}
        for asset in Asset:
            row = self._connection.execute(
                """SELECT * FROM kalshi_settlements WHERE asset=?
                ORDER BY settlement_timestamp DESC,id DESC LIMIT 1""",
                (asset.value,),
            ).fetchone()
            if row is not None:
                finalized[asset] = self._kalshi_settlement_record(row)
        return finalized

    def settlement_counts_by_asset(self) -> dict[Asset, int]:
        counts = {asset: 0 for asset in Asset}
        for row in self._connection.execute("SELECT asset, count FROM kalshi_settlement_counts"):
            counts[Asset(row["asset"])] = int(row["count"])
        return counts

    def has_kalshi_settlement(self, ticker: str) -> bool:
        """Use the unique ticker index for constant-time recorder bookkeeping."""

        return (
            self._connection.execute(
                "SELECT 1 FROM kalshi_settlements WHERE ticker=? LIMIT 1", (ticker,)
            ).fetchone()
            is not None
        )

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
            raise TrainingDataUnavailableError(
                TrainingDataUnavailableReason.OFFICIAL_SETTLEMENT_UNAVAILABLE,
                "official settlement label is unavailable",
            )
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
            raise TrainingDataUnavailableError(
                TrainingDataUnavailableReason.MISSING_DECISION_TIME_METADATA,
                "no official metadata existed at decision time",
            )
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

    def replay_underlying_range(
        self,
        asset: Asset,
        provider: UnderlyingProvider,
        *,
        start: datetime,
        end: datetime,
        max_row_id: int | None = None,
    ) -> Iterator[UnderlyingObservationRecord]:
        """Yield one source only; providers are never silently blended."""

        if start.tzinfo is None or end.tzinfo is None or start > end:
            raise ValueError("underlying range requires ordered aware timestamps")
        rows = self._connection.execute(
            """
            SELECT * FROM underlying_observations
            WHERE asset=? AND provider=? AND received_timestamp>=? AND received_timestamp<=?
              AND (? IS NULL OR id<=?)
            ORDER BY received_timestamp ASC,id ASC
            """,
            (
                asset.value,
                provider.value,
                _timestamp(start),
                _timestamp(end),
                max_row_id,
                max_row_id,
            ),
        )
        for row in rows:
            yield self._underlying_record(row)

    def latest_secondary_underlying(
        self, asset: Asset, provider: UnderlyingProvider
    ) -> SecondaryUnderlyingObservationRecord | None:
        row = self._connection.execute(
            """SELECT * FROM secondary_underlying_observations
            WHERE asset=? AND provider=? ORDER BY received_timestamp DESC,id DESC LIMIT 1""",
            (asset.value, provider.value),
        ).fetchone()
        return None if row is None else self._secondary_underlying_record(row)

    def replay_secondary_underlying_range(
        self,
        asset: Asset,
        provider: UnderlyingProvider,
        *,
        start: datetime,
        end: datetime,
    ) -> Iterator[SecondaryUnderlyingObservationRecord]:
        if start.tzinfo is None or end.tzinfo is None or start > end:
            raise ValueError("secondary range requires ordered aware timestamps")
        rows = self._connection.execute(
            """SELECT * FROM secondary_underlying_observations
            WHERE asset=? AND provider=? AND received_timestamp>=? AND received_timestamp<=?
            ORDER BY received_timestamp ASC,id ASC""",
            (asset.value, provider.value, _timestamp(start), _timestamp(end)),
        )
        for row in rows:
            yield self._secondary_underlying_record(row)

    def replay_kalshi_ws_orderbook_events(
        self, connection_id: str, subscription_id: int
    ) -> Iterator[KalshiWsOrderBookEventRecord]:
        """Replay raw WS arrival order; sequence faults remain observable."""

        rows = self._connection.execute(
            """SELECT * FROM kalshi_ws_orderbook_events
            WHERE connection_id=? AND subscription_id=? ORDER BY id ASC""",
            (connection_id, subscription_id),
        )
        for row in rows:
            yield self._kalshi_ws_event_record(row)

    def replay_kalshi_ws_checkpoints(self, ticker: str) -> Iterator[KalshiWsBookCheckpointRecord]:
        rows = self._connection.execute(
            """SELECT * FROM kalshi_ws_book_checkpoints
            WHERE ticker=? ORDER BY received_timestamp ASC,id ASC""",
            (ticker,),
        )
        for row in rows:
            yield self._kalshi_ws_checkpoint_record(row)

    @staticmethod
    def _kalshi_ws_event_record(row: sqlite3.Row) -> KalshiWsOrderBookEventRecord:
        try:
            event_kind = KalshiWsEventKind(row["event_kind"])
            side = KalshiBookSide(row["side"]) if row["side"] is not None else None
            price = _parse_decimal(row["price"], "price", optional=True)
            quantity_delta = _parse_decimal(row["quantity_delta"], "quantity_delta", optional=True)
            yes_bids = tuple(
                OrderBookLevel(Decimal(price_value), Decimal(quantity))
                for price_value, quantity in _book_levels(row["yes_bids"], "yes_bids")
            )
            no_bids = tuple(
                OrderBookLevel(Decimal(price_value), Decimal(quantity))
                for price_value, quantity in _book_levels(row["no_bids"], "no_bids")
            )
            market_tickers = tuple(json.loads(row["market_tickers"]))
            if any(not isinstance(ticker, str) or not ticker for ticker in market_tickers):
                raise RecorderStorageError("malformed Kalshi WS acknowledgement tickers")
            if event_kind is KalshiWsEventKind.SNAPSHOT:
                if side is not None or price is not None or quantity_delta is not None:
                    raise RecorderStorageError("malformed Kalshi WS snapshot record")
            elif event_kind is KalshiWsEventKind.DELTA and (
                side is None or price is None or quantity_delta is None or yes_bids or no_bids
            ):
                raise RecorderStorageError("malformed Kalshi WS delta record")
            elif event_kind is KalshiWsEventKind.SUBSCRIPTION_ACK and (
                row["ticker"] is not None
                or row["market_id"] is not None
                or side is not None
                or price is not None
                or quantity_delta is not None
                or yes_bids
                or no_bids
            ):
                raise RecorderStorageError("malformed Kalshi WS acknowledgement record")
            if not _compatible_record_version(row["schema_version"]):
                raise RecorderStorageError("malformed Kalshi WS event record")
            return KalshiWsOrderBookEventRecord(
                row_id=int(row["id"]),
                schema_version=int(row["schema_version"]),
                connection_id=str(row["connection_id"]),
                subscription_id=int(row["subscription_id"]),
                sequence=int(row["sequence"]),
                event_kind=event_kind,
                ticker=str(row["ticker"]) if row["ticker"] is not None else None,
                market_id=str(row["market_id"]) if row["market_id"] is not None else None,
                market_tickers=market_tickers,
                side=side,
                price=price,
                quantity_delta=quantity_delta,
                yes_bids=yes_bids,
                no_bids=no_bids,
                source_timestamp=(
                    _parse_timestamp(row["source_timestamp"], "source_timestamp")
                    if row["source_timestamp"] is not None
                    else None
                ),
                socket_received_timestamp=_parse_timestamp(
                    row["socket_received_timestamp"], "socket_received_timestamp"
                ),
                enqueue_timestamp=(
                    _parse_timestamp(row["enqueue_timestamp"], "enqueue_timestamp")
                    if row["enqueue_timestamp"] is not None
                    else None
                ),
                parse_timestamp=_parse_timestamp(row["parse_timestamp"], "parse_timestamp"),
                persisted_timestamp=(
                    _parse_timestamp(row["persisted_timestamp"], "persisted_timestamp")
                    if row["persisted_timestamp"] is not None
                    else None
                ),
                receive_enqueue_latency_ms=_parse_decimal(
                    row["receive_enqueue_latency_ms"],
                    "receive_enqueue_latency_ms",
                    optional=True,
                ),
                receive_persist_latency_ms=_parse_decimal(
                    row["receive_persist_latency_ms"],
                    "receive_persist_latency_ms",
                    optional=True,
                ),
                sync_status_after=KalshiBookSyncStatus(row["sync_status_after"]),
                provenance=str(row["provenance"]),
                role=DataRole(row["data_role"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RecorderStorageError("malformed Kalshi WS event record") from error

    @staticmethod
    def _kalshi_ws_checkpoint_record(row: sqlite3.Row) -> KalshiWsBookCheckpointRecord:
        try:
            yes_bids = tuple(
                OrderBookLevel(Decimal(price), Decimal(quantity))
                for price, quantity in _book_levels(row["yes_bids"], "yes_bids")
            )
            no_bids = tuple(
                OrderBookLevel(Decimal(price), Decimal(quantity))
                for price, quantity in _book_levels(row["no_bids"], "no_bids")
            )
            if not _compatible_record_version(row["schema_version"]):
                raise RecorderStorageError("malformed Kalshi WS checkpoint record")
            return KalshiWsBookCheckpointRecord(
                row_id=int(row["id"]),
                schema_version=int(row["schema_version"]),
                connection_id=str(row["connection_id"]),
                subscription_id=int(row["subscription_id"]),
                sequence=int(row["sequence"]),
                ticker=str(row["ticker"]),
                market_id=str(row["market_id"]),
                yes_bids=yes_bids,
                no_bids=no_bids,
                source_timestamp=(
                    _parse_timestamp(row["source_timestamp"], "source_timestamp")
                    if row["source_timestamp"] is not None
                    else None
                ),
                received_timestamp=_parse_timestamp(
                    row["received_timestamp"], "received_timestamp"
                ),
                persisted_timestamp=_parse_timestamp(
                    row["persisted_timestamp"], "persisted_timestamp"
                ),
                provenance=str(row["provenance"]),
                role=DataRole(row["data_role"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RecorderStorageError("malformed Kalshi WS checkpoint record") from error

    @staticmethod
    def _secondary_underlying_record(
        row: sqlite3.Row,
    ) -> SecondaryUnderlyingObservationRecord:
        try:
            price = _parse_decimal(row["price"], "price")
            bid = _parse_decimal(row["bid"], "bid", optional=True)
            ask = _parse_decimal(row["ask"], "ask", optional=True)
            source_receive = _parse_decimal(
                row["source_receive_latency_ms"], "source_receive_latency_ms"
            )
            receive_persist = _parse_decimal(
                row["receive_persist_latency_ms"],
                "receive_persist_latency_ms",
                optional=True,
            )
            persisted = (
                _parse_timestamp(row["persisted_timestamp"], "persisted_timestamp")
                if row["persisted_timestamp"] is not None
                else None
            )
            if (
                price is None
                or source_receive is None
                or not _compatible_record_version(row["schema_version"])
            ):
                raise RecorderStorageError("malformed secondary observation record")
            return SecondaryUnderlyingObservationRecord(
                row_id=int(row["id"]),
                schema_version=int(row["schema_version"]),
                asset=Asset(row["asset"]),
                provider=UnderlyingProvider(row["provider"]),
                instrument=str(row["instrument"]),
                price=price,
                price_semantics=SecondaryPriceSemantics(row["price_semantics"]),
                bid=bid,
                ask=ask,
                source_timestamp=_parse_timestamp(row["source_timestamp"], "source_timestamp"),
                received_timestamp=_parse_timestamp(
                    row["received_timestamp"], "received_timestamp"
                ),
                persisted_timestamp=persisted,
                source_receive_latency_ms=source_receive,
                receive_persist_latency_ms=receive_persist,
                provenance=str(row["provenance"]),
                freshness=FreshnessState(row["freshness"]),
                source_event_id=str(row["source_event_id"]),
                role=DataRole(row["data_role"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RecorderStorageError("malformed secondary observation record") from error

    @staticmethod
    def _underlying_record(row: sqlite3.Row) -> UnderlyingObservationRecord:
        try:
            price = _parse_decimal(row["price"], "price")
            confidence = _parse_decimal(row["confidence"], "confidence", optional=True)
            if price is None or not _compatible_record_version(row["schema_version"]):
                raise RecorderStorageError("malformed underlying observation record")
            return UnderlyingObservationRecord(
                row_id=int(row["id"]),
                schema_version=int(row["schema_version"]),
                asset=Asset(row["asset"]),
                provider=UnderlyingProvider(row["provider"]),
                symbol=str(row["symbol"]),
                feed_id=str(row["feed_id"]),
                price=price,
                source_timestamp=_parse_timestamp(row["source_timestamp"], "source_timestamp"),
                received_timestamp=_parse_timestamp(
                    row["received_timestamp"], "received_timestamp"
                ),
                confidence=confidence,
                provenance=str(row["provenance"]),
                freshness=FreshnessState(row["freshness"]),
                role=DataRole(row["data_role"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RecorderStorageError("malformed underlying observation record") from error

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
            if target is None or not _compatible_record_version(row["schema_version"]):
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
                or not _compatible_record_version(row["schema_version"])
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
            if not _compatible_record_version(row["schema_version"]):
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
            if not _compatible_record_version(row["schema_version"]):
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
            if not _compatible_record_version(row["schema_version"]):
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
            if target is None or not _compatible_record_version(row["schema_version"]):
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
            if target is None or not _compatible_record_version(row["schema_version"]):
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
            "recorder_events",
            "underlying_observations",
            "secondary_underlying_observations",
            "kalshi_ws_orderbook_events",
            "kalshi_ws_book_checkpoints",
            "data_gaps",
        }:
            raise ValueError("unknown recorder table")
        row = self._connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        if row is None:
            raise RecorderStorageError(f"could not count {table}")
        return int(row["count"])

    def row_counts(self) -> dict[str, int]:
        """Return exact full-table counts for offline diagnostics only."""

        return {
            table: self.count(table)
            for table in (
                "kalshi_market_lifecycle",
                "kalshi_prediction_quotes",
                "coinbase_ticks",
                "underlying_observations",
                "secondary_underlying_observations",
                "kalshi_ws_orderbook_events",
                "kalshi_ws_book_checkpoints",
                "kalshi_settlements",
                "kalshi_settlement_conflicts",
                "data_gaps",
            )
        }

    def bounded_row_count_estimates(self) -> dict[str, int]:
        """Return O(1) right-edge estimates when no trusted heartbeat baseline exists.

        Deleted IDs make these upper bounds rather than exact counts. Callers must
        expose that distinction instead of performing COUNT(*) during startup.
        """

        estimates: dict[str, int] = {}
        for table in (
            "kalshi_market_lifecycle",
            "kalshi_prediction_quotes",
            "coinbase_ticks",
            "underlying_observations",
            "secondary_underlying_observations",
            "kalshi_ws_orderbook_events",
            "kalshi_ws_book_checkpoints",
            "kalshi_settlements",
            "kalshi_settlement_conflicts",
            "data_gaps",
        ):
            row = self._connection.execute(
                f"SELECT id FROM {table} ORDER BY id DESC LIMIT 1"
            ).fetchone()
            estimates[table] = 0 if row is None else int(row["id"])
        return estimates

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

        pid_path = self.path.parent / "recorder.pid"
        try:
            pid = int(pid_path.read_text(encoding="ascii").strip())
        except (FileNotFoundError, OSError, ValueError):
            pid = None
        if pid is not None and process_alive(pid):
            raise ActiveRecorderAnalysisError(
                "dataset analysis requires a read-only snapshot while recorder is active"
            )

        tables = (
            "coinbase_ticks",
            "underlying_observations",
            "kalshi_prediction_quotes",
            "kalshi_market_lifecycle",
            "kalshi_settlements",
            "data_gaps",
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
        settlement_snapshot = snapshot["kalshi_settlements"]
        assert isinstance(settlement_snapshot, dict)
        settlement_snapshot["counts_by_asset"] = {
            asset.value: count for asset, count in self.settlement_counts_by_asset().items()
        }
        return snapshot

    def integrity_check(self) -> str:
        row = self._connection.execute("PRAGMA integrity_check").fetchone()
        return "missing_result" if row is None else str(row[0])

    def quick_check(self) -> str:
        """Run SQLite's full structural scan, returning at most one error row.

        This is appropriate only on an offline snapshot or maintenance copy,
        never during normal recorder startup or on the active event loop.
        """

        row = self._connection.execute("PRAGMA quick_check(1)").fetchone()
        return "missing_result" if row is None else str(row[0])

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> RecorderStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
