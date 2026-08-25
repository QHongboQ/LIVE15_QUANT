"""Independent atomic recorder for the SDK reliability shadow path."""

from __future__ import annotations

import json
import sqlite3
import zlib
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from live15_quant.kalshi_gateway.canonical_ws import CanonicalEventType, CanonicalSdkEvent
from live15_quant.kalshi_ws import SynchronizedKalshiOrderBook
from live15_quant.models import Asset, OrderBookLevel

SHADOW_RECORDER_SCHEMA_VERSION = 4
SHADOW_RECORDER_PROVENANCE = "sdk_reliability_shadow_only"


class RestSanityStatus(StrEnum):
    EXACT_MATCH = "EXACT_MATCH"
    MOVED_DURING_READ = "MOVED_DURING_READ"
    UNAVAILABLE = "UNAVAILABLE"
    TRUE_MISMATCH = "TRUE_MISMATCH"


@dataclass(frozen=True, slots=True)
class GapRecord:
    expected_sequence: int | None
    received_sequence: int | None
    subscription_id: int | None
    reason: str
    affected_assets: tuple[Asset, ...]


@dataclass(frozen=True, slots=True)
class RestSanityResult:
    asset: Asset
    ticker: str
    checked_at: datetime
    status: RestSanityStatus
    ws_sequence: int | None
    ws_yes_bid: Decimal | None
    ws_yes_ask: Decimal | None
    ws_no_bid: Decimal | None
    ws_no_ask: Decimal | None
    rest_yes_bid: Decimal | None
    rest_yes_ask: Decimal | None
    rest_no_bid: Decimal | None
    rest_no_ask: Decimal | None
    reason: str | None = None
    request_started_at: datetime | None = None
    response_received_at: datetime | None = None
    aligned_ws_at: datetime | None = None
    alignment_delta_ms: float | None = None


