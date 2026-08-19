"""Typed domain models shared by market-data providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class MarketTick:
    """Normalized top-of-book market observation."""

    symbol: str
    price: Decimal
    bid: Decimal
    ask: Decimal
    received_at: datetime
    exchange_time: datetime | None = None

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid
