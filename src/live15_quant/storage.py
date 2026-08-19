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
    RecorderDiagnosticKind,
)
from live15_quant.records import (
    SCHEMA_VERSION,
    CoinbaseTickRecord,
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
            self._create_v2_schema()
            return

        row = self._connection.execute(
            "SELECT value FROM recorder_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            raise RecorderStorageError("database schema metadata is missing")
        if row["value"] == str(SCHEMA_VERSION):
            self._ensure_v2_schema_objects()
            return
        if row["value"] == "1" and SCHEMA_VERSION == 2:
            self._migrate_v1_to_v2()
            return
        raise RecorderStorageError(
            f"database schema {row['value']} is incompatible with {SCHEMA_VERSION}"
        )

    def _create_v2_schema(self) -> None:
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

    def _ensure_v2_schema_objects(self) -> None:
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
                (SCHEMA_VERSION,),
            )
            self._connection.execute(
                "UPDATE coinbase_ticks SET schema_version = ? WHERE schema_version = 1",
                (SCHEMA_VERSION,),
            )
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

    def count(self, table: str) -> int:
        if table not in {
            "robinhood_snapshots",
            "robinhood_diagnostics",
            "coinbase_ticks",
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
