"""Crash-resilient append-only SQLite storage for recorder observations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from live15_quant.models import (
    Asset,
    FifteenMinuteContract,
    MarketTick,
    PredictionMarketQuote,
    RecorderDiagnosticKind,
)
from live15_quant.records import (
    SCHEMA_VERSION,
    CoinbaseTickRecord,
    PredictionQuoteRecord,
    RobinhoodDiagnosticRecord,
    RobinhoodSnapshotRecord,
)


class RecorderStorageError(RuntimeError):
    """Raised when persisted recorder data is invalid or incompatible."""


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
        return Decimal(value)
    except InvalidOperation as error:
        raise RecorderStorageError(f"malformed {field}") from error


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
            paper_store = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_metadata'"
            ).fetchone()
            if paper_store is not None:
                raise RecorderStorageError("raw recorder cannot share the paper ledger database")
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
        if row is not None and row["value"] == "2" and SCHEMA_VERSION == 3:
            self._migrate_v2_to_v3()
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
                    (SCHEMA_VERSION,),
                )
            self._connection.execute(_QUOTE_TABLE_SQL)
            self._connection.execute(_QUOTE_EVENT_INDEX_SQL)
            self._connection.execute(_QUOTE_TICKER_INDEX_SQL)
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

    def replay_coinbase(self, product: str) -> Iterator[CoinbaseTickRecord]:
        """Yield one Coinbase product deterministically by receive time and id."""

        rows = self._connection.execute(
            """
            SELECT * FROM coinbase_ticks
            WHERE product = ? ORDER BY received_timestamp ASC, id ASC
            """,
            (product,),
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

    def count(self, table: str) -> int:
        if table not in {
            "robinhood_snapshots",
            "robinhood_diagnostics",
            "coinbase_ticks",
            "prediction_market_quotes",
        }:
            raise ValueError("unknown recorder table")
        row = self._connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        if row is None:
            raise RecorderStorageError(f"could not count {table}")
        return int(row["count"])

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> RecorderStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
