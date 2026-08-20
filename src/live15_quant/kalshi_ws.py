"""Typed boundary for a future authenticated, read-only Kalshi WebSocket adapter.

This module opens no socket and handles no credential. Production WebSocket authentication is a
future operator-approved integration; these types define the snapshot/delta and sequence contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from live15_quant.models import OrderBookLevel


class KalshiBookSide(StrEnum):
    YES = "yes"
    NO = "no"


@dataclass(frozen=True, slots=True)
class KalshiOrderBookSnapshot:
    subscription_id: int
    sequence: int
    ticker: str
    market_id: str
    yes_bids: tuple[OrderBookLevel, ...]
    no_bids: tuple[OrderBookLevel, ...]
    received_timestamp: datetime

    def __post_init__(self) -> None:
        _validate_envelope(
            self.subscription_id,
            self.sequence,
            self.ticker,
            self.market_id,
            self.received_timestamp,
        )


@dataclass(frozen=True, slots=True)
class KalshiOrderBookDelta:
    subscription_id: int
    sequence: int
    ticker: str
    market_id: str
    side: KalshiBookSide
    price: Decimal
    quantity_delta: Decimal
    source_timestamp: datetime
    received_timestamp: datetime

    def __post_init__(self) -> None:
        _validate_envelope(
            self.subscription_id,
            self.sequence,
            self.ticker,
            self.market_id,
            self.received_timestamp,
        )
        if self.source_timestamp.tzinfo is None or self.source_timestamp.utcoffset() is None:
            raise ValueError("WebSocket source timestamp must be timezone-aware")
        if not self.price.is_finite() or not Decimal(0) <= self.price <= Decimal(1):
            raise ValueError("WebSocket orderbook price must be finite and within [0, 1]")
        if not self.quantity_delta.is_finite():
            raise ValueError("WebSocket quantity delta must be finite")


def _validate_envelope(
    subscription_id: int,
    sequence: int,
    ticker: str,
    market_id: str,
    received_timestamp: datetime,
) -> None:
    if subscription_id < 0 or sequence < 0:
        raise ValueError("WebSocket subscription and sequence must be non-negative")
    if not ticker or not market_id:
        raise ValueError("WebSocket market identifiers must not be empty")
    if received_timestamp.tzinfo is None or received_timestamp.utcoffset() is None:
        raise ValueError("WebSocket receive timestamp must be timezone-aware")


type KalshiOrderBookMessage = KalshiOrderBookSnapshot | KalshiOrderBookDelta


class KalshiReadOnlyOrderBookStream(Protocol):
    """Future adapter boundary; implementations may emit data but expose no order method."""

    def messages(self, tickers: Sequence[str]) -> AsyncIterator[KalshiOrderBookMessage]: ...


class KalshiSequenceGapError(RuntimeError):
    """Raised when an incremental book can no longer be trusted."""


class KalshiOrderBookSequenceGuard:
    """Require snapshot-first and contiguous sequence per official subscription ID."""

    def __init__(self) -> None:
        self._last: dict[int, tuple[int, str, str]] = {}

    def accept(self, message: KalshiOrderBookMessage) -> None:
        if isinstance(message, KalshiOrderBookSnapshot):
            self._last[message.subscription_id] = (
                message.sequence,
                message.ticker,
                message.market_id,
            )
            return
        previous = self._last.get(message.subscription_id)
        if previous is None:
            raise KalshiSequenceGapError("orderbook delta arrived before a snapshot")
        previous_sequence, ticker, market_id = previous
        if message.ticker != ticker or message.market_id != market_id:
            raise KalshiSequenceGapError("orderbook delta instrument does not match snapshot")
        if message.sequence != previous_sequence + 1:
            raise KalshiSequenceGapError(
                f"orderbook sequence gap: expected {previous_sequence + 1}, got {message.sequence}"
            )
        self._last[message.subscription_id] = (
            message.sequence,
            message.ticker,
            message.market_id,
        )

    def reset(self) -> None:
        """Invalidate all books on disconnect; reconnect must begin with fresh snapshots."""

        self._last.clear()
