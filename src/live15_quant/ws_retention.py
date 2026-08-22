"""Verified lossless WS archive, bounded HOT retention, and offline compaction."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from live15_quant.kalshi_ws import KalshiBookSide, KalshiWsEventKind
from live15_quant.records import KalshiWsOrderBookEventRecord
from live15_quant.storage import RecorderStore
from live15_quant.ws_archive import decode_archive_chunk, encode_archive_chunk

ARCHIVE_FORMAT_VERSION = 1
ARCHIVE_CODEC = "zlib"


class WsRetentionError(RuntimeError):
    """Archive, verification, purge, or maintenance correctness failed."""


class WsMaintenanceBusy(WsRetentionError):
    """Another bounded retention process currently owns the maintenance lease."""


class ArchiveState(StrEnum):
    WRITING = "writing"
    WRITTEN = "written"
    CHECKSUM_VERIFIED = "checksum_verified"
    REPLAY_VERIFIED = "replay_verified"
    COMMITTED = "committed"
    PURGE_ELIGIBLE = "purge_eligible"
    PURGED = "purged"
    FAILED = "failed"


class DiskThresholdState(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"
    ARCHIVE_URGENT = "archive_urgent"
    CRITICAL = "critical"
    FAIL_SAFE = "fail_safe"


_STATE_ORDER = {
    ArchiveState.WRITING: 0,
    ArchiveState.WRITTEN: 1,
    ArchiveState.CHECKSUM_VERIFIED: 2,
    ArchiveState.REPLAY_VERIFIED: 3,
    ArchiveState.COMMITTED: 4,
    ArchiveState.PURGE_ELIGIBLE: 5,
    ArchiveState.PURGED: 6,
}


@dataclass(frozen=True, slots=True)
class ArchiveChunk:
    chunk_id: str
    first_event_id: int
    last_event_id: int
    event_count: int
    state: ArchiveState
    relative_path: str
    logical_checksum: str | None
    file_checksum: str | None
    uncompressed_bytes: int | None
    compressed_bytes: int | None
    first_received_timestamp: datetime
    last_received_timestamp: datetime
    event_type_counts: dict[str, int]
    tickers: tuple[str, ...]
    subscription_ids: tuple[int, ...]
    first_sequence: int
    last_sequence: int
    source_replay_hash: str | None
    archive_replay_hash: str | None
    purged_events: int
    failure: str | None


@dataclass(frozen=True, slots=True)
class ArchiveRunResult:
    chunk: ArchiveChunk | None
    elapsed_seconds: float
    events_per_second: float
    backlog_events: int


@dataclass(frozen=True, slots=True)
class RetentionEligibility:
    status: str
    observed_at: datetime
    cutoff: datetime
    oldest_unarchived_event_id: int | None
    oldest_unarchived_timestamp: datetime | None
    next_eligible_at: datetime | None
    eligible_first_event_id: int | None
    eligible_last_event_id: int | None
    eligible_rows_bounded: int
    eligible_rows_capped: bool


@dataclass(frozen=True, slots=True)
class PurgeRunResult:
    chunk_id: str | None
    deleted_events: int
    remaining_events: int
    transaction_seconds: float
    freelist_pages_before: int
    freelist_pages_after: int
    reusable_bytes_increase: int
    database_bytes: int


@dataclass(frozen=True, slots=True)
class StorageTierMetrics:
    hot_sqlite_used_bytes: int
    freelist_reusable_bytes: int
    physical_database_bytes: int
    wal_bytes: int
    cold_archive_bytes: int
    cold_archive_growth_bytes_per_hour: float | None
    cold_archive_growth_bytes_per_day: float | None
    raw_ws_growth_bytes_per_hour: float | None = None
    raw_ws_growth_bytes_per_day: float | None = None
    raw_ws_observation_window_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class StorageGrowthMetrics:
    sample_interval_seconds: float | None
    net_disk_growth_bytes_per_hour: float | None
    net_disk_growth_bytes_per_day: float | None


@dataclass(frozen=True, slots=True)
class CompactionSwapResult:
    old_bytes: int
    compacted_bytes: int
    reclaimed_bytes: int
    rollback_path: Path
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class DiskQuota:
    warning_percent: Decimal = Decimal("70")
    archive_urgent_percent: Decimal = Decimal("75")
    critical_percent: Decimal = Decimal("85")
    fail_safe_percent: Decimal = Decimal("90")
    warning_free_bytes: int = 100 * 1024**3
    critical_free_bytes: int = 50 * 1024**3
    fail_safe_free_bytes: int = 25 * 1024**3

    def classify(self, *, total_bytes: int, free_bytes: int) -> DiskThresholdState:
        if total_bytes <= 0 or not 0 <= free_bytes <= total_bytes:
            raise ValueError("invalid disk capacity")
        used = Decimal(total_bytes - free_bytes) * Decimal(100) / Decimal(total_bytes)
        if used >= self.fail_safe_percent or free_bytes < self.fail_safe_free_bytes:
            return DiskThresholdState.FAIL_SAFE
        if used >= self.critical_percent or free_bytes < self.critical_free_bytes:
            return DiskThresholdState.CRITICAL
        if used >= self.archive_urgent_percent:
            return DiskThresholdState.ARCHIVE_URGENT
        if used >= self.warning_percent or free_bytes < self.warning_free_bytes:
            return DiskThresholdState.WARNING
        return DiskThresholdState.NORMAL


@dataclass(frozen=True, slots=True)
class CompactionBenefitGate:
    minimum_reclaimable_bytes: int
    minimum_reclaimable_percent: Decimal

    def evaluate(self, *, database_bytes: int, reclaimable_bytes: int) -> bool:
        if database_bytes <= 0 or not 0 <= reclaimable_bytes <= database_bytes:
            raise ValueError("invalid compaction benefit inputs")
        percent = Decimal(reclaimable_bytes) * Decimal(100) / Decimal(database_bytes)
        return (
            reclaimable_bytes >= self.minimum_reclaimable_bytes
            and percent >= self.minimum_reclaimable_percent
        )


@dataclass(frozen=True, slots=True)
class CompactionBenefitDecision:
    database_bytes: int
    reclaimable_bytes: int
    reclaimable_percent: Decimal
    minimum_reclaimable_bytes: int
    minimum_reclaimable_percent: Decimal
    allowed: bool


def evaluate_database_compaction(
    database: Path, gate: CompactionBenefitGate
) -> CompactionBenefitDecision:
    """Read O(1) SQLite page counters; never scan tables or modify the database."""

    resolved = database.resolve()
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=2.0)
    try:
        connection.execute("PRAGMA query_only=ON")
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
    finally:
        connection.close()
    database_bytes = page_size * page_count
    reclaimable_bytes = page_size * free_pages
    percent = Decimal(reclaimable_bytes) * Decimal(100) / Decimal(database_bytes)
    return CompactionBenefitDecision(
        database_bytes=database_bytes,
        reclaimable_bytes=reclaimable_bytes,
        reclaimable_percent=percent,
        minimum_reclaimable_bytes=gate.minimum_reclaimable_bytes,
        minimum_reclaimable_percent=gate.minimum_reclaimable_percent,
        allowed=gate.evaluate(database_bytes=database_bytes, reclaimable_bytes=reclaimable_bytes),
    )


@dataclass(frozen=True, slots=True)
class PurgeBenefitAssessment:
    source_bytes: int
    source_pages: int
    deleted_rows: int
    freed_pages: int
    reclaimable_bytes_after_delete: int
    baseline_compacted_bytes: int
    post_purge_compacted_bytes: int
    purge_physical_bytes: int
    purge_saving_percent: Decimal
    preexisting_reclaimable_bytes: int


def assess_purge_benefit(
    snapshot: Path,
    compacted: Path,
    ranges: tuple[tuple[int, int], ...],
) -> PurgeBenefitAssessment:
    """Measure exact purge/compaction benefit on a fixed disposable snapshot only."""

    snapshot = snapshot.resolve()
    compacted = compacted.resolve()
    if not snapshot.is_file() or compacted.exists() or snapshot == compacted:
        raise WsRetentionError("benefit assessment requires a fixed snapshot and new output")
    wal = snapshot.with_name(f"{snapshot.name}-wal")
    if wal.exists() and wal.stat().st_size:
        raise WsRetentionError("benefit assessment requires a WAL-free snapshot")
    previous = 0
    for first, last in ranges:
        if first <= previous or last < first:
            raise WsRetentionError("benefit assessment ranges must be ordered and disjoint")
        previous = last
    baseline = compacted.with_name(f"{compacted.stem}-baseline{compacted.suffix}")
    if baseline.exists():
        raise WsRetentionError("benefit assessment baseline output already exists")
    connection = sqlite3.connect(snapshot)
    try:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        source_pages = int(connection.execute("PRAGMA page_count").fetchone()[0])
        free_before = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        escaped_baseline = str(baseline).replace("'", "''")
        connection.execute(f"VACUUM INTO '{escaped_baseline}'")
        deleted = 0
        with connection:
            for first, last in ranges:
                cursor = connection.execute(
                    "DELETE FROM kalshi_ws_orderbook_events WHERE id BETWEEN ? AND ?",
                    (first, last),
                )
                deleted += cursor.rowcount
        free_after = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        escaped = str(compacted).replace("'", "''")
        connection.execute(f"VACUUM INTO '{escaped}'")
    finally:
        connection.close()
    for candidate in (baseline, compacted):
        verified = sqlite3.connect(candidate)
        try:
            if verified.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise WsRetentionError("benefit compact copy failed integrity verification")
            if verified.execute("PRAGMA foreign_key_check").fetchall():
                raise WsRetentionError("benefit compact copy failed foreign-key verification")
        finally:
            verified.close()
    source_bytes = snapshot.stat().st_size
    baseline_bytes = baseline.stat().st_size
    compacted_bytes = compacted.stat().st_size
    purge_saving = baseline_bytes - compacted_bytes
    return PurgeBenefitAssessment(
        source_bytes=source_bytes,
        source_pages=source_pages,
        deleted_rows=deleted,
        freed_pages=free_after - free_before,
        reclaimable_bytes_after_delete=(free_after - free_before) * page_size,
        baseline_compacted_bytes=baseline_bytes,
        post_purge_compacted_bytes=compacted_bytes,
        purge_physical_bytes=purge_saving,
        purge_saving_percent=(Decimal(purge_saving) * Decimal(100) / Decimal(baseline_bytes)),
        preexisting_reclaimable_bytes=source_bytes - baseline_bytes,
    )


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WsRetentionError("manifest timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _fixed_decimal(value: Decimal) -> str:
    return str(value)


@dataclass(slots=True)
class _Book:
    market_id: str
    yes: dict[Decimal, Decimal]
    no: dict[Decimal, Decimal]


@dataclass(slots=True)
class _ReplayState:
    connection_id: str | None
    subscription_id: int | None
    last_sequence: int | None
    synchronized: bool
    subscribed: set[str]
    books: dict[str, _Book]
    sequence_discontinuities: int

    @classmethod
    def empty(cls) -> _ReplayState:
        return cls(None, None, None, True, set(), {}, 0)

    @classmethod
    def from_json(cls, payload: str | None) -> _ReplayState:
        if payload is None:
            return cls.empty()
        value = json.loads(payload)
        if not isinstance(value, dict) or value.get("version") != 1:
            raise WsRetentionError("archive replay checkpoint is malformed")
        books: dict[str, _Book] = {}
        for ticker, fact in value["books"].items():
            books[str(ticker)] = _Book(
                market_id=str(fact["market_id"]),
                yes={Decimal(price): Decimal(quantity) for price, quantity in fact["yes"]},
                no={Decimal(price): Decimal(quantity) for price, quantity in fact["no"]},
            )
        return cls(
            connection_id=value["connection_id"],
            subscription_id=value["subscription_id"],
            last_sequence=value["last_sequence"],
            synchronized=bool(value["synchronized"]),
            subscribed=set(value["subscribed"]),
            books=books,
            sequence_discontinuities=int(value.get("sequence_discontinuities", 0)),
        )

    def as_json(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "connection_id": self.connection_id,
                "subscription_id": self.subscription_id,
                "last_sequence": self.last_sequence,
                "synchronized": self.synchronized,
                "sequence_discontinuities": self.sequence_discontinuities,
                "subscribed": sorted(self.subscribed),
                "books": {
                    ticker: {
                        "market_id": book.market_id,
                        "yes": [
                            [_fixed_decimal(p), _fixed_decimal(q)]
                            for p, q in sorted(book.yes.items())
                        ],
                        "no": [
                            [_fixed_decimal(p), _fixed_decimal(q)]
                            for p, q in sorted(book.no.items())
                        ],
                    }
                    for ticker, book in sorted(self.books.items())
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.as_json().encode()).hexdigest()

    def _sequence(self, record: KalshiWsOrderBookEventRecord) -> None:
        identity = (record.connection_id, record.subscription_id)
        if self.connection_id is None:
            self.connection_id, self.subscription_id = identity
        elif identity != (self.connection_id, self.subscription_id):
            raise WsRetentionError("archive chunk crossed replay stream identity")
        if self.last_sequence is not None and record.sequence != self.last_sequence + 1:
            if record.event_kind is KalshiWsEventKind.SNAPSHOT:
                self.sequence_discontinuities += 1
                self.last_sequence = record.sequence
                return
            self.synchronized = False
            raise WsRetentionError("archive source contains an unexplained sequence discontinuity")
        self.last_sequence = record.sequence

    def apply(self, record: KalshiWsOrderBookEventRecord) -> None:
        self._sequence(record)
        if record.event_kind is KalshiWsEventKind.SUBSCRIPTION_ACK:
            self.subscribed = set(record.market_tickers)
            self.books = {
                ticker: book for ticker, book in self.books.items() if ticker in self.subscribed
            }
            return
        if record.ticker is None or record.market_id is None:
            raise WsRetentionError("archive replay event identity is incomplete")
        if record.event_kind is KalshiWsEventKind.SNAPSHOT:
            self.subscribed.add(record.ticker)
            self.books[record.ticker] = _Book(
                record.market_id,
                {item.price: item.quantity for item in record.yes_bids},
                {item.price: item.quantity for item in record.no_bids},
            )
            self.synchronized = True
            return
        if record.side is None or record.price is None or record.quantity_delta is None:
            raise WsRetentionError("archive delta is incomplete")
        book = self.books.get(record.ticker)
        if book is None or book.market_id != record.market_id or not self.synchronized:
            raise WsRetentionError("archive delta has no synchronized replay baseline")
        levels = book.yes if record.side is KalshiBookSide.YES else book.no
        quantity = levels.get(record.price, Decimal(0)) + record.quantity_delta
        if quantity < 0:
            raise WsRetentionError("archive delta creates negative depth")
        if quantity == 0:
            levels.pop(record.price, None)
        else:
            levels[record.price] = quantity


class WsRetentionManifest:
    """Separate transactional manifest; raw database compaction cannot remove it."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """CREATE TABLE IF NOT EXISTS ws_retention_chunks(
                    chunk_id TEXT PRIMARY KEY,
                    first_event_id INTEGER NOT NULL,
                    last_event_id INTEGER NOT NULL,
                    event_count INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    relative_path TEXT NOT NULL UNIQUE,
                    logical_checksum TEXT,
                    file_checksum TEXT,
                    uncompressed_bytes INTEGER,
                    compressed_bytes INTEGER,
                    first_received_timestamp TEXT NOT NULL,
                    last_received_timestamp TEXT NOT NULL,
                    first_source_timestamp TEXT,
                    last_source_timestamp TEXT,
                    event_type_counts TEXT NOT NULL,
                    tickers TEXT NOT NULL,
                    subscription_ids TEXT NOT NULL,
                    first_sequence INTEGER NOT NULL,
                    last_sequence INTEGER NOT NULL,
                    archive_format_version INTEGER NOT NULL,
                    codec TEXT NOT NULL,
                    source_replay_hash TEXT,
                    archive_replay_hash TEXT,
                    end_replay_state TEXT,
                    purged_events INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    failure TEXT,
                    UNIQUE(first_event_id,last_event_id),
                    CHECK(first_event_id>0 AND last_event_id>=first_event_id),
                    CHECK(event_count>0 AND purged_events>=0 AND purged_events<=event_count)
                ) STRICT;
                CREATE INDEX IF NOT EXISTS idx_ws_retention_state
                ON ws_retention_chunks(state,first_event_id);
                CREATE INDEX IF NOT EXISTS idx_ws_retention_range
                ON ws_retention_chunks(first_event_id,last_event_id);
                CREATE TABLE IF NOT EXISTS ws_storage_samples(
                    id INTEGER PRIMARY KEY,
                    observed_at TEXT NOT NULL UNIQUE,
                    physical_database_bytes INTEGER NOT NULL,
                    wal_bytes INTEGER NOT NULL,
                    cold_archive_bytes INTEGER NOT NULL,
                    CHECK(physical_database_bytes>=0 AND wal_bytes>=0 AND cold_archive_bytes>=0)
                ) STRICT;
                CREATE INDEX IF NOT EXISTS idx_ws_storage_samples_time
                ON ws_storage_samples(observed_at);
                CREATE TABLE IF NOT EXISTS ws_retention_lease(
                    lock_name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                ) STRICT;"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def maintenance_lease(self, *, lifetime: timedelta = timedelta(minutes=10)) -> Iterator[None]:
        """Serialize archive/purge across recorder and scheduler-compatible CLI processes."""

        if lifetime <= timedelta(0):
            raise ValueError("maintenance lease lifetime must be positive")
        owner = uuid4().hex
        observed = datetime.now(UTC)
        expires = observed + lifetime
        with self._connect() as connection, connection:
            cursor = connection.execute(
                """INSERT INTO ws_retention_lease(lock_name,owner,expires_at)
                VALUES('archive-purge',?,?) ON CONFLICT(lock_name) DO UPDATE SET
                owner=excluded.owner,expires_at=excluded.expires_at
                WHERE ws_retention_lease.expires_at<=?""",
                (owner, expires.isoformat(), observed.isoformat()),
            )
            if cursor.rowcount != 1:
                raise WsMaintenanceBusy("another archive/purge maintenance pass is active")
        try:
            yield
        finally:
            with self._connect() as connection, connection:
                connection.execute(
                    "DELETE FROM ws_retention_lease WHERE lock_name='archive-purge' AND owner=?",
                    (owner,),
                )

    @staticmethod
    def _chunk(row: sqlite3.Row) -> ArchiveChunk:
        return ArchiveChunk(
            chunk_id=str(row["chunk_id"]),
            first_event_id=int(row["first_event_id"]),
            last_event_id=int(row["last_event_id"]),
            event_count=int(row["event_count"]),
            state=ArchiveState(row["state"]),
            relative_path=str(row["relative_path"]),
            logical_checksum=row["logical_checksum"],
            file_checksum=row["file_checksum"],
            uncompressed_bytes=row["uncompressed_bytes"],
            compressed_bytes=row["compressed_bytes"],
            first_received_timestamp=_parse_time(row["first_received_timestamp"]),
            last_received_timestamp=_parse_time(row["last_received_timestamp"]),
            event_type_counts=json.loads(row["event_type_counts"]),
            tickers=tuple(json.loads(row["tickers"])),
            subscription_ids=tuple(json.loads(row["subscription_ids"])),
            first_sequence=int(row["first_sequence"]),
            last_sequence=int(row["last_sequence"]),
            source_replay_hash=row["source_replay_hash"],
            archive_replay_hash=row["archive_replay_hash"],
            purged_events=int(row["purged_events"]),
            failure=row["failure"],
        )

    def chunks(self, *states: ArchiveState) -> tuple[ArchiveChunk, ...]:
        with self._connect() as connection:
            if states:
                placeholders = ",".join("?" for _ in states)
                rows = connection.execute(
                    f"SELECT * FROM ws_retention_chunks WHERE state IN ({placeholders}) "
                    "ORDER BY first_event_id",
                    tuple(state.value for state in states),
                )
            else:
                rows = connection.execute(
                    "SELECT * FROM ws_retention_chunks ORDER BY first_event_id"
                )
            return tuple(self._chunk(row) for row in rows)

    def reserve(
        self,
        records: tuple[KalshiWsOrderBookEventRecord, ...],
        *,
        relative_path: str,
        created_at: datetime,
    ) -> ArchiveChunk:
        first, last = records[0], records[-1]
        chunk_id = f"ws-{first.row_id}-{last.row_id}"
        counts = Counter(record.event_kind.value for record in records)
        tickers = sorted(
            {
                ticker
                for record in records
                for ticker in ((record.ticker,) if record.ticker else record.market_tickers)
            }
        )
        subscriptions = sorted({record.subscription_id for record in records})
        source_times = [record.source_timestamp for record in records if record.source_timestamp]
        with self._connect() as connection, connection:
            overlap = connection.execute(
                """SELECT chunk_id,first_event_id,last_event_id FROM ws_retention_chunks
                WHERE NOT(last_event_id<? OR first_event_id>?)""",
                (first.row_id, last.row_id),
            ).fetchone()
            if overlap is not None:
                if (
                    overlap["chunk_id"] == chunk_id
                    and overlap["first_event_id"] == first.row_id
                    and overlap["last_event_id"] == last.row_id
                ):
                    row = connection.execute(
                        "SELECT * FROM ws_retention_chunks WHERE chunk_id=?", (chunk_id,)
                    ).fetchone()
                    assert row is not None
                    return self._chunk(row)
                raise WsRetentionError("archive chunk range overlaps an existing manifest fact")
            connection.execute(
                """INSERT INTO ws_retention_chunks(
                    chunk_id,first_event_id,last_event_id,event_count,state,relative_path,
                    first_received_timestamp,last_received_timestamp,first_source_timestamp,
                    last_source_timestamp,event_type_counts,tickers,subscription_ids,
                    first_sequence,last_sequence,archive_format_version,codec,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    chunk_id,
                    first.row_id,
                    last.row_id,
                    len(records),
                    ArchiveState.WRITING.value,
                    relative_path,
                    min(record.socket_received_timestamp for record in records).isoformat(),
                    max(record.socket_received_timestamp for record in records).isoformat(),
                    min(source_times).isoformat() if source_times else None,
                    max(source_times).isoformat() if source_times else None,
                    json.dumps(counts, sort_keys=True, separators=(",", ":")),
                    json.dumps(tickers, separators=(",", ":")),
                    json.dumps(subscriptions, separators=(",", ":")),
                    min(record.sequence for record in records),
                    max(record.sequence for record in records),
                    ARCHIVE_FORMAT_VERSION,
                    ARCHIVE_CODEC,
                    created_at.isoformat(),
                    created_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM ws_retention_chunks WHERE chunk_id=?", (chunk_id,)
            ).fetchone()
            assert row is not None
            return self._chunk(row)

    def advance(
        self, chunk_id: str, state: ArchiveState, *, now: datetime, **facts: object
    ) -> None:
        allowed = {
            "logical_checksum",
            "file_checksum",
            "uncompressed_bytes",
            "compressed_bytes",
            "source_replay_hash",
            "archive_replay_hash",
            "end_replay_state",
            "failure",
            "purged_events",
        }
        if set(facts) - allowed:
            raise ValueError("unsupported manifest fact")
        with self._connect() as connection, connection:
            row = connection.execute(
                "SELECT state FROM ws_retention_chunks WHERE chunk_id=?", (chunk_id,)
            ).fetchone()
            if row is None:
                raise WsRetentionError("archive manifest chunk is missing")
            current = ArchiveState(row["state"])
            if current is ArchiveState.FAILED and state is not ArchiveState.FAILED:
                raise WsRetentionError("failed archive chunk cannot advance")
            if state is not ArchiveState.FAILED and _STATE_ORDER[state] < _STATE_ORDER[current]:
                return
            if state is not ArchiveState.FAILED and _STATE_ORDER[state] > _STATE_ORDER[current] + 1:
                raise WsRetentionError("archive manifest state transition skipped verification")
            assignments = ["state=?", "updated_at=?", *(f"{key}=?" for key in facts)]
            connection.execute(
                f"UPDATE ws_retention_chunks SET {','.join(assignments)} WHERE chunk_id=?",
                (state.value, now.isoformat(), *facts.values(), chunk_id),
            )

    def end_state_before(
        self, event_id: int, connection_id: str, subscription_id: int
    ) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT end_replay_state FROM ws_retention_chunks
                WHERE last_event_id<? AND state IN (?,?,?) AND end_replay_state IS NOT NULL
                ORDER BY last_event_id DESC LIMIT 1""",
                (
                    event_id,
                    ArchiveState.COMMITTED.value,
                    ArchiveState.PURGE_ELIGIBLE.value,
                    ArchiveState.PURGED.value,
                ),
            ).fetchone()
            if row is None:
                return None
            state = _ReplayState.from_json(row[0])
            if (state.connection_id, state.subscription_id) != (connection_id, subscription_id):
                return None
            return row[0]

    def last_event_id(self) -> int:
        with self._connect() as connection:
            failed = connection.execute(
                "SELECT chunk_id FROM ws_retention_chunks WHERE state='failed' LIMIT 1"
            ).fetchone()
            if failed is not None:
                raise WsRetentionError("failed archive chunk blocks later retention ranges")
            row = connection.execute(
                "SELECT MAX(last_event_id) FROM ws_retention_chunks"
            ).fetchone()
            return 0 if row is None or row[0] is None else int(row[0])

    def update_purge_progress(self, chunk_id: str, purged: int, *, now: datetime) -> None:
        self.advance(
            chunk_id,
            ArchiveState.PURGE_ELIGIBLE,
            now=now,
            purged_events=purged,
        )

    def metrics(self) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS chunks,
                SUM(state IN (
                    'checksum_verified','replay_verified','committed','purge_eligible','purged'
                )) verified,
                SUM(state IN ('committed','purge_eligible','purged')) retention_verified,
                SUM(state='failed') failed,SUM(state='purge_eligible') eligible,
                COALESCE(SUM(purged_events),0) purged,
                COALESCE(SUM(compressed_bytes),0) compressed,
                COALESCE(SUM(uncompressed_bytes),0) uncompressed,
                MAX(CASE WHEN state IN (
                    'committed','purge_eligible','purged'
                ) THEN updated_at END) last_archive,
                MAX(CASE WHEN state IN (
                    'replay_verified','committed','purge_eligible','purged'
                ) THEN updated_at END) last_replay
                FROM ws_retention_chunks"""
            ).fetchone()
            assert row is not None
            return dict(row)

    def storage_metrics(self, database: Path) -> StorageTierMetrics:
        """Return bounded HOT/COLD tier counters without scanning raw event rows."""

        connection = sqlite3.connect(
            f"file:{database.resolve().as_posix()}?mode=ro", uri=True, timeout=2.0
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            connection.execute("SELECT rootpage FROM sqlite_schema LIMIT 1").fetchone()
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        finally:
            connection.close()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT COALESCE(SUM(compressed_bytes),0),
                MIN(first_received_timestamp),MAX(last_received_timestamp)
                FROM ws_retention_chunks WHERE state IN (?,?,?,?,?,?)""",
                (
                    ArchiveState.WRITTEN.value,
                    ArchiveState.CHECKSUM_VERIFIED.value,
                    ArchiveState.REPLAY_VERIFIED.value,
                    ArchiveState.COMMITTED.value,
                    ArchiveState.PURGE_ELIGIBLE.value,
                    ArchiveState.PURGED.value,
                ),
            ).fetchone()
            verified_raw = connection.execute(
                """SELECT COALESCE(SUM(uncompressed_bytes),0),
                MIN(first_received_timestamp),MAX(last_received_timestamp)
                FROM ws_retention_chunks WHERE state IN (?,?,?)""",
                (
                    ArchiveState.COMMITTED.value,
                    ArchiveState.PURGE_ELIGIBLE.value,
                    ArchiveState.PURGED.value,
                ),
            ).fetchone()
        assert row is not None
        assert verified_raw is not None
        cold_bytes = int(row[0])
        cold_duration_hours = None
        if row[1] is not None and row[2] is not None:
            cold_duration_hours = (
                _parse_time(str(row[2])) - _parse_time(str(row[1]))
            ).total_seconds() / 3600
        cold_per_hour = (
            None
            if cold_duration_hours is None or cold_duration_hours <= 0
            else cold_bytes / cold_duration_hours
        )
        raw_duration_hours = None
        if verified_raw[1] is not None and verified_raw[2] is not None:
            raw_duration_hours = (
                _parse_time(str(verified_raw[2])) - _parse_time(str(verified_raw[1]))
            ).total_seconds() / 3600
        raw_per_hour = (
            None
            if raw_duration_hours is None or raw_duration_hours <= 0
            else int(verified_raw[0]) / raw_duration_hours
        )
        physical = page_count * page_size
        reusable = freelist_count * page_size
        wal = database.with_name(f"{database.name}-wal")
        return StorageTierMetrics(
            hot_sqlite_used_bytes=physical - reusable,
            freelist_reusable_bytes=reusable,
            physical_database_bytes=physical,
            wal_bytes=wal.stat().st_size if wal.exists() else 0,
            cold_archive_bytes=cold_bytes,
            cold_archive_growth_bytes_per_hour=cold_per_hour,
            cold_archive_growth_bytes_per_day=(
                None if cold_per_hour is None else cold_per_hour * 24
            ),
            raw_ws_growth_bytes_per_hour=raw_per_hour,
            raw_ws_growth_bytes_per_day=(None if raw_per_hour is None else raw_per_hour * 24),
            raw_ws_observation_window_seconds=(
                None if raw_duration_hours is None else raw_duration_hours * 3600
            ),
        )

    def record_storage_sample(
        self,
        metrics: StorageTierMetrics,
        *,
        observed_at: datetime | None = None,
        minimum_interval_seconds: float = 60.0,
        maximum_samples: int = 128,
    ) -> StorageGrowthMetrics:
        """Keep bounded low-frequency size samples and report observed net disk growth."""

        if minimum_interval_seconds <= 0 or not 2 <= maximum_samples <= 4096:
            raise ValueError("storage sample bounds are invalid")
        observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
        with self._connect() as connection, connection:
            latest = connection.execute(
                "SELECT observed_at FROM ws_storage_samples ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if (
                latest is None
                or (observed - _parse_time(str(latest[0]))).total_seconds()
                >= minimum_interval_seconds
            ):
                connection.execute(
                    """INSERT OR IGNORE INTO ws_storage_samples(
                    observed_at,physical_database_bytes,wal_bytes,cold_archive_bytes
                    ) VALUES(?,?,?,?)""",
                    (
                        observed.isoformat(),
                        metrics.physical_database_bytes,
                        metrics.wal_bytes,
                        metrics.cold_archive_bytes,
                    ),
                )
                connection.execute(
                    """DELETE FROM ws_storage_samples WHERE id NOT IN(
                    SELECT id FROM ws_storage_samples ORDER BY id DESC LIMIT ?)""",
                    (maximum_samples,),
                )
            oldest = connection.execute(
                """SELECT observed_at,physical_database_bytes,wal_bytes,cold_archive_bytes
                FROM ws_storage_samples ORDER BY id LIMIT 1"""
            ).fetchone()
        if oldest is None:
            return StorageGrowthMetrics(None, None, None)
        elapsed = (observed - _parse_time(str(oldest[0]))).total_seconds()
        if elapsed < minimum_interval_seconds:
            return StorageGrowthMetrics(None, None, None)
        old_total = int(oldest[1]) + int(oldest[2]) + int(oldest[3])
        new_total = metrics.physical_database_bytes + metrics.wal_bytes + metrics.cold_archive_bytes
        per_hour = (new_total - old_total) * 3600 / elapsed
        return StorageGrowthMetrics(elapsed, per_hour, per_hour * 24)

    def latest(self) -> ArchiveChunk | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ws_retention_chunks ORDER BY last_event_id DESC LIMIT 1"
            ).fetchone()
            return None if row is None else self._chunk(row)


