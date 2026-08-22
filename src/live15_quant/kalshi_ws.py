"""Typed, read-only Kalshi WebSocket orderbook protocol and state machine.

The module contains no account or order operation. It models Kalshi's documented
snapshot-first ``orderbook_delta`` channel, whose sequence is scoped to a server
subscription id rather than to an individual market.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol

from live15_quant.models import DataRole, OrderBookLevel


class KalshiMarketDataProvenance(StrEnum):
    REST = "kalshi_rest"
    WEBSOCKET = "kalshi_ws"


KALSHI_WS_PROVENANCE = KalshiMarketDataProvenance.WEBSOCKET.value
KALSHI_WS_DOCS = "https://docs.kalshi.com/getting_started/quick_start_websockets"
KALSHI_ORDERBOOK_DOCS = "https://docs.kalshi.com/websockets/orderbook-updates"
_TICKER = re.compile(r"^[A-Z0-9-]+$")


class KalshiBookSide(StrEnum):
    YES = "yes"
    NO = "no"


class KalshiBookSyncStatus(StrEnum):
    SYNCHRONIZED = "synchronized"
    UNSYNCHRONIZED = "unsynchronized"


class KalshiWsRuntimeState(StrEnum):
    CONNECTING = "connecting"
    WAITING_SNAPSHOT = "waiting_snapshot"
    SYNCHRONIZED = "synchronized"
    UNSYNCHRONIZED = "unsynchronized"
    RECONNECTING = "reconnecting"


class KalshiWsEventKind(StrEnum):
    SNAPSHOT = "orderbook_snapshot"
    DELTA = "orderbook_delta"
    SUBSCRIPTION_ACK = "subscription_ack"


class KalshiWsPayloadError(ValueError):
    """A documented WebSocket payload is malformed or has the wrong identity."""


class KalshiSequenceGapError(RuntimeError):
    """The subscription sequence is no longer complete and requires a snapshot."""

    def __init__(self, message: str, *, tickers: tuple[str, ...]) -> None:
        super().__init__(message)
        self.tickers = tickers


class KalshiBookInvariantError(RuntimeError):
    """A delta would produce an impossible orderbook fact."""


class KalshiUnsynchronizedBookError(RuntimeError):
    """A caller attempted to consume a book that is not synchronized."""


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _monotonic(value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or value < 0):
        raise ValueError("socket receive monotonic timestamp must be non-negative")


def _monotonic_order(received: int | None, enqueued: int | None) -> None:
    _monotonic(received)
    _monotonic(enqueued)
    if received is not None and enqueued is not None and enqueued < received:
        raise ValueError("enqueue monotonic timestamp cannot precede socket receive")


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise KalshiWsPayloadError(f"malformed Kalshi WebSocket {field}")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise KalshiWsPayloadError(f"malformed Kalshi WebSocket {field}") from None
    if not parsed.is_finite():
        raise KalshiWsPayloadError(f"malformed Kalshi WebSocket {field}")
    return parsed


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise KalshiWsPayloadError(f"malformed Kalshi WebSocket {field}")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        raise KalshiWsPayloadError(f"malformed Kalshi WebSocket {field}") from None
    if str(parsed) != str(value) or parsed < minimum:
        raise KalshiWsPayloadError(f"malformed Kalshi WebSocket {field}")
    return parsed


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KalshiWsPayloadError(f"malformed Kalshi WebSocket {field}")
    return value


def _ticker(value: object) -> str:
    result = _identity(value, "market_ticker")
    if _TICKER.fullmatch(result) is None:
        raise KalshiWsPayloadError("malformed Kalshi WebSocket market_ticker")
    return result


def _source_timestamp(message: Mapping[str, object]) -> datetime | None:
    raw_ms = message.get("ts_ms")
    raw_text = message.get("ts")
    if raw_ms is None and raw_text is None:
        return None
    by_ms: datetime | None = None
    by_text: datetime | None = None
    if raw_ms is not None:
        milliseconds = _integer(raw_ms, "ts_ms")
        seconds, remainder = divmod(milliseconds, 1000)
        try:
            by_ms = datetime.fromtimestamp(seconds, UTC).replace(microsecond=remainder * 1000)
        except (OSError, OverflowError, ValueError):
            raise KalshiWsPayloadError("malformed Kalshi WebSocket ts_ms") from None
    if raw_text is not None:
        if isinstance(raw_text, (int, Decimal)) and not isinstance(raw_text, bool):
            seconds = _integer(raw_text, "ts")
            try:
                by_text = datetime.fromtimestamp(seconds, UTC)
            except (OSError, OverflowError, ValueError):
                raise KalshiWsPayloadError("malformed Kalshi WebSocket ts") from None
        elif isinstance(raw_text, str):
            try:
                by_text = datetime.fromisoformat(raw_text.replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                raise KalshiWsPayloadError("malformed Kalshi WebSocket ts") from None
        else:
            raise KalshiWsPayloadError("malformed Kalshi WebSocket ts")
    if by_ms is not None and by_text is not None and abs((by_ms - by_text).total_seconds()) >= 1:
        raise KalshiWsPayloadError("Kalshi WebSocket timestamp fields conflict")
    return by_ms or by_text


def _levels(value: object, field: str) -> tuple[OrderBookLevel, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise KalshiWsPayloadError(f"malformed Kalshi WebSocket {field}")
    levels: dict[Decimal, Decimal] = {}
    for raw in value:
        if not isinstance(raw, list) or len(raw) != 2:
            raise KalshiWsPayloadError(f"malformed Kalshi WebSocket {field}")
        price = _decimal(raw[0], f"{field} price")
        quantity = _decimal(raw[1], f"{field} quantity")
        if not Decimal(0) <= price <= Decimal(1) or quantity <= 0 or price in levels:
            raise KalshiWsPayloadError(f"malformed Kalshi WebSocket {field}")
        levels[price] = quantity
    return tuple(OrderBookLevel(price, levels[price]) for price in sorted(levels, reverse=True))


@dataclass(frozen=True, slots=True)
class KalshiOrderBookSnapshot:
    connection_id: str
    subscription_id: int
    sequence: int
    ticker: str
    market_id: str
    yes_bids: tuple[OrderBookLevel, ...]
    no_bids: tuple[OrderBookLevel, ...]
    source_timestamp: datetime | None
    socket_received_timestamp: datetime
    parse_timestamp: datetime
    socket_received_monotonic_ns: int | None = None
    enqueue_timestamp: datetime | None = None
    enqueue_monotonic_ns: int | None = None
    provenance: str = KALSHI_WS_PROVENANCE
    role: DataRole = DataRole.CONTRACT_MARKET_QUOTE

    def __post_init__(self) -> None:
        _validate_envelope(self)
        if self.source_timestamp is not None:
            _aware(self.source_timestamp, "source timestamp")


@dataclass(frozen=True, slots=True)
class KalshiOrderBookDelta:
    connection_id: str
    subscription_id: int
    sequence: int
    ticker: str
    market_id: str
    side: KalshiBookSide
    price: Decimal
    quantity_delta: Decimal
    source_timestamp: datetime | None
    socket_received_timestamp: datetime
    parse_timestamp: datetime
    socket_received_monotonic_ns: int | None = None
    enqueue_timestamp: datetime | None = None
    enqueue_monotonic_ns: int | None = None
    provenance: str = KALSHI_WS_PROVENANCE
    role: DataRole = DataRole.CONTRACT_MARKET_QUOTE

    def __post_init__(self) -> None:
        _validate_envelope(self)
        if self.source_timestamp is not None:
            _aware(self.source_timestamp, "source timestamp")
        if not self.price.is_finite() or not Decimal(0) <= self.price <= Decimal(1):
            raise ValueError("WebSocket orderbook price must be finite and within [0, 1]")
        if not self.quantity_delta.is_finite():
            raise ValueError("WebSocket quantity delta must be finite")


@dataclass(frozen=True, slots=True)
class KalshiTickerUpdate:
    connection_id: str
    subscription_id: int
    ticker: str
    market_id: str
    last_trade: Decimal
    yes_bid: Decimal
    yes_ask: Decimal
    volume: Decimal
    source_timestamp: datetime
    socket_received_timestamp: datetime
    parse_timestamp: datetime
    socket_received_monotonic_ns: int | None = None
    enqueue_timestamp: datetime | None = None
    enqueue_monotonic_ns: int | None = None
    provenance: str = KALSHI_WS_PROVENANCE
    role: DataRole = DataRole.CONTRACT_MARKET_QUOTE

    def __post_init__(self) -> None:
        if not self.connection_id or _TICKER.fullmatch(self.ticker) is None or not self.market_id:
            raise ValueError("Kalshi ticker identities are invalid")
        if self.subscription_id < 1:
            raise ValueError("Kalshi ticker subscription must be positive")
        for timestamp in (
            self.source_timestamp,
            self.socket_received_timestamp,
            self.parse_timestamp,
        ):
            _aware(timestamp, "ticker timestamp")
        prices = (self.last_trade, self.yes_bid, self.yes_ask)
        if any(not value.is_finite() or not Decimal(0) <= value <= Decimal(1) for value in prices):
            raise ValueError("Kalshi ticker prices must be finite and within [0, 1]")
        if self.yes_ask < self.yes_bid or self.volume < 0:
            raise ValueError("Kalshi ticker book or volume is invalid")
        _monotonic_order(self.socket_received_monotonic_ns, self.enqueue_monotonic_ns)
        if self.enqueue_timestamp is not None:
            _aware(self.enqueue_timestamp, "ticker enqueue timestamp")


@dataclass(frozen=True, slots=True)
class KalshiSubscribed:
    request_id: int
    subscription_id: int
    channel: str


@dataclass(frozen=True, slots=True)
class KalshiCommandAcknowledged:
    connection_id: str
    request_id: int
    subscription_id: int | None
    sequence: int | None
    market_tickers: tuple[str, ...]
    socket_received_timestamp: datetime
    parse_timestamp: datetime
    socket_received_monotonic_ns: int | None = None
    enqueue_timestamp: datetime | None = None
    enqueue_monotonic_ns: int | None = None
    provenance: str = KALSHI_WS_PROVENANCE
    role: DataRole = DataRole.CONTRACT_MARKET_QUOTE

    def __post_init__(self) -> None:
        if not self.connection_id or self.request_id < 1:
            raise ValueError("Kalshi acknowledgement identity is invalid")
        if (self.subscription_id is None) != (self.sequence is None):
            raise ValueError("Kalshi acknowledgement sid and seq must appear together")
        if self.subscription_id is not None and (
            self.subscription_id < 1 or self.sequence is None or self.sequence < 1
        ):
            raise ValueError("Kalshi acknowledgement sequence identity is invalid")
        if any(_TICKER.fullmatch(ticker) is None for ticker in self.market_tickers):
            raise ValueError("Kalshi acknowledgement ticker is invalid")
        _aware(self.socket_received_timestamp, "acknowledgement receive timestamp")
        _aware(self.parse_timestamp, "acknowledgement parse timestamp")
        if self.parse_timestamp < self.socket_received_timestamp:
            raise ValueError("acknowledgement parse timestamp cannot precede receive timestamp")
        _monotonic_order(self.socket_received_monotonic_ns, self.enqueue_monotonic_ns)
        if self.enqueue_timestamp is not None:
            _aware(self.enqueue_timestamp, "acknowledgement enqueue timestamp")


@dataclass(frozen=True, slots=True)
class KalshiWsErrorMessage:
    request_id: int | None
    code: int
    message: str
    ticker: str | None


type KalshiOrderBookMessage = KalshiOrderBookSnapshot | KalshiOrderBookDelta
type KalshiServerMessage = (
    KalshiOrderBookMessage
    | KalshiTickerUpdate
    | KalshiSubscribed
    | KalshiCommandAcknowledged
    | KalshiWsErrorMessage
)


def _validate_envelope(message: KalshiOrderBookMessage) -> None:
    if not message.connection_id or not message.provenance:
        raise ValueError("WebSocket connection identity and provenance are required")
    if message.subscription_id < 1 or message.sequence < 1:
        raise ValueError("WebSocket subscription and sequence must be positive")
    if _TICKER.fullmatch(message.ticker) is None or not message.market_id:
        raise ValueError("WebSocket market identifiers are invalid")
    _aware(message.socket_received_timestamp, "socket receive timestamp")
    _aware(message.parse_timestamp, "parse timestamp")
    if message.parse_timestamp < message.socket_received_timestamp:
        raise ValueError("parse timestamp cannot precede socket receive timestamp")
    _monotonic_order(message.socket_received_monotonic_ns, message.enqueue_monotonic_ns)
    if message.enqueue_timestamp is not None:
        _aware(message.enqueue_timestamp, "enqueue timestamp")


def parse_kalshi_server_message(
    payload: str | bytes | Mapping[str, object],
    *,
    connection_id: str,
    socket_received_timestamp: datetime,
    parse_timestamp: datetime,
    socket_received_monotonic_ns: int | None = None,
    enqueue_timestamp: datetime | None = None,
    enqueue_monotonic_ns: int | None = None,
) -> KalshiServerMessage:
    """Parse only documented server messages without floating-point conversion."""

    if isinstance(payload, (str, bytes)):
        try:
            raw = json.loads(payload, parse_float=Decimal, parse_int=Decimal)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            raise KalshiWsPayloadError("malformed Kalshi WebSocket JSON") from None
    else:
        raw = payload
    if not isinstance(raw, Mapping):
        raise KalshiWsPayloadError("Kalshi WebSocket message must be an object")
    kind = raw.get("type")
    request_id = raw.get("id")
    sid = raw.get("sid")
    sequence = raw.get("seq")
    msg = raw.get("msg")
    if kind == "subscribed":
        if not isinstance(msg, Mapping):
            raise KalshiWsPayloadError("malformed Kalshi subscribed message")
        return KalshiSubscribed(
            request_id=_integer(request_id, "id", minimum=1),
            subscription_id=_integer(msg.get("sid"), "sid", minimum=1),
            channel=_identity(msg.get("channel"), "channel"),
        )
    if kind == "ok":
        tickers: tuple[str, ...] = ()
        if isinstance(msg, Mapping) and msg.get("market_tickers") is not None:
            values = msg["market_tickers"]
            if not isinstance(values, list):
                raise KalshiWsPayloadError("malformed Kalshi update acknowledgement")
            tickers = tuple(_ticker(value) for value in values)
        return KalshiCommandAcknowledged(
            connection_id=connection_id,
            request_id=_integer(request_id, "id", minimum=1),
            subscription_id=_integer(sid, "sid", minimum=1) if sid is not None else None,
            sequence=_integer(sequence, "seq", minimum=1) if sequence is not None else None,
            market_tickers=tickers,
            socket_received_timestamp=socket_received_timestamp,
            parse_timestamp=parse_timestamp,
            socket_received_monotonic_ns=socket_received_monotonic_ns,
            enqueue_timestamp=enqueue_timestamp,
            enqueue_monotonic_ns=enqueue_monotonic_ns,
        )
    if kind == "error":
        if not isinstance(msg, Mapping):
            raise KalshiWsPayloadError("malformed Kalshi error message")
        return KalshiWsErrorMessage(
            request_id=_integer(request_id, "id", minimum=1) if request_id is not None else None,
            code=_integer(msg.get("code"), "error code", minimum=1),
            message=_identity(msg.get("msg"), "error message"),
            ticker=_ticker(msg.get("market_ticker")) if msg.get("market_ticker") else None,
        )
    if not isinstance(msg, Mapping):
        raise KalshiWsPayloadError("malformed Kalshi WebSocket data message")
    subscription_id = _integer(sid, "sid", minimum=1)
    ticker = _ticker(msg.get("market_ticker"))
    market_id = _identity(msg.get("market_id"), "market_id")
    if kind == KalshiWsEventKind.SNAPSHOT:
        return KalshiOrderBookSnapshot(
            connection_id=connection_id,
            subscription_id=subscription_id,
            sequence=_integer(sequence, "seq", minimum=1),
            ticker=ticker,
            market_id=market_id,
            yes_bids=_levels(msg.get("yes_dollars_fp"), "yes_dollars_fp"),
            no_bids=_levels(msg.get("no_dollars_fp"), "no_dollars_fp"),
            source_timestamp=_source_timestamp(msg),
            socket_received_timestamp=socket_received_timestamp,
            parse_timestamp=parse_timestamp,
            socket_received_monotonic_ns=socket_received_monotonic_ns,
            enqueue_timestamp=enqueue_timestamp,
            enqueue_monotonic_ns=enqueue_monotonic_ns,
        )
    if kind == KalshiWsEventKind.DELTA:
        try:
            side = KalshiBookSide(msg.get("side"))
        except (TypeError, ValueError):
            raise KalshiWsPayloadError("malformed Kalshi WebSocket side") from None
        return KalshiOrderBookDelta(
            connection_id=connection_id,
            subscription_id=subscription_id,
            sequence=_integer(sequence, "seq", minimum=1),
            ticker=ticker,
            market_id=market_id,
            side=side,
            price=_decimal(msg.get("price_dollars"), "price_dollars"),
            quantity_delta=_decimal(msg.get("delta_fp"), "delta_fp"),
            source_timestamp=_source_timestamp(msg),
            socket_received_timestamp=socket_received_timestamp,
            parse_timestamp=parse_timestamp,
            socket_received_monotonic_ns=socket_received_monotonic_ns,
            enqueue_timestamp=enqueue_timestamp,
            enqueue_monotonic_ns=enqueue_monotonic_ns,
        )
    if kind == "ticker":
        source = _source_timestamp(msg)
        if source is None:
            raise KalshiWsPayloadError("Kalshi ticker source timestamp is missing")
        return KalshiTickerUpdate(
            connection_id=connection_id,
            subscription_id=subscription_id,
            ticker=ticker,
            market_id=market_id,
            last_trade=_decimal(msg.get("price_dollars"), "price_dollars"),
            yes_bid=_decimal(msg.get("yes_bid_dollars"), "yes_bid_dollars"),
            yes_ask=_decimal(msg.get("yes_ask_dollars"), "yes_ask_dollars"),
            volume=_decimal(msg.get("volume_fp"), "volume_fp"),
            source_timestamp=source,
            socket_received_timestamp=socket_received_timestamp,
            parse_timestamp=parse_timestamp,
            socket_received_monotonic_ns=socket_received_monotonic_ns,
            enqueue_timestamp=enqueue_timestamp,
            enqueue_monotonic_ns=enqueue_monotonic_ns,
        )
    raise KalshiWsPayloadError("unsupported Kalshi WebSocket message type")


@dataclass(frozen=True, slots=True)
class SynchronizedKalshiOrderBook:
    connection_id: str
    subscription_id: int
    sequence: int
    ticker: str
    market_id: str
    yes_bids: tuple[OrderBookLevel, ...]
    no_bids: tuple[OrderBookLevel, ...]
    source_timestamp: datetime | None
    received_timestamp: datetime
    status: KalshiBookSyncStatus = KalshiBookSyncStatus.SYNCHRONIZED
    provenance: str = KALSHI_WS_PROVENANCE


@dataclass(slots=True)
class _MutableBook:
    market_id: str
    yes: dict[Decimal, Decimal]
    no: dict[Decimal, Decimal]
    status: KalshiBookSyncStatus
    source_timestamp: datetime | None
    received_timestamp: datetime


class KalshiAtomicOrderBookCoordinator:
    """Reconstruct books while enforcing one contiguous sequence per subscription."""

    def __init__(self, connection_id: str, subscribed_tickers: Sequence[str]) -> None:
        if not connection_id:
            raise ValueError("connection_id is required")
        tickers = tuple(dict.fromkeys(subscribed_tickers))
        if not tickers or any(_TICKER.fullmatch(item) is None for item in tickers):
            raise ValueError("at least one exact Kalshi ticker is required")
        self.connection_id = connection_id
        self._subscribed = set(tickers)
        self._books: dict[str, _MutableBook] = {}
        self._subscription_id: int | None = None
        self._last_sequence: int | None = None
        self._resync_pending: set[str] = set()
        self._awaiting_resync_baseline = False

    @property
    def subscription_id(self) -> int | None:
        return self._subscription_id

    @property
    def subscribed_tickers(self) -> tuple[str, ...]:
        return tuple(sorted(self._subscribed))

    @property
    def synchronized_tickers(self) -> tuple[str, ...]:
        if self._resync_pending:
            return ()
        return tuple(
            sorted(
                ticker
                for ticker, book in self._books.items()
                if book.status is KalshiBookSyncStatus.SYNCHRONIZED
            )
        )

    def add_expected_ticker(self, ticker: str) -> None:
        if _TICKER.fullmatch(ticker) is None:
            raise ValueError("invalid Kalshi ticker")
        self._subscribed.add(ticker)

    def remove_expected_ticker(self, ticker: str) -> None:
        self._subscribed.discard(ticker)
        self._books.pop(ticker, None)

    def _invalidate_all(self) -> tuple[str, ...]:
        for book in self._books.values():
            book.status = KalshiBookSyncStatus.UNSYNCHRONIZED
        self._resync_pending = set(self._subscribed)
        self._awaiting_resync_baseline = True
        return self.subscribed_tickers

    def _sequence(
        self,
        *,
        connection_id: str,
        subscription_id: int,
        sequence: int,
        snapshot: bool = False,
    ) -> None:
        if connection_id != self.connection_id:
            raise KalshiBookInvariantError("WebSocket connection identity mismatch")
        if self._subscription_id is None:
            self._subscription_id = subscription_id
        elif subscription_id != self._subscription_id:
            raise KalshiBookInvariantError("WebSocket subscription identity mismatch")
        if snapshot and self._awaiting_resync_baseline:
            self._last_sequence = None
            self._awaiting_resync_baseline = False
        if self._last_sequence is None:
            if not snapshot:
                raise KalshiSequenceGapError(
                    "sequenced message arrived before a snapshot", tickers=self._invalidate_all()
                )
            self._last_sequence = sequence
            return
        expected = self._last_sequence + 1
        if sequence != expected:
            relation = (
                "duplicate"
                if sequence == self._last_sequence
                else "backward"
                if sequence < self._last_sequence
                else "gap"
            )
            raise KalshiSequenceGapError(
                f"orderbook sequence {relation}: expected {expected}, got {sequence}",
                tickers=self._invalidate_all(),
            )
        self._last_sequence = sequence

    def accept(self, message: KalshiOrderBookMessage) -> SynchronizedKalshiOrderBook | None:
        if message.ticker not in self._subscribed:
            raise KalshiBookInvariantError("WebSocket message ticker is not subscribed")
        existing = self._books.get(message.ticker)
        if existing is not None and existing.market_id != message.market_id:
            raise KalshiBookInvariantError("WebSocket ticker/market identity conflict")
        self._sequence(
            connection_id=message.connection_id,
            subscription_id=message.subscription_id,
            sequence=message.sequence,
            snapshot=isinstance(message, KalshiOrderBookSnapshot),
        )
        if isinstance(message, KalshiOrderBookSnapshot):
            self._books[message.ticker] = _MutableBook(
                market_id=message.market_id,
                yes={level.price: level.quantity for level in message.yes_bids},
                no={level.price: level.quantity for level in message.no_bids},
                status=KalshiBookSyncStatus.SYNCHRONIZED,
                source_timestamp=message.source_timestamp,
                received_timestamp=message.socket_received_timestamp,
            )
            self._resync_pending.discard(message.ticker)
            if self._resync_pending:
                return None
            return self.book(message.ticker)
        book = self._books.get(message.ticker)
        if book is None or book.status is not KalshiBookSyncStatus.SYNCHRONIZED:
            raise KalshiSequenceGapError(
                "orderbook delta arrived for an unsynchronized market",
                tickers=self._invalidate_all(),
            )
        levels = book.yes if message.side is KalshiBookSide.YES else book.no
        next_quantity = levels.get(message.price, Decimal(0)) + message.quantity_delta
        if next_quantity < 0:
            self._invalidate_all()
            raise KalshiBookInvariantError("orderbook delta would create negative depth")
        if next_quantity == 0:
            levels.pop(message.price, None)
        else:
            levels[message.price] = next_quantity
        book.source_timestamp = message.source_timestamp
        book.received_timestamp = message.socket_received_timestamp
        return self.book(message.ticker)

    def accept_ack(self, message: KalshiCommandAcknowledged) -> None:
        """Advance a sequenced subscription ack so replay does not invent a data gap."""

        if message.subscription_id is None or message.sequence is None:
            return
        self._sequence(
            connection_id=message.connection_id,
            subscription_id=message.subscription_id,
            sequence=message.sequence,
        )

    def book(self, ticker: str) -> SynchronizedKalshiOrderBook:
        book = self._books.get(ticker)
        if (
            book is None
            or book.status is not KalshiBookSyncStatus.SYNCHRONIZED
            or self._subscription_id is None
            or self._last_sequence is None
            or self._resync_pending
        ):
            raise KalshiUnsynchronizedBookError(f"Kalshi WS book is not synchronized: {ticker}")
        return SynchronizedKalshiOrderBook(
            connection_id=self.connection_id,
            subscription_id=self._subscription_id,
            sequence=self._last_sequence,
            ticker=ticker,
            market_id=book.market_id,
            yes_bids=tuple(
                OrderBookLevel(price, quantity)
                for price, quantity in sorted(book.yes.items(), reverse=True)
            ),
            no_bids=tuple(
                OrderBookLevel(price, quantity)
                for price, quantity in sorted(book.no.items(), reverse=True)
            ),
            source_timestamp=book.source_timestamp,
            received_timestamp=book.received_timestamp,
        )

    def reset(self) -> None:
        """Disconnect invalidates every book; reconnect must begin with snapshots."""

        self._invalidate_all()
        self._subscription_id = None
        self._last_sequence = None
        self._resync_pending.clear()
        self._awaiting_resync_baseline = False


class SynchronizedKalshiBookProvider(Protocol):
    """Future paper/live-feature boundary; unsynchronized books must raise."""

    def book(self, ticker: str) -> SynchronizedKalshiOrderBook: ...


class KalshiReadOnlyOrderBookStream(Protocol):
    """Authenticated market-data stream with no order, account, or trading method."""

    def messages(self, tickers: Sequence[str]) -> AsyncIterator[KalshiServerMessage]: ...


@dataclass(slots=True)
class KalshiResyncDiagnostics:
    requests: int = 0
    completed: int = 0
    last_duration_seconds: float | None = None


class KalshiAtomicSessionProcessor:
    """Apply stream messages and issue one documented get_snapshot on sequence loss."""

    def __init__(
        self,
        coordinator: KalshiAtomicOrderBookCoordinator,
        sender: Callable[[str], Awaitable[None]],
        *,
        first_request_id: int = 1000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if first_request_id < 1:
            raise ValueError("first_request_id must be positive")
        self._coordinator = coordinator
        self._sender = sender
        self._next_request_id = first_request_id
        self._monotonic = monotonic
        self._pending: set[str] = set()
        self._resync_started: float | None = None
        self.diagnostics = KalshiResyncDiagnostics()

    async def process(
        self, message: KalshiOrderBookMessage | KalshiCommandAcknowledged
    ) -> SynchronizedKalshiOrderBook | None:
        try:
            if isinstance(message, KalshiCommandAcknowledged):
                self._coordinator.accept_ack(message)
                return None
            book = self._coordinator.accept(message)
        except KalshiSequenceGapError as error:
            new_tickers = tuple(ticker for ticker in error.tickers if ticker not in self._pending)
            if new_tickers:
                subscription_id = self._coordinator.subscription_id or message.subscription_id
                command = update_subscription_command(
                    self._next_request_id,
                    subscription_id,
                    "get_snapshot",
                    new_tickers,
                )
                self._next_request_id += 1
                await self._sender(command.payload)
                self._pending.update(new_tickers)
                if self._resync_started is None:
                    self._resync_started = self._monotonic()
                self.diagnostics.requests += 1
            return None
        if isinstance(message, KalshiOrderBookSnapshot) and message.ticker in self._pending:
            self._pending.remove(message.ticker)
            if not self._pending and self._resync_started is not None:
                self.diagnostics.completed += 1
                self.diagnostics.last_duration_seconds = self._monotonic() - self._resync_started
                self._resync_started = None
        return book


class PersistedKalshiWsEvent(Protocol):
    connection_id: str
    subscription_id: int
    sequence: int
    event_kind: KalshiWsEventKind
    ticker: str | None
    market_id: str | None
    market_tickers: tuple[str, ...]
    side: KalshiBookSide | None
    price: Decimal | None
    quantity_delta: Decimal | None
    yes_bids: tuple[OrderBookLevel, ...]
    no_bids: tuple[OrderBookLevel, ...]
    source_timestamp: datetime | None
    socket_received_timestamp: datetime
    parse_timestamp: datetime


def replay_orderbook_events(
    records: Sequence[PersistedKalshiWsEvent], subscribed_tickers: Sequence[str]
) -> Mapping[str, SynchronizedKalshiOrderBook]:
    """Deterministically rebuild books from arrival-ordered persisted events."""

    if not records:
        return {}
    connection_id = records[0].connection_id
    initial_tickers = (
        records[0].market_tickers
        if records[0].event_kind is KalshiWsEventKind.SUBSCRIPTION_ACK and records[0].market_tickers
        else subscribed_tickers
    )
    coordinator = KalshiAtomicOrderBookCoordinator(connection_id, initial_tickers)
    for record in records:
        if record.connection_id != connection_id:
            raise KalshiBookInvariantError("replay cannot cross WebSocket connections")
        if record.event_kind is KalshiWsEventKind.SUBSCRIPTION_ACK:
            try:
                acknowledged = set(record.market_tickers)
                current = set(coordinator.subscribed_tickers)
                for ticker in sorted(acknowledged - current):
                    coordinator.add_expected_ticker(ticker)
                for ticker in sorted(current - acknowledged):
                    coordinator.remove_expected_ticker(ticker)
                coordinator.accept_ack(
                    KalshiCommandAcknowledged(
                        connection_id=record.connection_id,
                        request_id=1,
                        subscription_id=record.subscription_id,
                        sequence=record.sequence,
                        market_tickers=record.market_tickers,
                        socket_received_timestamp=record.socket_received_timestamp,
                        parse_timestamp=record.parse_timestamp,
                    )
                )
            except KalshiSequenceGapError:
                pass
            continue
        if record.ticker is None or record.market_id is None:
            raise KalshiBookInvariantError("persisted orderbook identity is incomplete")
        if record.event_kind is KalshiWsEventKind.SNAPSHOT:
            message: KalshiOrderBookMessage = KalshiOrderBookSnapshot(
                connection_id=record.connection_id,
                subscription_id=record.subscription_id,
                sequence=record.sequence,
                ticker=record.ticker,
                market_id=record.market_id,
                yes_bids=record.yes_bids,
                no_bids=record.no_bids,
                source_timestamp=record.source_timestamp,
                socket_received_timestamp=record.socket_received_timestamp,
                parse_timestamp=record.parse_timestamp,
            )
        else:
            if record.side is None or record.price is None or record.quantity_delta is None:
                raise KalshiBookInvariantError("persisted delta fields are incomplete")
            message = KalshiOrderBookDelta(
                connection_id=record.connection_id,
                subscription_id=record.subscription_id,
                sequence=record.sequence,
                ticker=record.ticker,
                market_id=record.market_id,
                side=record.side,
                price=record.price,
                quantity_delta=record.quantity_delta,
                source_timestamp=record.source_timestamp,
                socket_received_timestamp=record.socket_received_timestamp,
                parse_timestamp=record.parse_timestamp,
            )
        try:
            coordinator.accept(message)
        except KalshiSequenceGapError:
            continue
    result: dict[str, SynchronizedKalshiOrderBook] = {}
    for ticker in subscribed_tickers:
        try:
            result[ticker] = coordinator.book(ticker)
        except KalshiUnsynchronizedBookError:
            continue
    return result


@dataclass(frozen=True, slots=True)
class KalshiSubscriptionCommand:
    request_id: int
    payload: str

    def as_object(self) -> Mapping[str, object]:
        value = json.loads(self.payload)
        assert isinstance(value, Mapping)
        return value


def subscribe_command(request_id: int, tickers: Sequence[str]) -> KalshiSubscriptionCommand:
    exact = tuple(dict.fromkeys(tickers))
    if request_id < 1 or not exact or any(_TICKER.fullmatch(item) is None for item in exact):
        raise ValueError("invalid Kalshi subscription command")
    return KalshiSubscriptionCommand(
        request_id,
        json.dumps(
            {
                "id": request_id,
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta", "ticker"],
                    "market_tickers": list(exact),
                    "use_yes_price": False,
                },
            },
            separators=(",", ":"),
        ),
    )


def update_subscription_command(
    request_id: int,
    subscription_id: int,
    action: str,
    tickers: Sequence[str],
) -> KalshiSubscriptionCommand:
    exact = tuple(dict.fromkeys(tickers))
    if (
        request_id < 1
        or subscription_id < 1
        or action not in {"add_markets", "delete_markets", "get_snapshot"}
        or not exact
        or any(_TICKER.fullmatch(item) is None for item in exact)
    ):
        raise ValueError("invalid Kalshi update_subscription command")
    return KalshiSubscriptionCommand(
        request_id,
        json.dumps(
            {
                "id": request_id,
                "cmd": "update_subscription",
                "params": {
                    "sid": subscription_id,
                    "market_tickers": list(exact),
                    "action": action,
                },
            },
            separators=(",", ":"),
        ),
    )


@dataclass(slots=True)
class KalshiRolloverPlan:
    predecessor: str
    successor: str
    successor_snapshot_synchronized: bool = False
    predecessor_removed: bool = False


class KalshiSubscriptionRollover:
    """Add successor first; remove predecessor only after its synchronized snapshot."""

    def __init__(self, coordinator: KalshiAtomicOrderBookCoordinator) -> None:
        self._coordinator = coordinator
        self._plans: dict[str, KalshiRolloverPlan] = {}

    def add_successor(
        self,
        *,
        request_id: int,
        subscription_id: int,
        predecessor: str,
        successor: str,
    ) -> KalshiSubscriptionCommand:
        if predecessor not in self._coordinator.subscribed_tickers or predecessor == successor:
            raise ValueError("rollover predecessor/successor identity is invalid")
        if successor in self._plans:
            raise ValueError("rollover successor is already pending")
        self._coordinator.add_expected_ticker(successor)
        self._plans[successor] = KalshiRolloverPlan(predecessor, successor)
        return update_subscription_command(request_id, subscription_id, "add_markets", (successor,))

    def successor_synchronized(
        self, *, request_id: int, subscription_id: int, successor: str
    ) -> KalshiSubscriptionCommand:
        plan = self._plans.get(successor)
        if plan is None:
            raise ValueError("rollover successor is not pending")
        self._coordinator.book(successor)
        plan.successor_snapshot_synchronized = True
        return update_subscription_command(
            request_id, subscription_id, "delete_markets", (plan.predecessor,)
        )

    def predecessor_removed(self, successor: str) -> None:
        plan = self._plans.get(successor)
        if plan is None or not plan.successor_snapshot_synchronized:
            raise ValueError("cannot remove predecessor before successor synchronization")
        self._coordinator.remove_expected_ticker(plan.predecessor)
        plan.predecessor_removed = True