@dataclass(frozen=True, slots=True)
class BookPriceSample:
    observed_at: datetime
    sequence: int
    prices: tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("shadow recorder timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _best(levels: tuple[OrderBookLevel, ...]) -> Decimal | None:
    return levels[0].price if levels else None


def executable_prices(
    yes_bids: tuple[OrderBookLevel, ...],
    no_bids: tuple[OrderBookLevel, ...],
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    yes_bid = _best(yes_bids)
    no_bid = _best(no_bids)
    yes_ask = None if no_bid is None else Decimal(1) - no_bid
    no_ask = None if yes_bid is None else Decimal(1) - yes_bid
    return yes_bid, yes_ask, no_bid, no_ask


def _rest_levels(raw: object) -> tuple[OrderBookLevel, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("REST orderbook side is malformed")
    levels = tuple(
        OrderBookLevel(Decimal(str(item.price)), Decimal(str(item.quantity))) for item in raw
    )
    if any(level.price < 0 or level.price > 1 or level.quantity <= 0 for level in levels):
        raise ValueError("REST orderbook side is malformed")
    return tuple(sorted(levels, key=lambda item: item.price, reverse=True))


def compare_rest_orderbook(
    *,
    asset: Asset,
    ticker: str,
    checked_at: datetime,
    ws_book: SynchronizedKalshiOrderBook,
    rest_orderbook: object,
    request_started_at: datetime | None = None,
    response_received_at: datetime | None = None,
    aligned_sample: BookPriceSample | None = None,
    interval_samples: tuple[BookPriceSample, ...] = (),
    alignment_tolerance_seconds: float = 0.25,
) -> RestSanityResult:
    """Compare REST with canonical WS history around the response timestamp."""

    rest_ticker = str(getattr(rest_orderbook, "ticker", ""))
    if rest_ticker != ticker:
        raise ValueError("REST orderbook ticker identity mismatch")
    rest_yes = _rest_levels(getattr(rest_orderbook, "yes", None))
    rest_no = _rest_levels(getattr(rest_orderbook, "no", None))
    if alignment_tolerance_seconds <= 0:
        raise ValueError("REST alignment tolerance must be positive")
    ws_prices = executable_prices(ws_book.yes_bids, ws_book.no_bids)
    rest_prices = executable_prices(rest_yes, rest_no)
    aligned_prices = None if aligned_sample is None else aligned_sample.prices
    alignment_delta_ms = None
    if aligned_sample is not None and response_received_at is not None:
        alignment_delta_ms = abs(
            (aligned_sample.observed_at - response_received_at).total_seconds() * 1000
        )
    if (
        aligned_sample is None
        or alignment_delta_ms is None
        or alignment_delta_ms > alignment_tolerance_seconds * 1000
    ):
        status = RestSanityStatus.UNAVAILABLE
        reason = "NO_WS_SAMPLE_WITHIN_ALIGNMENT_WINDOW"
    elif aligned_prices == rest_prices:
        status = RestSanityStatus.EXACT_MATCH
        reason = None
    elif any(sample.prices == rest_prices for sample in interval_samples):
        status = RestSanityStatus.MOVED_DURING_READ
        reason = "MATCHED_WS_SAMPLE_DURING_REST_READ"
    else:
        status = RestSanityStatus.TRUE_MISMATCH
        reason = "NO_MATCHING_WS_SAMPLE_IN_BOUNDED_REQUEST_WINDOW"
    return RestSanityResult(
        asset=asset,
        ticker=ticker,
        checked_at=checked_at,
        status=status,
        ws_sequence=(ws_book.sequence if aligned_sample is None else aligned_sample.sequence),
        ws_yes_bid=(ws_prices[0] if aligned_prices is None else aligned_prices[0]),
        ws_yes_ask=(ws_prices[1] if aligned_prices is None else aligned_prices[1]),
        ws_no_bid=(ws_prices[2] if aligned_prices is None else aligned_prices[2]),
        ws_no_ask=(ws_prices[3] if aligned_prices is None else aligned_prices[3]),
        rest_yes_bid=rest_prices[0],
        rest_yes_ask=rest_prices[1],
        rest_no_bid=rest_prices[2],
        rest_no_ask=rest_prices[3],
        reason=reason,
        request_started_at=request_started_at,
        response_received_at=response_received_at,
        aligned_ws_at=(None if aligned_sample is None else aligned_sample.observed_at),
        alignment_delta_ms=alignment_delta_ms,
    )


class SdkReliabilityShadowRecorder:
    """Append-only shadow store with bounded, ordered delta persistence."""

    def __init__(
        self,
        path: Path,
        *,
        official_recorder_path: Path | None = None,
        commit_batch_size: int = 1,
    ) -> None:
        if commit_batch_size < 1:
            raise ValueError("shadow recorder commit batch size must be positive")
        self.path = path.resolve()
        if official_recorder_path is not None and self.path == official_recorder_path.resolve():
            raise ValueError("SDK reliability shadow must not open the official Recorder database")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        # This is an isolated, reproducible shadow store rather than the
        # official Recorder ledger. WAL+NORMAL preserves transaction atomicity
        # and database consistency while avoiding a disk flush for every
        # high-frequency delta on Windows. FULL synchronous here can block the
        # asyncio loop long enough to miss SDK transport keepalives.
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._commit_batch_size = commit_batch_size
        self._pending_events = 0
        self._delta_rows: list[tuple[object, ...]] = []
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS shadow_metadata(
              key TEXT PRIMARY KEY,value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS canonical_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              receive_timestamp TEXT NOT NULL,exchange_timestamp TEXT,
              asset TEXT NOT NULL,ticker TEXT NOT NULL,event_type TEXT NOT NULL,
              sequence INTEGER,subscription_id INTEGER,connection_id TEXT NOT NULL,
              authoritative INTEGER NOT NULL,payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS canonical_delta_batches(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              first_receive_timestamp TEXT NOT NULL,
              last_receive_timestamp TEXT NOT NULL,
              event_count INTEGER NOT NULL CHECK(event_count > 0),
              compression TEXT NOT NULL CHECK(compression = 'zlib-json-v1'),
              payload BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS validated_books(
              event_id INTEGER PRIMARY KEY REFERENCES canonical_events(id),
              sequence INTEGER NOT NULL,market_id TEXT NOT NULL,
              yes_bids_json TEXT NOT NULL,no_bids_json TEXT NOT NULL,
              yes_best_bid TEXT,yes_best_ask TEXT,no_best_bid TEXT,no_best_ask TEXT,
              top_depth_json TEXT NOT NULL,book_timestamp TEXT NOT NULL,
              source_timestamp TEXT,provenance TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS asset_state_transitions(
              id INTEGER PRIMARY KEY AUTOINCREMENT,event_id INTEGER,
              observed_at TEXT NOT NULL,asset TEXT NOT NULL,ticker TEXT NOT NULL,
              old_state TEXT NOT NULL,new_state TEXT NOT NULL,reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sequence_gaps(
              id INTEGER PRIMARY KEY AUTOINCREMENT,event_id INTEGER NOT NULL,
              detected_at TEXT NOT NULL,connection_id TEXT NOT NULL,
              subscription_id INTEGER,expected_sequence INTEGER,received_sequence INTEGER,
              reason TEXT NOT NULL,affected_assets_json TEXT NOT NULL,recovered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS diagnostics(
              id INTEGER PRIMARY KEY AUTOINCREMENT,event_id INTEGER,
              observed_at TEXT NOT NULL,asset TEXT,ticker TEXT,
              diagnostic_type TEXT NOT NULL,detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rollovers(
              id INTEGER PRIMARY KEY AUTOINCREMENT,observed_at TEXT NOT NULL,
              reason TEXT NOT NULL,old_tickers_json TEXT NOT NULL,new_tickers_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rest_sanity(
              id INTEGER PRIMARY KEY AUTOINCREMENT,checked_at TEXT NOT NULL,
              asset TEXT NOT NULL,ticker TEXT NOT NULL,status TEXT NOT NULL,
              ws_sequence INTEGER,ws_yes_bid TEXT,ws_yes_ask TEXT,ws_no_bid TEXT,ws_no_ask TEXT,
              rest_yes_bid TEXT,rest_yes_ask TEXT,rest_no_bid TEXT,rest_no_ask TEXT,reason TEXT
            );
            CREATE TABLE IF NOT EXISTS reconnect_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,observed_at TEXT NOT NULL,
              initiator TEXT NOT NULL,close_code INTEGER,close_reason TEXT,
              exception_type TEXT,last_frame_age_seconds REAL,
              affected_assets_json TEXT NOT NULL,rollover_in_progress INTEGER NOT NULL
            );
            """
        )
        existing_rest_columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(rest_sanity)")
        }
        for name, data_type in (
            ("request_started_at", "TEXT"),
            ("response_received_at", "TEXT"),
            ("aligned_ws_at", "TEXT"),
            ("alignment_delta_ms", "REAL"),
        ):
            if name not in existing_rest_columns:
                self.connection.execute(f"ALTER TABLE rest_sanity ADD COLUMN {name} {data_type}")
        self.connection.execute(
            "INSERT OR REPLACE INTO shadow_metadata(key,value) VALUES('schema_version',?)",
            (str(SHADOW_RECORDER_SCHEMA_VERSION),),
        )
        self.connection.execute(
            "INSERT OR REPLACE INTO shadow_metadata(key,value) VALUES('provenance',?)",
            (SHADOW_RECORDER_PROVENANCE,),
        )
        self.connection.commit()
        self._summary_cache = self._load_summary_cache()

    def _load_summary_cache(self) -> dict[str, object]:
        """Scan historical facts once, before the live socket is connected."""

        event_counts = Counter(
            {
                str(row["event_type"]): int(row["count"])
                for row in self.connection.execute(
                    "SELECT event_type,COUNT(*) count FROM canonical_events GROUP BY event_type"
                )
            }
        )
        event_counts[CanonicalEventType.DELTA.value] += int(
            self.connection.execute(
                "SELECT COALESCE(SUM(event_count),0) FROM canonical_delta_batches"
            ).fetchone()[0]
        )
        gaps = self.connection.execute(
            """SELECT COUNT(*) total,
              SUM(CASE WHEN recovered_at IS NULL THEN 1 ELSE 0 END) unrecovered
              FROM sequence_gaps"""
        ).fetchone()
        sanity_rows = self.connection.execute(
            "SELECT status,COUNT(*) count FROM rest_sanity GROUP BY status"
        ).fetchall()
        sanity_by_asset = self.connection.execute(
            """SELECT asset,
              SUM(CASE WHEN status IN ('PASS','EXACT_MATCH') THEN 1 ELSE 0 END) pass_count,
              SUM(CASE WHEN status IN ('MISMATCH','TRUE_MISMATCH') THEN 1 ELSE 0 END)
                mismatch_count,
              COUNT(*) total
              FROM rest_sanity GROUP BY asset ORDER BY asset"""
        ).fetchall()
        return {
            "event_counts": event_counts,
            "validated_book_count": int(
                self.connection.execute("SELECT COUNT(*) FROM validated_books").fetchone()[0]
            ),
            "gap_count": int(gaps["total"] or 0),
            "unrecovered_gap_count": int(gaps["unrecovered"] or 0),
            "rollover_count": int(
                self.connection.execute("SELECT COUNT(*) FROM rollovers").fetchone()[0]
            ),
            "rest_sanity": Counter({str(row["status"]): int(row["count"]) for row in sanity_rows}),
            "rest_sanity_by_asset": {
                str(row["asset"]): {
                    "pass_count": int(row["pass_count"] or 0),
                    "mismatch_count": int(row["mismatch_count"] or 0),
                    "total": int(row["total"] or 0),
                }
                for row in sanity_by_asset
            },
            "reconnect_count": int(
                self.connection.execute("SELECT COUNT(*) FROM reconnect_events").fetchone()[0]
            ),
        }

    def _count_event(
        self,
        event: CanonicalSdkEvent,
        *,
        book: SynchronizedKalshiOrderBook | None,
        gap: GapRecord | None,
        recover_open_gaps: bool,
    ) -> None:
        counts = self._summary_cache["event_counts"]
        assert isinstance(counts, Counter)
        counts[event.event_type.value] += 1
        if book is not None and event.event_type is CanonicalEventType.SNAPSHOT:
            self._summary_cache["validated_book_count"] = (
                int(self._summary_cache["validated_book_count"]) + 1
            )
        if gap is not None:
            self._summary_cache["gap_count"] = int(self._summary_cache["gap_count"]) + 1
            self._summary_cache["unrecovered_gap_count"] = (
                int(self._summary_cache["unrecovered_gap_count"]) + 1
            )
        if recover_open_gaps:
            self._summary_cache["unrecovered_gap_count"] = 0

    @contextmanager
    def _event_transaction(self) -> Iterator[None]:
        if self._commit_batch_size == 1:
            with self.connection:
                yield
            return
        if not self.connection.in_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute("SAVEPOINT canonical_event")
        try:
            yield
        except Exception:
            self.connection.execute("ROLLBACK TO canonical_event")
            self.connection.execute("RELEASE canonical_event")
            raise
        self.connection.execute("RELEASE canonical_event")
        self._pending_events += 1
        if self._pending_events >= self._commit_batch_size:
            self.flush()

    def flush(self) -> None:
        if self._delta_rows:
            payload = zlib.compress(
                json.dumps(self._delta_rows, separators=(",", ":")).encode("utf-8"),
                level=1,
            )
            self.connection.execute(
                """INSERT INTO canonical_delta_batches(
                  first_receive_timestamp,last_receive_timestamp,event_count,
                  compression,payload
                ) VALUES(?,?,?,?,?)""",
                (
                    self._delta_rows[0][0],
                    self._delta_rows[-1][0],
                    len(self._delta_rows),
                    "zlib-json-v1",
                    payload,
                ),
            )
            self._delta_rows.clear()
        if self.connection.in_transaction:
            self.connection.commit()
        self._pending_events = 0

    @staticmethod
    def _event_payload(event: CanonicalSdkEvent) -> dict[str, object]:
        return {
            "market_id": event.market_id,
            "delta_side": event.delta_side,
            "delta_price": None if event.delta_price is None else str(event.delta_price),
            "delta_quantity": (None if event.delta_quantity is None else str(event.delta_quantity)),
            "lifecycle_type": event.lifecycle_type,
            "lifecycle_result": event.lifecycle_result,
            "event_ticker": event.event_ticker,
            "exchange_index": event.exchange_index,
            "diagnostic": event.diagnostic,
        }

    def record_validated(
        self,
        event: CanonicalSdkEvent,
        *,
        authoritative: bool,
        book: SynchronizedKalshiOrderBook | None,
        transitions: tuple[tuple[Asset, str, str, str], ...] = (),
        transition_tickers: dict[Asset, str] | None = None,
        gap: GapRecord | None = None,
        recover_open_gaps: bool = False,
        diagnostic: tuple[str, str] | None = None,
    ) -> int:
        """Persist event and all derived reliability facts atomically."""

        event_row = (
            _timestamp(event.sdk_receive_timestamp),
            _timestamp(event.exchange_timestamp),
            event.asset.value,
            event.ticker,
            event.event_type.value,
            event.sequence,
            event.subscription_id,
            event.connection_id,
            int(authoritative),
            json.dumps(self._event_payload(event), sort_keys=True, separators=(",", ":")),
        )
        if (
            event.event_type is CanonicalEventType.DELTA
            and not transitions
            and gap is None
            and not recover_open_gaps
            and diagnostic is None
        ):
            # The common Production hot path has no dependent facts needing
            # an event id. Buffer it in receive order and let SQLite execute
            # the bounded batch in C. This retains every canonical delta and
            # strict ordering while avoiding one Python/SQLite crossing per
            # wire frame. Any snapshot, gap or diagnostic flushes first.
            self._delta_rows.append(event_row)
            self._count_event(
                event,
                book=book,
                gap=gap,
                recover_open_gaps=recover_open_gaps,
            )
            if self._pending_events + len(self._delta_rows) >= self._commit_batch_size:
                self.flush()
            return -1

        self.flush()

        with self._event_transaction():
            cursor = self.connection.execute(
                """INSERT INTO canonical_events(
                  receive_timestamp,exchange_timestamp,asset,ticker,event_type,sequence,
                  subscription_id,connection_id,authoritative,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                event_row,
            )
            event_id = int(cursor.lastrowid)
            # Persist the full immutable book only as a snapshot checkpoint.
            # Every accepted delta is retained in the ordered compressed
            # canonical_delta_batches stream, so another full depth JSON copy
            # per delta is redundant. Snapshot + ordered batches remains a
            # complete replayable representation.
            if book is not None and event.event_type is CanonicalEventType.SNAPSHOT:
                yes_bid, yes_ask, no_bid, no_ask = executable_prices(book.yes_bids, book.no_bids)
                yes_json = json.dumps(
                    [[str(level.price), str(level.quantity)] for level in book.yes_bids],
                    separators=(",", ":"),
                )
                no_json = json.dumps(
                    [[str(level.price), str(level.quantity)] for level in book.no_bids],
                    separators=(",", ":"),
                )
                top_depth = json.dumps(
                    {
                        "yes": json.loads(yes_json)[:3],
                        "no": json.loads(no_json)[:3],
                    },
                    separators=(",", ":"),
                )
                self.connection.execute(
                    """INSERT INTO validated_books(
                      event_id,sequence,market_id,yes_bids_json,no_bids_json,
                      yes_best_bid,yes_best_ask,no_best_bid,no_best_ask,top_depth_json,
                      book_timestamp,source_timestamp,provenance
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        event_id,
                        book.sequence,
                        book.market_id,
                        yes_json,
                        no_json,
                        None if yes_bid is None else str(yes_bid),
                        None if yes_ask is None else str(yes_ask),
                        None if no_bid is None else str(no_bid),
                        None if no_ask is None else str(no_ask),
                        top_depth,
                        _timestamp(book.received_timestamp),
                        _timestamp(book.source_timestamp),
                        SHADOW_RECORDER_PROVENANCE,
                    ),
                )
            for asset, old, new, reason in transitions:
                self.connection.execute(
                    """INSERT INTO asset_state_transitions(
                      event_id,observed_at,asset,ticker,old_state,new_state,reason
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        event_id,
                        _timestamp(event.sdk_receive_timestamp),
                        asset.value,
                        (
                            transition_tickers[asset]
                            if transition_tickers is not None
                            else event.ticker
                        ),
                        old,
                        new,
                        reason,
                    ),
                )
            if gap is not None:
                self.connection.execute(
                    """INSERT INTO sequence_gaps(
                      event_id,detected_at,connection_id,subscription_id,
                      expected_sequence,received_sequence,reason,affected_assets_json
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        event_id,
                        _timestamp(event.sdk_receive_timestamp),
                        event.connection_id,
                        gap.subscription_id,
                        gap.expected_sequence,
                        gap.received_sequence,
                        gap.reason,
                        json.dumps([asset.value for asset in gap.affected_assets]),
                    ),
                )
            if recover_open_gaps:
                self.connection.execute(
                    "UPDATE sequence_gaps SET recovered_at=? WHERE recovered_at IS NULL",
                    (_timestamp(event.sdk_receive_timestamp),),
                )
            if diagnostic is not None:
                self.connection.execute(
                    """INSERT INTO diagnostics(
                      event_id,observed_at,asset,ticker,diagnostic_type,detail
                    ) VALUES(?,?,?,?,?,?)""",
                    (
                        event_id,
                        _timestamp(event.sdk_receive_timestamp),
                        event.asset.value,
                        event.ticker,
                        diagnostic[0],
                        diagnostic[1][:500],
                    ),
                )
        self._count_event(
            event,
            book=book,
            gap=gap,
            recover_open_gaps=recover_open_gaps,
        )
        return event_id

    def record_rollover(
        self,
        *,
        observed_at: datetime,
        reason: str,
        old_tickers: tuple[str, ...],
        new_tickers: tuple[str, ...],
    ) -> None:
        self.flush()
        with self.connection:
            self.connection.execute(
                """INSERT INTO rollovers(
                  observed_at,reason,old_tickers_json,new_tickers_json
                ) VALUES(?,?,?,?)""",
                (
                    _timestamp(observed_at),
                    reason,
                    json.dumps(list(old_tickers)),
                    json.dumps(list(new_tickers)),
                ),
            )
        self._summary_cache["rollover_count"] = int(self._summary_cache["rollover_count"]) + 1

    def record_state_transitions(
        self,
        *,
        observed_at: datetime,
        transitions: tuple[tuple[Asset, str, str, str], ...],
        tickers: dict[Asset, str],
    ) -> None:
        self.flush()
        with self.connection:
            for asset, old, new, reason in transitions:
                self.connection.execute(
                    """INSERT INTO asset_state_transitions(
                      event_id,observed_at,asset,ticker,old_state,new_state,reason
                    ) VALUES(NULL,?,?,?,?,?,?)""",
                    (
                        _timestamp(observed_at),
                        asset.value,
                        tickers[asset],
                        old,
                        new,
                        reason,
                    ),
                )

    def record_diagnostic(
        self,
        *,
        observed_at: datetime,
        diagnostic_type: str,
        detail: str,
        asset: Asset | None = None,
        ticker: str | None = None,
    ) -> None:
        self.flush()
        with self.connection:
            self.connection.execute(
                """INSERT INTO diagnostics(
                  event_id,observed_at,asset,ticker,diagnostic_type,detail
                ) VALUES(NULL,?,?,?,?,?)""",
                (
                    _timestamp(observed_at),
                    None if asset is None else asset.value,
                    ticker,
                    diagnostic_type[:120],
                    detail[:500],
                ),
            )

    def record_reconnect(
        self,
        *,
        observed_at: datetime,
        initiator: str,
        close_code: int | None,
        close_reason: str | None,
        exception_type: str | None,
        last_frame_age_seconds: float | None,
        affected_assets: tuple[Asset, ...],
        rollover_in_progress: bool,
    ) -> None:
        if initiator not in {
            "SDK_INTERNAL",
            "SERVER_INITIATED",
            "LIVE15_INITIATED",
            "SUPERVISOR_RESTART",
            "NETWORK_ERROR",
            "UNKNOWN",
        }:
            raise ValueError("invalid reconnect initiator")
        self.flush()
        with self.connection:
            self.connection.execute(
                """INSERT INTO reconnect_events(
                  observed_at,initiator,close_code,close_reason,exception_type,
                  last_frame_age_seconds,affected_assets_json,rollover_in_progress
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    _timestamp(observed_at),
                    initiator,
                    close_code,
                    None if close_reason is None else close_reason[:200],
                    None if exception_type is None else exception_type[:120],
                    last_frame_age_seconds,
                    json.dumps([asset.value for asset in affected_assets]),
                    int(rollover_in_progress),
                ),
            )
        self._summary_cache["reconnect_count"] = int(self._summary_cache["reconnect_count"]) + 1

    def record_rest_sanity(self, result: RestSanityResult) -> None:
        def value(raw: Decimal | None) -> str | None:
            return None if raw is None else str(raw)

        self.flush()
        with self.connection:
            self.connection.execute(
                """INSERT INTO rest_sanity(
                  checked_at,asset,ticker,status,ws_sequence,
                  ws_yes_bid,ws_yes_ask,ws_no_bid,ws_no_ask,
                  rest_yes_bid,rest_yes_ask,rest_no_bid,rest_no_ask,reason,
                  request_started_at,response_received_at,aligned_ws_at,alignment_delta_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _timestamp(result.checked_at),
                    result.asset.value,
                    result.ticker,
                    result.status.value,
                    result.ws_sequence,
                    value(result.ws_yes_bid),
                    value(result.ws_yes_ask),
                    value(result.ws_no_bid),
                    value(result.ws_no_ask),
                    value(result.rest_yes_bid),
                    value(result.rest_yes_ask),
                    value(result.rest_no_bid),
                    value(result.rest_no_ask),
                    result.reason,
                    _timestamp(result.request_started_at),
                    _timestamp(result.response_received_at),
                    _timestamp(result.aligned_ws_at),
                    result.alignment_delta_ms,
                ),
            )
        sanity = self._summary_cache["rest_sanity"]
        assert isinstance(sanity, Counter)
        sanity[result.status.value] += 1
        by_asset = self._summary_cache["rest_sanity_by_asset"]
        assert isinstance(by_asset, dict)
        asset_counts = by_asset.setdefault(
            result.asset.value,
            {"pass_count": 0, "mismatch_count": 0, "total": 0},
        )
        asset_counts["total"] += 1
        if result.status is RestSanityStatus.EXACT_MATCH:
            asset_counts["pass_count"] += 1
        if result.status is RestSanityStatus.TRUE_MISMATCH:
            asset_counts["mismatch_count"] += 1

    def summary(self) -> dict[str, object]:
        self.flush()
        event_counts = self._summary_cache["event_counts"]
        sanity = self._summary_cache["rest_sanity"]
        sanity_by_asset = self._summary_cache["rest_sanity_by_asset"]
        assert isinstance(event_counts, Counter)
        assert isinstance(sanity, Counter)
        assert isinstance(sanity_by_asset, dict)
        return {
            "event_count": sum(event_counts.values()),
            "snapshot_count": event_counts["SNAPSHOT"],
            "delta_count": event_counts["DELTA"],
            "ticker_count": event_counts["TICKER"],
            "lifecycle_count": event_counts["LIFECYCLE"],
            "unknown_lifecycle_count": event_counts["UNKNOWN_LIFECYCLE"],
            "payload_invalid_count": event_counts["PAYLOAD_INVALID"],
            "reconnect_event_count": event_counts["RECONNECT"],
            "validated_book_count": int(self._summary_cache["validated_book_count"]),
            "gap_count": int(self._summary_cache["gap_count"]),
            "unrecovered_gap_count": int(self._summary_cache["unrecovered_gap_count"]),
            "rollover_count": int(self._summary_cache["rollover_count"]),
            "rest_sanity": dict(sanity),
            "rest_sanity_by_asset": {
                str(asset): dict(counts) for asset, counts in sanity_by_asset.items()
            },
            "reconnect_count": int(self._summary_cache["reconnect_count"]),
        }

    def close(self) -> None:
        self.flush()
        self.connection.close()