def _read_records(
    database: Path,
    *,
    after_id: int,
    cutoff: datetime,
    maximum_records: int,
    exact_range: tuple[int, int] | None = None,
) -> tuple[KalshiWsOrderBookEventRecord, ...]:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=2000")
    try:
        if exact_range is not None:
            rows = connection.execute(
                """SELECT * FROM kalshi_ws_orderbook_events
                WHERE id BETWEEN ? AND ? ORDER BY id""",
                exact_range,
            )
        else:
            next_row = connection.execute(
                """SELECT socket_received_timestamp FROM kalshi_ws_orderbook_events
                WHERE id>? ORDER BY id LIMIT 1""",
                (after_id,),
            ).fetchone()
            if next_row is None or _parse_time(next_row[0]) >= cutoff:
                return ()
            rows = connection.execute(
                """SELECT * FROM kalshi_ws_orderbook_events
                WHERE id>? ORDER BY id LIMIT ?""",
                (after_id, maximum_records),
            )
        records = tuple(RecorderStore._kalshi_ws_event_record(row) for row in rows)
    finally:
        connection.close()
    if not records:
        return ()
    if exact_range is None:
        time_boundary = next(
            (
                index
                for index, record in enumerate(records)
                if record.socket_received_timestamp >= cutoff
            ),
            len(records),
        )
        records = records[:time_boundary]
        if not records:
            return ()
    identity = (records[0].connection_id, records[0].subscription_id)
    boundary = next(
        (
            index
            for index, record in enumerate(records)
            if (record.connection_id, record.subscription_id) != identity
        ),
        len(records),
    )
    return records[:boundary]


