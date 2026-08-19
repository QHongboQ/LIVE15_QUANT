"""Typed domain models shared by market-data providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class Asset(StrEnum):
    """Assets offered in Robinhood's Live 15-minute category."""

    BTC = "BTC"
    ETH = "ETH"
    GOLD = "Gold"
    SILVER = "Silver"
    XRP = "XRP"
    WTI_OIL = "WTI Oil"
    SOL = "SOL"
    HYPE = "HYPE"
    DOGE = "DOGE"
    BNB = "BNB"


class DataRole(StrEnum):
    """A source's role; these roles must never be treated as interchangeable."""

    PREDICTIVE_MARKET_DATA = "predictive_market_data"
    CONTRACT_MARKET_QUOTE = "contract_market_quote"
    SETTLEMENT_BENCHMARK = "settlement_benchmark"


class LifecycleState(StrEnum):
    """Normalized contract lifecycle."""

    UPCOMING = "upcoming"
    LIVE = "live"
    CLOSED = "closed"
    SETTLED = "settled"
    UNKNOWN = "unknown"


class FreshnessState(StrEnum):
    """Freshness of the public webpage snapshot."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class SupportLevel(StrEnum):
    """Implementation/data-access support level."""

    FULL = "full"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class MarketTick:
    """Normalized top-of-book market observation."""

    symbol: str
    price: Decimal
    bid: Decimal
    ask: Decimal
    received_at: datetime
    exchange_time: datetime | None = None
    role: DataRole = field(init=False, default=DataRole.PREDICTIVE_MARKET_DATA)

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class SettlementSpec:
    """Verified settlement metadata, separate from predictive input data."""

    asset: Asset
    benchmark: str
    method: str
    decimal_places: int | None
    source_url: str
    benchmark_data_url: str
    data_access: SupportLevel
    access_notes: str
    role: DataRole = field(init=False, default=DataRole.SETTLEMENT_BENCHMARK)

    def __post_init__(self) -> None:
        if self.decimal_places is not None and self.decimal_places < 0:
            raise ValueError("settlement decimal_places must be non-negative or None")


@dataclass(frozen=True, slots=True)
class ContractQuote:
    """Prices displayed by the public Robinhood page, not executable quotes."""

    yes_probability: Decimal | None
    no_probability: Decimal | None
    availability: SupportLevel
    is_executable: bool = field(init=False, default=False)
    role: DataRole = field(init=False, default=DataRole.CONTRACT_MARKET_QUOTE)

    def __post_init__(self) -> None:
        probabilities = (self.yes_probability, self.no_probability)
        if any(
            value is not None and not Decimal(0) <= value <= Decimal(1) for value in probabilities
        ):
            raise ValueError("contract probabilities must be within [0, 1]")
        available_count = sum(value is not None for value in probabilities)
        expected = (
            SupportLevel.UNSUPPORTED
            if available_count == 0
            else SupportLevel.PARTIAL
            if available_count == 1
            else SupportLevel.FULL
        )
        if self.availability is not expected:
            raise ValueError("quote availability does not match its displayed fields")


@dataclass(frozen=True, slots=True)
class FifteenMinuteContract:
    """Normalized public Robinhood Live 15-minute event snapshot."""

    asset: Asset
    event_id: str
    contract_id: str
    start_time: datetime
    end_time: datetime
    target_price: Decimal
    quote: ContractQuote
    venue: str | None
    venue_candidates: tuple[str, ...]
    settlement: SettlementSpec
    lifecycle_state: LifecycleState
    source_url: str
    fetched_at: datetime
    freshness_state: FreshnessState
    source_age_seconds: int | None

    def __post_init__(self) -> None:
        if (
            self.start_time.tzinfo is None
            or self.start_time.utcoffset() is None
            or self.end_time.tzinfo is None
            or self.end_time.utcoffset() is None
        ):
            raise ValueError("contract times must be timezone-aware")
        if self.end_time - self.start_time != timedelta(minutes=15):
            raise ValueError("contract window must be exactly 15 minutes")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware")
        if self.target_price <= 0:
            raise ValueError("target_price must be positive")
        if self.settlement.asset is not self.asset:
            raise ValueError("settlement asset must match contract asset")
        if self.source_age_seconds is not None and self.source_age_seconds < 0:
            raise ValueError("source_age_seconds must be non-negative or None")


@dataclass(frozen=True, slots=True)
class AssetSupport:
    """Capability-specific support; overall support cannot hide partial inputs."""

    asset: Asset
    discovery: SupportLevel
    predictive_input: SupportLevel
    displayed_quote: SupportLevel
    settlement_metadata: SupportLevel
    settlement_truth: SupportLevel
    overall: SupportLevel

    def __post_init__(self) -> None:
        dimensions = (
            self.discovery,
            self.predictive_input,
            self.displayed_quote,
            self.settlement_metadata,
            self.settlement_truth,
        )
        if self.overall is SupportLevel.FULL and any(
            level is not SupportLevel.FULL for level in dimensions
        ):
            raise ValueError("overall support cannot be full while a dimension is not full")
