"""Read-only SDK WebSocket shadow reliability and parity bridge.

This module deliberately never writes the Recorder database.  SDK messages are
normalized into a small canonical envelope and then applied through LIVE15's
existing atomic order-book coordinator so shadow and a future promoted path use
the same sequence, quarantine, and resynchronization semantics.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from live15_quant.kalshi_ws import (
    KalshiAtomicOrderBookCoordinator,
    KalshiBookInvariantError,
    KalshiBookSide,
    KalshiOrderBookDelta,
    KalshiOrderBookSnapshot,
    KalshiSequenceGapError,
    KalshiUnsynchronizedBookError,
    OrderBookLevel,
    SynchronizedKalshiOrderBook,
)
from live15_quant.models import Asset
from live15_quant.runtime_status import read_json

SHADOW_SCHEMA_VERSION = 1
SHADOW_PROVENANCE = "kalshi_sdk_v12_ws_shadow"


class CanonicalWsEventType(StrEnum):
    SNAPSHOT = "SNAPSHOT"
    DELTA = "DELTA"
    TICKER = "TICKER"
    LIFECYCLE = "LIFECYCLE"
    GAP = "GAP"
    RECONNECT = "RECONNECT"


class ShadowSyncState(StrEnum):
    WAITING_SNAPSHOT = "WAITING_SNAPSHOT"
    SYNCHRONIZED = "SYNCHRONIZED"
    UNSYNCHRONIZED = "UNSYNCHRONIZED"
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class CanonicalWsEvent:
    asset: Asset
    ticker: str
    event_type: CanonicalWsEventType
    sequence: int | None
    exchange_timestamp: datetime | None
    receive_timestamp: datetime
    subscription_id: int | None
    connection_id: str
    payload: Mapping[str, object]
    provenance: str = SHADOW_PROVENANCE


@dataclass(slots=True)
class ShadowAssetState:
    ticker: str
    state: ShadowSyncState = ShadowSyncState.WAITING_SNAPSHOT
    last_frame_at: datetime | None = None
    last_book_at: datetime | None = None
    last_quote_at: datetime | None = None
    snapshots: int = 0
    deltas: int = 0
    gaps: int = 0
    reconnects: int = 0
    stale_seconds: float = 0.0
    last_lifecycle: str | None = None


@dataclass(frozen=True, slots=True)
class ParityResult:
    asset: Asset
    ticker: str
    compared_at: datetime
    old_timestamp: datetime | None
    new_timestamp: datetime
    ticker_match: bool | None
    best_bid_match: bool | None
    best_ask_match: bool | None
    top_depth_match: bool | None
    lifecycle_match: bool | None
    aligned: bool
    mismatch_reason: str | None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("shadow timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else _aware(value).isoformat()


def _sdk_source_timestamp(payload: object) -> datetime | None:
    ts_ms = getattr(payload, "ts_ms", None)
    if isinstance(ts_ms, int) and not isinstance(ts_ms, bool):
        return datetime.fromtimestamp(ts_ms / 1000, UTC)
    raw = getattr(payload, "ts", None)
    return raw.astimezone(UTC) if isinstance(raw, datetime) else None


def _levels(values: Mapping[object, object]) -> tuple[OrderBookLevel, ...]:
    parsed = tuple(
        OrderBookLevel(Decimal(str(price)), Decimal(str(quantity)))
        for price, quantity in values.items()
    )
    return tuple(sorted(parsed, key=lambda level: level.price, reverse=True))


def _asset_for_ticker(asset_by_ticker: Mapping[str, Asset], ticker: str) -> Asset:
    try:
        return asset_by_ticker[ticker]
    except KeyError:
        raise ValueError("SDK WebSocket ticker is outside the shadow universe") from None


def canonical_from_sdk(
    message: object,
    *,
    asset_by_ticker: Mapping[str, Asset],
    connection_id: str,
    received_at: datetime,
) -> tuple[CanonicalWsEvent, KalshiOrderBookSnapshot | KalshiOrderBookDelta | None]:
    """Map one SDK typed message without importing or copying SDK internals."""

    received = _aware(received_at)
    kind = str(getattr(message, "type", ""))
    payload = getattr(message, "msg", None)
    ticker = str(getattr(payload, "market_ticker", ""))
    asset = _asset_for_ticker(asset_by_ticker, ticker)
    sid = getattr(message, "sid", None)
    seq = getattr(message, "seq", None)
    if not isinstance(sid, int) or sid < 1:
        raise ValueError("SDK WebSocket subscription identity is invalid")
    if seq is not None and (not isinstance(seq, int) or seq < 1):
        raise ValueError("SDK WebSocket sequence identity is invalid")

    if kind == "orderbook_snapshot":
        market_id = str(getattr(payload, "market_id", ""))
        yes = getattr(payload, "yes", None)
        no = getattr(payload, "no", None)
        if not market_id or not isinstance(yes, Mapping) or not isinstance(no, Mapping):
            raise ValueError("SDK orderbook snapshot is incomplete")
        yes_levels = _levels(yes)
        no_levels = _levels(no)
        canonical = CanonicalWsEvent(
            asset=asset,
            ticker=ticker,
            event_type=CanonicalWsEventType.SNAPSHOT,
            sequence=seq,
            exchange_timestamp=None,
            receive_timestamp=received,
            subscription_id=sid,
            connection_id=connection_id,
            payload={
                "market_id": market_id,
                "yes_bids": [[str(item.price), str(item.quantity)] for item in yes_levels],
                "no_bids": [[str(item.price), str(item.quantity)] for item in no_levels],
            },
        )
        if seq is None:
            raise ValueError("SDK orderbook snapshot sequence is missing")
        return canonical, KalshiOrderBookSnapshot(
            connection_id=connection_id,
            subscription_id=sid,
            sequence=seq,
            ticker=ticker,
            market_id=market_id,
            yes_bids=yes_levels,
            no_bids=no_levels,
            source_timestamp=None,
            socket_received_timestamp=received,
            parse_timestamp=received,
            provenance=SHADOW_PROVENANCE,
        )

    if kind == "orderbook_delta":
        market_id = str(getattr(payload, "market_id", ""))
        side = str(getattr(payload, "side", ""))
        if not market_id or side not in {"yes", "no"} or seq is None:
            raise ValueError("SDK orderbook delta is incomplete")
        price = Decimal(str(getattr(payload, "price", "")))
        quantity_delta = Decimal(str(getattr(payload, "delta", "")))
        source_timestamp = _sdk_source_timestamp(payload)
        canonical = CanonicalWsEvent(
            asset=asset,
            ticker=ticker,
            event_type=CanonicalWsEventType.DELTA,
            sequence=seq,
            exchange_timestamp=source_timestamp,
            receive_timestamp=received,
            subscription_id=sid,
            connection_id=connection_id,
            payload={
                "market_id": market_id,
                "side": side,
                "price": str(price),
                "quantity_delta": str(quantity_delta),
            },
        )
        return canonical, KalshiOrderBookDelta(
            connection_id=connection_id,
            subscription_id=sid,
            sequence=seq,
            ticker=ticker,
            market_id=market_id,
            side=KalshiBookSide.YES if side == "yes" else KalshiBookSide.NO,
            price=price,
            quantity_delta=quantity_delta,
            source_timestamp=source_timestamp,
            socket_received_timestamp=received,
            parse_timestamp=received,
            provenance=SHADOW_PROVENANCE,
        )

    if kind == "ticker":
        source_timestamp = _sdk_source_timestamp(payload)
        if source_timestamp is None:
            raise ValueError("SDK ticker timestamp is missing")
        return CanonicalWsEvent(
            asset=asset,
            ticker=ticker,
            event_type=CanonicalWsEventType.TICKER,
            sequence=seq,
            exchange_timestamp=source_timestamp,
            receive_timestamp=received,
            subscription_id=sid,
            connection_id=connection_id,
            payload={
                "market_id": getattr(payload, "market_id", None),
                "yes_bid": str(getattr(payload, "yes_bid", "")),
                "yes_ask": str(getattr(payload, "yes_ask", "")),
                "yes_bid_size": str(getattr(payload, "yes_bid_size", "")),
                "yes_ask_size": str(getattr(payload, "yes_ask_size", "")),
                "volume": str(getattr(payload, "volume", "")),
            },
        ), None

    if kind == "market_lifecycle_v2":
        lifecycle = str(getattr(payload, "event_type", ""))
        if not lifecycle:
            raise ValueError("SDK lifecycle event type is missing")
        exchange_timestamp = None
        for name in ("settled_ts", "determination_ts", "close_ts", "open_ts"):
            raw = getattr(payload, name, None)
            if isinstance(raw, int) and not isinstance(raw, bool):
                exchange_timestamp = datetime.fromtimestamp(raw, UTC)
                break
        return CanonicalWsEvent(
            asset=asset,
            ticker=ticker,
            event_type=CanonicalWsEventType.LIFECYCLE,
            sequence=seq,
            exchange_timestamp=exchange_timestamp,
            receive_timestamp=received,
            subscription_id=sid,
            connection_id=connection_id,
            payload={
                "event_type": lifecycle,
                "event_ticker": getattr(payload, "event_ticker", None),
                "result": getattr(payload, "result", None),
                "exchange_index": getattr(payload, "exchange_index", None),
            },
        ), None
    raise ValueError("unsupported SDK WebSocket typed message")


class ShadowTelemetryStore:
    """Independent append-only telemetry; never opens the Recorder database."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS shadow_metadata(
              key TEXT PRIMARY KEY,value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              receive_timestamp TEXT NOT NULL,exchange_timestamp TEXT,
              asset TEXT NOT NULL,ticker TEXT NOT NULL,event_type TEXT NOT NULL,
              sequence INTEGER,subscription_id INTEGER,connection_id TEXT NOT NULL,
              synchronized INTEGER NOT NULL,payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_gaps(
              id INTEGER PRIMARY KEY AUTOINCREMENT,detected_at TEXT NOT NULL,
              connection_id TEXT NOT NULL,subscription_id INTEGER,
              expected_sequence INTEGER,received_sequence INTEGER,
              affected_assets_json TEXT NOT NULL,recovered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS shadow_reconnects(
              id INTEGER PRIMARY KEY AUTOINCREMENT,observed_at TEXT NOT NULL,
              old_state TEXT NOT NULL,new_state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_parity(
              id INTEGER PRIMARY KEY AUTOINCREMENT,compared_at TEXT NOT NULL,
              old_timestamp TEXT,new_timestamp TEXT NOT NULL,asset TEXT NOT NULL,
              ticker TEXT NOT NULL,ticker_match INTEGER,best_bid_match INTEGER,
              best_ask_match INTEGER,top_depth_match INTEGER,lifecycle_match INTEGER,
              aligned INTEGER NOT NULL,mismatch_reason TEXT
            );
            """
        )
        self.connection.execute(
            "INSERT OR REPLACE INTO shadow_metadata(key,value) VALUES('schema_version',?)",
            (str(SHADOW_SCHEMA_VERSION),),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def record_event(self, event: CanonicalWsEvent, *, synchronized: bool) -> None:
        self.connection.execute(
            """INSERT INTO shadow_events(
              receive_timestamp,exchange_timestamp,asset,ticker,event_type,sequence,
              subscription_id,connection_id,synchronized,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                _timestamp(event.receive_timestamp),
                _timestamp(event.exchange_timestamp),
                event.asset.value,
                event.ticker,
                event.event_type.value,
                event.sequence,
                event.subscription_id,
                event.connection_id,
                int(synchronized),
                json.dumps(event.payload, sort_keys=True, separators=(",", ":"), default=str),
            ),
        )
        self.connection.commit()

    def record_gap(
        self,
        *,
        detected_at: datetime,
        connection_id: str,
        subscription_id: int | None,
        expected_sequence: int | None,
        received_sequence: int | None,
        affected_assets: tuple[Asset, ...],
    ) -> int:
        cursor = self.connection.execute(
            """INSERT INTO shadow_gaps(
              detected_at,connection_id,subscription_id,expected_sequence,
              received_sequence,affected_assets_json
            ) VALUES(?,?,?,?,?,?)""",
            (
                _timestamp(detected_at),
                connection_id,
                subscription_id,
                expected_sequence,
                received_sequence,
                json.dumps([asset.value for asset in affected_assets]),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def recover_gaps(self, recovered_at: datetime) -> None:
        self.connection.execute(
            "UPDATE shadow_gaps SET recovered_at=? WHERE recovered_at IS NULL",
            (_timestamp(recovered_at),),
        )
        self.connection.commit()

    def record_reconnect(self, observed_at: datetime, old_state: str, new_state: str) -> None:
        self.connection.execute(
            "INSERT INTO shadow_reconnects(observed_at,old_state,new_state) VALUES(?,?,?)",
            (_timestamp(observed_at), old_state, new_state),
        )
        self.connection.commit()

    def record_parity(self, result: ParityResult) -> None:
        def flag(value: bool | None) -> int | None:
            return None if value is None else int(value)

        self.connection.execute(
            """INSERT INTO shadow_parity(
              compared_at,old_timestamp,new_timestamp,asset,ticker,ticker_match,
              best_bid_match,best_ask_match,top_depth_match,lifecycle_match,
              aligned,mismatch_reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _timestamp(result.compared_at),
                _timestamp(result.old_timestamp),
                _timestamp(result.new_timestamp),
                result.asset.value,
                result.ticker,
                flag(result.ticker_match),
                flag(result.best_bid_match),
                flag(result.best_ask_match),
                flag(result.top_depth_match),
                flag(result.lifecycle_match),
                int(result.aligned),
                result.mismatch_reason,
            ),
        )
        self.connection.commit()

    def summary(self) -> dict[str, object]:
        event_counts = Counter(
            {
                str(row["event_type"]): int(row["count"])
                for row in self.connection.execute(
                    "SELECT event_type,COUNT(*) count FROM shadow_events GROUP BY event_type"
                )
            }
        )
        parity = self.connection.execute(
            """SELECT COUNT(*) comparisons,
              SUM(CASE WHEN aligned=1 THEN 1 ELSE 0 END) aligned,
              SUM(CASE WHEN aligned=1 AND ticker_match=1 THEN 1 ELSE 0 END) ticker_matches,
              SUM(CASE WHEN aligned=1 AND best_bid_match=1 THEN 1 ELSE 0 END) bid_matches,
              SUM(CASE WHEN aligned=1 AND best_ask_match=1 THEN 1 ELSE 0 END) ask_matches,
              SUM(CASE WHEN aligned=1 AND top_depth_match=1 THEN 1 ELSE 0 END) depth_matches,
              SUM(CASE WHEN aligned=1 AND mismatch_reason IS NOT NULL THEN 1 ELSE 0 END) mismatches
              FROM shadow_parity"""
        ).fetchone()
        gaps = self.connection.execute("SELECT COUNT(*) count FROM shadow_gaps").fetchone()
        reconnects = self.connection.execute(
            "SELECT COUNT(*) count FROM shadow_reconnects WHERE new_state='reconnecting'"
        ).fetchone()
        gap_rows = self.connection.execute(
            "SELECT detected_at,recovered_at FROM shadow_gaps"
        ).fetchall()
        recovery_seconds: list[float] = []
        for gap_row in gap_rows:
            if gap_row["recovered_at"] is None:
                continue
            detected = datetime.fromisoformat(str(gap_row["detected_at"])).astimezone(UTC)
            recovered = datetime.fromisoformat(str(gap_row["recovered_at"])).astimezone(UTC)
            recovery_seconds.append(max(0.0, (recovered - detected).total_seconds()))
        comparisons = int(parity["comparisons"] or 0)
        aligned = int(parity["aligned"] or 0)

        asset_rows = self.connection.execute(
            """SELECT asset,COUNT(*) comparisons,
              SUM(CASE WHEN aligned=1 THEN 1 ELSE 0 END) aligned,
              SUM(CASE WHEN aligned=1 AND ticker_match=1 THEN 1 ELSE 0 END) ticker_matches,
              SUM(CASE WHEN aligned=1 AND best_bid_match=1 THEN 1 ELSE 0 END) bid_matches,
              SUM(CASE WHEN aligned=1 AND best_ask_match=1 THEN 1 ELSE 0 END) ask_matches,
              SUM(CASE WHEN aligned=1 AND top_depth_match=1 THEN 1 ELSE 0 END) depth_matches,
              SUM(CASE WHEN aligned=1 AND lifecycle_match=1 THEN 1 ELSE 0 END) lifecycle_matches,
              SUM(CASE WHEN aligned=1 AND mismatch_reason IS NOT NULL THEN 1 ELSE 0 END) mismatches
              FROM shadow_parity GROUP BY asset ORDER BY asset"""
        ).fetchall()

        def rate(name: str) -> float | None:
            return None if aligned == 0 else int(parity[name] or 0) / aligned

        per_asset: dict[str, object] = {}
        for row in asset_rows:
            asset_aligned = int(row["aligned"] or 0)
            per_asset[str(row["asset"])] = {
                "comparisons": int(row["comparisons"] or 0),
                "aligned_comparisons": asset_aligned,
                "mismatch_count": int(row["mismatches"] or 0),
                "ticker_match_rate": (
                    None if asset_aligned == 0 else int(row["ticker_matches"] or 0) / asset_aligned
                ),
                "best_bid_match_rate": (
                    None if asset_aligned == 0 else int(row["bid_matches"] or 0) / asset_aligned
                ),
                "best_ask_match_rate": (
                    None if asset_aligned == 0 else int(row["ask_matches"] or 0) / asset_aligned
                ),
                "top_depth_match_rate": (
                    None if asset_aligned == 0 else int(row["depth_matches"] or 0) / asset_aligned
                ),
                "lifecycle_match_rate": (
                    None
                    if asset_aligned == 0
                    else int(row["lifecycle_matches"] or 0) / asset_aligned
                ),
            }

        return {
            "snapshot_count": event_counts[CanonicalWsEventType.SNAPSHOT.value],
            "delta_count": event_counts[CanonicalWsEventType.DELTA.value],
            "ticker_count": event_counts[CanonicalWsEventType.TICKER.value],
            "lifecycle_count": event_counts[CanonicalWsEventType.LIFECYCLE.value],
            "gap_count": int(gaps["count"] or 0),
            "reconnect_count": int(reconnects["count"] or 0),
            "unrecovered_gap_count": len(gap_rows) - len(recovery_seconds),
            "average_resubscribe_recovery_seconds": (
                sum(recovery_seconds) / len(recovery_seconds) if recovery_seconds else None
            ),
            "max_resubscribe_recovery_seconds": (
                max(recovery_seconds) if recovery_seconds else None
            ),
            "parity_comparisons": comparisons,
            "aligned_comparisons": aligned,
            "recent_mismatch_count": int(parity["mismatches"] or 0),
            "ticker_match_rate": rate("ticker_matches"),
            "best_bid_match_rate": rate("bid_matches"),
            "best_ask_match_rate": rate("ask_matches"),
            "top_depth_match_rate": rate("depth_matches"),
            "per_asset": per_asset,
        }


class ShadowParityComparator:
    def __init__(self, old_projection_path: Path, *, alignment_seconds: float = 1.0) -> None:
        if alignment_seconds <= 0:
            raise ValueError("shadow parity alignment must be positive")
        self.old_projection_path = old_projection_path
        self.alignment_seconds = alignment_seconds

    @staticmethod
    def _best_prices(book: Mapping[str, object]) -> tuple[Decimal | None, Decimal | None]:
        yes = book.get("yes_bids")
        no = book.get("no_bids")
        yes_rows = yes if isinstance(yes, list) else []
        no_rows = no if isinstance(no, list) else []
        bid = Decimal(str(yes_rows[0][0])) if yes_rows else None
        ask = Decimal(1) - Decimal(str(no_rows[0][0])) if no_rows else None
        return bid, ask

    @staticmethod
    def _shadow_book(book: SynchronizedKalshiOrderBook) -> dict[str, object]:
        return {
            "yes_bids": [[str(item.price), str(item.quantity)] for item in book.yes_bids[:3]],
            "no_bids": [[str(item.price), str(item.quantity)] for item in book.no_bids[:3]],
        }

    def compare(
        self,
        *,
        asset: Asset,
        book: SynchronizedKalshiOrderBook,
        lifecycle: str | None,
        compared_at: datetime,
    ) -> ParityResult:
        observed = _aware(compared_at)
        old = read_json(self.old_projection_path)
        if not isinstance(old, dict) or old.get("state") != "SYNCHRONIZED":
            return ParityResult(
                asset,
                book.ticker,
                observed,
                None,
                book.received_timestamp,
                None,
                None,
                None,
                None,
                None,
                False,
                "OLD_WS_UNAVAILABLE",
            )
        published_raw = old.get("published_at")
        try:
            published = datetime.fromisoformat(str(published_raw)).astimezone(UTC)
        except (TypeError, ValueError):
            return ParityResult(
                asset,
                book.ticker,
                observed,
                None,
                book.received_timestamp,
                None,
                None,
                None,
                None,
                None,
                False,
                "OLD_TIMESTAMP_INVALID",
            )
        books = old.get("books")
        old_book = books.get(book.ticker) if isinstance(books, dict) else None
        if not isinstance(old_book, dict):
            return ParityResult(
                asset,
                book.ticker,
                observed,
                published,
                book.received_timestamp,
                False,
                None,
                None,
                None,
                None,
                False,
                "TICKER_MISMATCH",
            )
        old_book_timestamp = published
        old_book_timestamp_raw = old_book.get("book_received_at")
        if old_book_timestamp_raw is not None:
            try:
                old_book_timestamp = datetime.fromisoformat(str(old_book_timestamp_raw)).astimezone(
                    UTC
                )
            except (TypeError, ValueError):
                return ParityResult(
                    asset,
                    book.ticker,
                    observed,
                    None,
                    book.received_timestamp,
                    True,
                    None,
                    None,
                    None,
                    None,
                    False,
                    "OLD_BOOK_TIMESTAMP_INVALID",
                )
        age = abs((old_book_timestamp - book.received_timestamp).total_seconds())
        if age > self.alignment_seconds:
            return ParityResult(
                asset,
                book.ticker,
                observed,
                old_book_timestamp,
                book.received_timestamp,
                True,
                None,
                None,
                None,
                None,
                False,
                "ALIGNMENT_WINDOW_EXCEEDED",
            )
        new_projection = self._shadow_book(book)
        old_bid, old_ask = self._best_prices(old_book)
        new_bid, new_ask = self._best_prices(new_projection)
        bid_match = old_bid == new_bid
        ask_match = old_ask == new_ask
        depth_match = (
            old_book.get("yes_bids", [])[:3] == new_projection["yes_bids"]
            and old_book.get("no_bids", [])[:3] == new_projection["no_bids"]
        )
        old_current = old.get("current_tickers")
        lifecycle_match = book.ticker in old_current if isinstance(old_current, list) else None
        reasons = []
        if not bid_match:
            reasons.append("BEST_BID")
        if not ask_match:
            reasons.append("BEST_ASK")
        if not depth_match:
            reasons.append("TOP_DEPTH")
        if lifecycle_match is False:
            reasons.append("LIFECYCLE")
        return ParityResult(
            asset=asset,
            ticker=book.ticker,
            compared_at=observed,
            old_timestamp=old_book_timestamp,
            new_timestamp=book.received_timestamp,
            ticker_match=True,
            best_bid_match=bid_match,
            best_ask_match=ask_match,
            top_depth_match=depth_match,
            lifecycle_match=lifecycle_match,
            aligned=True,
            mismatch_reason="|".join(reasons) or None,
        )


class KalshiSdkReliabilityAdapter:
    """LIVE15 reliability layer around SDK typed market-data messages."""

    def __init__(
        self,
        asset_by_ticker: Mapping[str, Asset],
        store: ShadowTelemetryStore,
        comparator: ShadowParityComparator,
        *,
        connection_id: str | None = None,
        stale_seconds: float = 10.0,
    ) -> None:
        if len(asset_by_ticker) == 0 or stale_seconds <= 0:
            raise ValueError("shadow universe and freshness threshold are required")
        self.asset_by_ticker = dict(asset_by_ticker)
        self.store = store
        self.comparator = comparator
        self.connection_id = connection_id or f"sdk-shadow-{uuid.uuid4().hex}"
        self.stale_seconds = stale_seconds
        self.coordinator = KalshiAtomicOrderBookCoordinator(
            self.connection_id, tuple(self.asset_by_ticker)
        )
        self.assets = {
            asset: ShadowAssetState(ticker=ticker) for ticker, asset in self.asset_by_ticker.items()
        }
        self.books: dict[Asset, SynchronizedKalshiOrderBook] = {}
        self.last_sequence: int | None = None
        self.last_state = "disconnected"
        self.started_at = datetime.now(UTC)
        self._last_health_at = self.started_at

    def _record_reliability_event(
        self,
        event_type: CanonicalWsEventType,
        observed_at: datetime,
        payload: Mapping[str, object],
    ) -> None:
        for ticker, asset in self.asset_by_ticker.items():
            self.store.record_event(
                CanonicalWsEvent(
                    asset=asset,
                    ticker=ticker,
                    event_type=event_type,
                    sequence=None,
                    exchange_timestamp=None,
                    receive_timestamp=_aware(observed_at),
                    subscription_id=self.coordinator.subscription_id,
                    connection_id=self.connection_id,
                    payload=payload,
                ),
                synchronized=False,
            )

    def _mark_all_unsynchronized(self, *, reconnect: bool = False) -> None:
        self.coordinator.reset()
        self.books.clear()
        self.last_sequence = None
        for state in self.assets.values():
            state.state = ShadowSyncState.UNSYNCHRONIZED
            if reconnect:
                state.reconnects += 1

    def connection_state_changed(
        self, old_state: str, new_state: str, observed_at: datetime
    ) -> None:
        old = old_state.lower()
        new = new_state.lower()
        self.last_state = new
        self.store.record_reconnect(observed_at, old, new)
        self._record_reliability_event(
            CanonicalWsEventType.RECONNECT,
            observed_at,
            {"old_state": old, "new_state": new},
        )
        if new in {"connecting", "reconnecting", "disconnected", "closed"}:
            self._mark_all_unsynchronized(reconnect=new == "reconnecting")

    def accept(self, message: object, *, received_at: datetime) -> CanonicalWsEvent:
        event, book_message = canonical_from_sdk(
            message,
            asset_by_ticker=self.asset_by_ticker,
            connection_id=self.connection_id,
            received_at=received_at,
        )
        state = self.assets[event.asset]
        state.last_frame_at = event.receive_timestamp
        if event.event_type is CanonicalWsEventType.LIFECYCLE:
            state.last_lifecycle = str(event.payload["event_type"])
            self.store.record_event(event, synchronized=state.state is ShadowSyncState.SYNCHRONIZED)
            return event
        if event.event_type is CanonicalWsEventType.TICKER:
            state.last_quote_at = event.receive_timestamp
            self.store.record_event(event, synchronized=state.state is ShadowSyncState.SYNCHRONIZED)
            return event
        assert book_message is not None
        if event.event_type is CanonicalWsEventType.SNAPSHOT:
            state.snapshots += 1
        else:
            state.deltas += 1
        expected = None if self.last_sequence is None else self.last_sequence + 1
        try:
            book = self.coordinator.accept(book_message)
        except KalshiSequenceGapError:
            affected = tuple(sorted(self.assets, key=lambda item: item.value))
            self.store.record_gap(
                detected_at=event.receive_timestamp,
                connection_id=self.connection_id,
                subscription_id=event.subscription_id,
                expected_sequence=expected,
                received_sequence=event.sequence,
                affected_assets=affected,
            )
            self._record_reliability_event(
                CanonicalWsEventType.GAP,
                event.receive_timestamp,
                {
                    "expected_sequence": expected,
                    "received_sequence": event.sequence,
                    "subscription_id": event.subscription_id,
                },
            )
            for item in self.assets.values():
                item.state = ShadowSyncState.UNSYNCHRONIZED
                item.gaps += 1
            self.books.clear()
            if not isinstance(book_message, KalshiOrderBookSnapshot):
                self.store.record_event(event, synchronized=False)
                return event
            # The SDK gap path requests an authoritative fresh snapshot.  The
            # coordinator has already entered awaiting-resync state, so retrying
            # this exact snapshot establishes the new baseline without applying
            # the dropped delta.
            book = self.coordinator.accept(book_message)
        except KalshiBookInvariantError as error:
            self._mark_all_unsynchronized()
            if str(error) != "WebSocket subscription identity mismatch" or not isinstance(
                book_message, KalshiOrderBookSnapshot
            ):
                self.store.record_event(event, synchronized=False)
                return event
            # The SDK assigns a new sid after a targeted resubscribe.  That
            # fresh snapshot is authoritative, but the old subscription must
            # first be quarantined and the transition must remain observable.
            affected = tuple(sorted(self.assets, key=lambda item: item.value))
            self.store.record_gap(
                detected_at=event.receive_timestamp,
                connection_id=self.connection_id,
                subscription_id=event.subscription_id,
                expected_sequence=expected,
                received_sequence=event.sequence,
                affected_assets=affected,
            )
            self._record_reliability_event(
                CanonicalWsEventType.GAP,
                event.receive_timestamp,
                {
                    "expected_sequence": expected,
                    "received_sequence": event.sequence,
                    "subscription_id": event.subscription_id,
                    "reason": "SDK_RESUBSCRIBE_SID_CHANGED",
                },
            )
            for item in self.assets.values():
                item.gaps += 1
            book = self.coordinator.accept(book_message)
        self.last_sequence = event.sequence
        if book is not None:
            synchronized = set(self.coordinator.synchronized_tickers)
            for ticker, asset in self.asset_by_ticker.items():
                asset_state = self.assets[asset]
                if ticker in synchronized:
                    asset_state.state = ShadowSyncState.SYNCHRONIZED
                    try:
                        current = self.coordinator.book(ticker)
                    except KalshiUnsynchronizedBookError:
                        continue
                    self.books[asset] = current
                    asset_state.last_book_at = current.received_timestamp
                    asset_state.last_quote_at = current.received_timestamp
                else:
                    asset_state.state = ShadowSyncState.WAITING_SNAPSHOT
            if len(synchronized) == len(self.asset_by_ticker):
                self.store.recover_gaps(event.receive_timestamp)
            current = self.books.get(event.asset)
            if current is not None:
                parity = self.comparator.compare(
                    asset=event.asset,
                    book=current,
                    lifecycle=state.last_lifecycle,
                    compared_at=event.receive_timestamp,
                )
                self.store.record_parity(parity)
        synchronized_event = state.state is ShadowSyncState.SYNCHRONIZED
        self.store.record_event(event, synchronized=synchronized_event)
        return event

    def health(self, observed_at: datetime) -> dict[str, object]:
        observed = _aware(observed_at)
        elapsed = max(0.0, (observed - self._last_health_at).total_seconds())
        self._last_health_at = observed
        synchronized = 0
        per_asset: dict[str, object] = {}
        for asset, state in sorted(self.assets.items(), key=lambda item: item[0].value):
            frame_age = (
                None
                if state.last_frame_at is None
                else max(0.0, (observed - state.last_frame_at).total_seconds())
            )
            book_age = (
                None
                if state.last_book_at is None
                else max(0.0, (observed - state.last_book_at).total_seconds())
            )
            effective = state.state
            if frame_age is not None and frame_age > self.stale_seconds:
                state.stale_seconds += elapsed
                effective = ShadowSyncState.STALE
            if effective is ShadowSyncState.SYNCHRONIZED:
                synchronized += 1
            per_asset[asset.value] = {
                "ticker": state.ticker,
                "state": effective.value,
                "last_frame_age_seconds": frame_age,
                "book_age_seconds": book_age,
                "quote_age_seconds": book_age,
                "snapshots": state.snapshots,
                "deltas": state.deltas,
                "gaps": state.gaps,
                "reconnects": state.reconnects,
                "stale_seconds": state.stale_seconds,
                "last_lifecycle": state.last_lifecycle,
            }
        return {
            "connected_status": self.last_state.upper(),
            "synchronized_count": synchronized,
            "subscribed_assets": len(self.assets),
            "assets": per_asset,
            "metrics": self.store.summary(),
        }