class WsArchiveService:
    """Archive one bounded oldest-first range per call; caller controls cadence."""

    def __init__(
        self,
        source_database: Path,
        archive_root: Path,
        manifest: WsRetentionManifest,
        *,
        hot_retention: timedelta = timedelta(hours=6),
        chunk_records: int = 100_000,
    ) -> None:
        if hot_retention <= timedelta(0) or not 1 <= chunk_records <= 250_000:
            raise ValueError("archive retention/chunk bounds are invalid")
        self.source_database = source_database.resolve()
        self.archive_root = archive_root.resolve()
        self.manifest = manifest
        self.hot_retention = hot_retention
        self.chunk_records = chunk_records
        if self.source_database == self.manifest.path:
            raise ValueError("archive manifest must be separate from raw storage")

    def _range_records(self, chunk: ArchiveChunk) -> tuple[KalshiWsOrderBookEventRecord, ...]:
        records = _read_records(
            self.source_database,
            after_id=0,
            cutoff=datetime.max.replace(tzinfo=UTC),
            maximum_records=chunk.event_count,
            exact_range=(chunk.first_event_id, chunk.last_event_id),
        )
        if len(records) != chunk.event_count:
            raise WsRetentionError("archive source range changed before verification")
        return records

    def eligibility(self, now: datetime | None = None) -> RetentionEligibility:
        """Return one bounded, non-blocking retention decision without sleeping."""

        observed = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = observed - self.hot_retention
        after = self.manifest.last_event_id()
        connection = sqlite3.connect(
            f"file:{self.source_database.as_posix()}?mode=ro", uri=True, timeout=2.0
        )
        try:
            row = connection.execute(
                """SELECT id,socket_received_timestamp FROM kalshi_ws_orderbook_events
                WHERE id>? ORDER BY id LIMIT 1""",
                (after,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return RetentionEligibility(
                "WAITING_FOR_SOURCE_DATA",
                observed,
                cutoff,
                None,
                None,
                None,
                None,
                None,
                0,
                False,
            )
        oldest_id = int(row[0])
        oldest_time = _parse_time(row[1])
        if oldest_time >= cutoff:
            return RetentionEligibility(
                "WAITING_FOR_RETENTION_ELIGIBILITY",
                observed,
                cutoff,
                oldest_id,
                oldest_time,
                oldest_time + self.hot_retention,
                None,
                None,
                0,
                False,
            )
        records = _read_records(
            self.source_database,
            after_id=after,
            cutoff=cutoff,
            maximum_records=self.chunk_records,
        )
        if not records:
            raise WsRetentionError("eligible retention boundary produced no bounded records")
        return RetentionEligibility(
            "ELIGIBLE",
            observed,
            cutoff,
            oldest_id,
            oldest_time,
            oldest_time + self.hot_retention,
            records[0].row_id,
            records[-1].row_id,
            len(records),
            len(records) == self.chunk_records,
        )

    def _verify_and_publish(
        self,
        chunk: ArchiveChunk,
        records: tuple[KalshiWsOrderBookEventRecord, ...],
        now: datetime,
    ) -> ArchiveChunk:
        blob, metadata = encode_archive_chunk(records)
        file_checksum = hashlib.sha256(blob).hexdigest()
        destination = (self.archive_root / chunk.relative_path).resolve()
        if self.archive_root != destination.parent and self.archive_root not in destination.parents:
            raise WsRetentionError("archive path escaped configured root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(f"{destination.suffix}.partial")
        try:
            with partial.open("wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            decoded, header = decode_archive_chunk(partial.read_bytes())
            if decoded != records:
                raise WsRetentionError("archive logical events differ from SQLite source")
            if header["checksum_sha256"] != metadata.checksum_sha256:
                raise WsRetentionError("archive logical checksum differs from encoded metadata")
            prior_json = self.manifest.end_state_before(
                chunk.first_event_id, records[0].connection_id, records[0].subscription_id
            )
            source_state = _ReplayState.from_json(prior_json)
            archive_state = _ReplayState.from_json(prior_json)
            for source, archived in zip(records, decoded, strict=True):
                source_state.apply(source)
                archive_state.apply(archived)
            source_hash = source_state.digest()
            archive_hash = archive_state.digest()
            if source_hash != archive_hash or source_state.as_json() != archive_state.as_json():
                raise WsRetentionError("archive deterministic replay state differs from source")
            partial.replace(destination)
            reopened = destination.read_bytes()
            if hashlib.sha256(reopened).hexdigest() != file_checksum:
                raise WsRetentionError("published archive file checksum mismatch")
            verified, _ = decode_archive_chunk(reopened)
            if verified != records:
                raise WsRetentionError("published archive cannot reproduce source events")
            self.manifest.advance(
                chunk.chunk_id,
                ArchiveState.WRITTEN,
                now=now,
                logical_checksum=metadata.checksum_sha256,
                file_checksum=file_checksum,
                uncompressed_bytes=metadata.uncompressed_bytes,
                compressed_bytes=len(blob),
            )
            self.manifest.advance(chunk.chunk_id, ArchiveState.CHECKSUM_VERIFIED, now=now)
            self.manifest.advance(
                chunk.chunk_id,
                ArchiveState.REPLAY_VERIFIED,
                now=now,
                source_replay_hash=source_hash,
                archive_replay_hash=archive_hash,
                end_replay_state=source_state.as_json(),
            )
            self.manifest.advance(chunk.chunk_id, ArchiveState.COMMITTED, now=now)
            self.manifest.advance(chunk.chunk_id, ArchiveState.PURGE_ELIGIBLE, now=now)
        except Exception as error:
            self.manifest.advance(
                chunk.chunk_id,
                ArchiveState.FAILED,
                now=now,
                failure=type(error).__name__,
            )
            raise
        finally:
            partial.unlink(missing_ok=True)
        result = self.manifest.latest()
        if result is None or result.chunk_id != chunk.chunk_id:
            raise WsRetentionError("archive manifest lost the verified chunk")
        return result

    def run_once(self, *, now: datetime | None = None) -> ArchiveRunResult:
        with self.manifest.maintenance_lease():
            return self._run_once(now=now)

    def _run_once(self, *, now: datetime | None = None) -> ArchiveRunResult:
        observed = (now or datetime.now(UTC)).astimezone(UTC)
        started = time.perf_counter()
        incomplete = self.manifest.chunks(
            ArchiveState.WRITING,
            ArchiveState.WRITTEN,
            ArchiveState.CHECKSUM_VERIFIED,
            ArchiveState.REPLAY_VERIFIED,
            ArchiveState.COMMITTED,
        )
        if incomplete:
            chunk = incomplete[0]
            records = self._range_records(chunk)
        else:
            after = self.manifest.last_event_id()
            records = _read_records(
                self.source_database,
                after_id=after,
                cutoff=observed - self.hot_retention,
                maximum_records=self.chunk_records,
            )
            if not records:
                return ArchiveRunResult(None, time.perf_counter() - started, 0.0, 0)
            first = records[0]
            relative = (
                f"{first.socket_received_timestamp:%Y-%m-%d/%H}/"
                f"chunk-{first.row_id}-{records[-1].row_id}.zlib"
            )
            chunk = self.manifest.reserve(records, relative_path=relative, created_at=observed)
        result = self._verify_and_publish(chunk, records, observed)
        elapsed = max(time.perf_counter() - started, 1e-9)
        return ArchiveRunResult(
            result, elapsed, len(records) / elapsed, self.backlog_events(observed)
        )

    def backlog_events(self, now: datetime | None = None) -> int:
        return self.eligibility(now).eligible_rows_bounded

    def hot_metrics(self, now: datetime | None = None) -> dict[str, object]:
        observed = (now or datetime.now(UTC)).astimezone(UTC)
        connection = sqlite3.connect(
            f"file:{self.source_database.as_posix()}?mode=ro", uri=True, timeout=2.0
        )
        connection.row_factory = sqlite3.Row
        try:
            oldest = connection.execute(
                """SELECT id,socket_received_timestamp FROM kalshi_ws_orderbook_events
                ORDER BY id LIMIT 1"""
            ).fetchone()
            newest = connection.execute(
                """SELECT id,socket_received_timestamp FROM kalshi_ws_orderbook_events
                ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        finally:
            connection.close()
        if oldest is None or newest is None:
            return {
                "hot_events_estimate": 0,
                "hot_oldest_timestamp": None,
                "hot_newest_timestamp": None,
                "hot_oldest_age_seconds": None,
            }
        oldest_time = _parse_time(oldest["socket_received_timestamp"])
        newest_time = _parse_time(newest["socket_received_timestamp"])
        return {
            "hot_events_estimate": int(newest["id"]) - int(oldest["id"]) + 1,
            "hot_oldest_timestamp": oldest_time.isoformat(),
            "hot_newest_timestamp": newest_time.isoformat(),
            "hot_oldest_age_seconds": max(0.0, (observed - oldest_time).total_seconds()),
        }


class WsPurgeService:
    def __init__(
        self,
        database: Path,
        archive_root: Path,
        manifest: WsRetentionManifest,
        *,
        batch_rows: int = 20_000,
    ):
        if not 1 <= batch_rows <= 100_000:
            raise ValueError("purge batch must be in [1,100000]")
        self.database = database.resolve()
        self.archive_root = archive_root.resolve()
        self.manifest = manifest
        self.batch_rows = batch_rows

    def verify_preserved_archive(self, chunk: ArchiveChunk) -> None:
        """Reopen and verify immutable archive proof before or after source deletion."""

        if (
            not chunk.file_checksum
            or not chunk.logical_checksum
            or not chunk.source_replay_hash
            or chunk.source_replay_hash != chunk.archive_replay_hash
            or chunk.last_event_id - chunk.first_event_id + 1 != chunk.event_count
        ):
            raise WsRetentionError("purge chunk is not fully verified and contiguous")
        archive = (self.archive_root / chunk.relative_path).resolve()
        if self.archive_root != archive.parent and self.archive_root not in archive.parents:
            raise WsRetentionError("purge archive path escaped configured root")
        if not archive.is_file():
            raise WsRetentionError("purge archive file is unavailable")
        blob = archive.read_bytes()
        if hashlib.sha256(blob).hexdigest() != chunk.file_checksum:
            raise WsRetentionError("purge archive file checksum changed")
        records, header = decode_archive_chunk(blob)
        if (
            len(records) != chunk.event_count
            or records[0].row_id != chunk.first_event_id
            or records[-1].row_id != chunk.last_event_id
            or header.get("checksum_sha256") != chunk.logical_checksum
        ):
            raise WsRetentionError("purge archive facts no longer match the manifest")

    def _authorize(self, chunk: ArchiveChunk) -> None:
        if chunk.state is not ArchiveState.PURGE_ELIGIBLE:
            raise WsRetentionError("purge chunk is not manifest-authorized")
        self.verify_preserved_archive(chunk)

    def run_once(self, *, now: datetime | None = None) -> PurgeRunResult:
        with self.manifest.maintenance_lease():
            return self._run_once(now=now)

    def _run_once(self, *, now: datetime | None = None) -> PurgeRunResult:
        observed = (now or datetime.now(UTC)).astimezone(UTC)
        chunks = self.manifest.chunks(ArchiveState.PURGE_ELIGIBLE)
        if not chunks:
            return PurgeRunResult(None, 0, 0, 0.0, 0, 0, 0, self.database.stat().st_size)
        chunk = chunks[0]
        self._authorize(chunk)
        connection = sqlite3.connect(self.database, timeout=2.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=2000")
        try:
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            started = time.perf_counter()
            connection.execute("BEGIN IMMEDIATE")
            try:
                free_before = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
                remaining_fact = connection.execute(
                    """SELECT COUNT(*),MIN(id),MAX(id) FROM kalshi_ws_orderbook_events
                    WHERE id BETWEEN ? AND ?""",
                    (chunk.first_event_id, chunk.last_event_id),
                ).fetchone()
                assert remaining_fact is not None
                remaining_before = int(remaining_fact[0])
                inferred_purged = chunk.event_count - remaining_before
                if inferred_purged < chunk.purged_events or inferred_purged < 0:
                    raise WsRetentionError("purge range facts conflict with source database")
                if remaining_before and (
                    int(remaining_fact[1]) != chunk.first_event_id + inferred_purged
                    or int(remaining_fact[2]) != chunk.last_event_id
                ):
                    raise WsRetentionError("purge range is not an exact contiguous suffix")
                cursor = connection.execute(
                    """DELETE FROM kalshi_ws_orderbook_events WHERE id IN(
                    SELECT id FROM kalshi_ws_orderbook_events WHERE id BETWEEN ? AND ?
                    ORDER BY id LIMIT ?)""",
                    (chunk.first_event_id, chunk.last_event_id, self.batch_rows),
                )
                free_after = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            elapsed = time.perf_counter() - started
            deleted = cursor.rowcount
            purged = inferred_purged + deleted
            remaining = chunk.event_count - purged
            if remaining == 0:
                self.manifest.advance(
                    chunk.chunk_id,
                    ArchiveState.PURGED,
                    now=observed,
                    purged_events=purged,
                )
            else:
                self.manifest.update_purge_progress(chunk.chunk_id, purged, now=observed)
            return PurgeRunResult(
                chunk.chunk_id,
                deleted,
                remaining,
                elapsed,
                free_before,
                free_after,
                (free_after - free_before) * page_size,
                self.database.stat().st_size,
            )
        finally:
            connection.close()


def compact_database_offline(
    source: Path,
    destination: Path,
    *,
    minimum_free_bytes: int,
) -> dict[str, int]:
    """Build and verify a compact copy; caller owns pause/swap/rollback orchestration."""

    source = source.resolve()
    destination = destination.resolve()
    if source == destination or destination.exists():
        raise WsRetentionError("offline compaction destination must be new")
    wal = source.with_name(f"{source.name}-wal")
    if wal.exists() and wal.stat().st_size:
        raise WsRetentionError("offline compaction requires a stopped, WAL-free database")
    free = shutil.disk_usage(source.parent).free
    required = source.stat().st_size + minimum_free_bytes
    if free < required:
        raise WsRetentionError("insufficient disk headroom for rollback-safe compaction")
    connection = sqlite3.connect(source)
    try:
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        )
        source_counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }
        escaped = str(destination).replace("'", "''")
        connection.execute(f"VACUUM INTO '{escaped}'")
    finally:
        connection.close()
    verified = sqlite3.connect(destination)
    try:
        integrity = verified.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = verified.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_keys:
            raise WsRetentionError("compacted database verification failed")
        destination_tables = tuple(
            str(row[0])
            for row in verified.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        )
        if destination_tables != tables:
            raise WsRetentionError("compacted database table inventory changed")
        for table, expected in source_counts.items():
            actual = int(verified.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            if actual != expected:
                raise WsRetentionError("compacted database row counts changed")
    finally:
        verified.close()
    return {
        "source_bytes": source.stat().st_size,
        "compacted_bytes": destination.stat().st_size,
        "free_bytes_before": free,
    }


def checkpoint_stopped_database(source: Path) -> None:
    """Truncate WAL only after the managed recorder has been confirmed stopped."""

    source = source.resolve()
    connection = sqlite3.connect(source, timeout=5.0)
    try:
        busy, _log, remaining = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy or remaining:
            raise WsRetentionError("offline WAL checkpoint is busy")
    finally:
        connection.close()


def swap_compacted_database(
    source: Path,
    compacted: Path,
    rollback: Path,
) -> CompactionSwapResult:
    """Atomically swap a verified compact copy while retaining an exact rollback source."""

    started = time.perf_counter()
    source = source.resolve()
    compacted = compacted.resolve()
    rollback = rollback.resolve()
    if not source.is_file() or not compacted.is_file() or rollback.exists():
        raise WsRetentionError("invalid compaction swap paths")
    if len({source, compacted, rollback}) != 3 or not (
        source.parent == compacted.parent == rollback.parent
    ):
        raise WsRetentionError("compaction swap must use distinct files in one directory")
    old_bytes = source.stat().st_size
    compacted_bytes = compacted.stat().st_size
    source.replace(rollback)
    try:
        compacted.replace(source)
    except BaseException:
        rollback.replace(source)
        raise
    return CompactionSwapResult(
        old_bytes=old_bytes,
        compacted_bytes=compacted_bytes,
        reclaimed_bytes=old_bytes - compacted_bytes,
        rollback_path=rollback,
        duration_seconds=time.perf_counter() - started,
    )


def rollback_compacted_database(source: Path, rollback: Path, failed_copy: Path) -> None:
    """Restore the retained source if post-swap recorder acceptance fails."""

    source = source.resolve()
    rollback = rollback.resolve()
    failed_copy = failed_copy.resolve()
    if not source.is_file() or not rollback.is_file() or failed_copy.exists():
        raise WsRetentionError("invalid rollback paths")
    source.replace(failed_copy)
    try:
        rollback.replace(source)
    except BaseException:
        failed_copy.replace(source)
        raise
