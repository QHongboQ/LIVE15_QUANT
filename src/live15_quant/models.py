"""Typed domain models shared by market-data providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class Asset(StrEnum):
    """The ten approved Kalshi-native 15-minute research assets."""

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
    SETTLEMENT_TRUTH = "official_settlement_truth"
    PAPER_EXECUTION = "paper_execution"


class UnderlyingProvider(StrEnum):
    """Predictive sources remain independently identifiable at rest."""

    COINBASE = "coinbase"
    PYTH_HERMES = "pyth_hermes"
    BINANCE_SPOT = "binance_spot"
    HYPERLIQUID_PERP = "hyperliquid_perp"


class SecondaryPriceSemantics(StrEnum):
    """Provider-native meaning of a secondary predictive price."""

    AGGREGATE_TRADE = "aggregate_trade"
    BBO_MIDPOINT = "bbo_midpoint"


class Venue(StrEnum):
    """Regulated venues named by Robinhood for event contracts."""

    KALSHI = "KalshiEX LLC"
    FORECASTEX = "ForecastEX, LLC"
    ROTHERA = "Rothera Exchange and Clearing LLC"


class MappingConfidence(StrEnum):
    """Strength of a Robinhood-contract to venue-instrument mapping."""

    VERIFIED = "verified"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class ExecutabilityClassification(StrEnum):
    """What a quote can, and deliberately cannot, claim to represent."""

    OFFICIAL_VENUE_ORDER_BOOK = "official_venue_order_book"
    INDICATIVE_ONLY = "indicative_only"
    UNSUPPORTED = "unsupported"


class SourceTimestampKind(StrEnum):
    """Semantics of a provider-supplied timestamp."""

    HTTP_RESPONSE_DATE = "http_response_date"
    EXCHANGE_EVENT_TIME = "exchange_event_time"
    UNAVAILABLE = "unavailable"


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


class RecorderDiagnosticKind(StrEnum):
    """Recorder observations that must never enter the training snapshot stream."""

    POST_END_EVENT_RETURNED = "post_end_event_returned"
    ROLLOVER_GAP_STARTED = "rollover_gap_started"
    ROLLOVER_GAP_ENDED = "rollover_gap_ended"


class RecorderEventSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


class RecorderEventType(StrEnum):
    RECORDER_STARTED = "recorder_started"
    RECORDER_STOPPED = "recorder_stopped"
    RECORDER_RECOVERED = "recorder_recovered"
    SOURCE_UNAVAILABLE = "source_temporarily_unavailable"
    RETRY_EXHAUSTED = "retry_exhausted"
    SOURCE_STALE = "source_stale"
    LIFECYCLE_REGRESSION = "lifecycle_regression"
    SETTLEMENT_CONFLICT = "settlement_conflict"
    MAPPING_CONFLICT = "mapping_ticker_conflict"
    SQLITE_FAILURE = "sqlite_integrity_failure"
    FATAL_TASK = "fatal_task"
    WS_PROTOCOL_NOTICE = "ws_protocol_notice"
    WS_PAYLOAD_RECOVERY = "ws_payload_recovery"


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    """One explicit venue order-book bid level."""

    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.price <= Decimal(1):
            raise ValueError("order-book price must be within [0, 1]")
        if self.quantity < 0:
            raise ValueError("order-book quantity must be non-negative")


@dataclass(frozen=True, slots=True)
class VenueMapping:
    """Auditable mapping from Robinhood identifiers to one venue instrument."""

    asset: Asset
    robinhood_event_id: str
    robinhood_contract_id: str
    venue: Venue | None
    venue_series: str | None
    venue_ticker: str | None
    confidence: MappingConfidence
    matched_fields: tuple[str, ...]
    evidence_urls: tuple[str, ...]
    notes: str

    def __post_init__(self) -> None:
        if not self.robinhood_event_id or not self.robinhood_contract_id:
            raise ValueError("Robinhood mapping identifiers must not be empty")
        if self.confidence is MappingConfidence.VERIFIED and (
            self.venue is None or self.venue_series is None or self.venue_ticker is None
        ):
            raise ValueError("verified mappings require a venue, series, and ticker")


@dataclass(frozen=True, slots=True)
class PredictionMarketQuote:
    """Official venue quote kept separate from Robinhood SSR and predictive ticks."""

    asset: Asset
    robinhood_event_id: str
    robinhood_contract_id: str
    venue: Venue
    venue_series: str
    venue_ticker: str
    mapping_confidence: MappingConfidence
    source_timestamp: datetime | None
    source_timestamp_kind: SourceTimestampKind
    received_timestamp: datetime
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    last_trade: Decimal | None
    volume: Decimal | None
    yes_bid_depth: tuple[OrderBookLevel, ...]
    no_bid_depth: tuple[OrderBookLevel, ...]
    source: str
    freshness: FreshnessState
    executability: ExecutabilityClassification
    evidence_urls: tuple[str, ...]
    role: DataRole = field(init=False, default=DataRole.CONTRACT_MARKET_QUOTE)

    def __post_init__(self) -> None:
        if not all(
            (
                self.robinhood_event_id,
                self.robinhood_contract_id,
                self.venue_series,
                self.venue_ticker,
                self.source,
            )
        ):
            raise ValueError("prediction quote identifiers and source must not be empty")
        if self.mapping_confidence is not MappingConfidence.VERIFIED:
            raise ValueError("prediction quotes require a verified venue mapping")
        timestamps = (self.source_timestamp, self.received_timestamp)
        if any(
            value is not None and (value.tzinfo is None or value.utcoffset() is None)
            for value in timestamps
        ):
            raise ValueError("prediction quote timestamps must be timezone-aware")
        prices = (self.yes_bid, self.yes_ask, self.no_bid, self.no_ask, self.last_trade)
        if any(value is not None and not Decimal(0) <= value <= Decimal(1) for value in prices):
            raise ValueError("prediction quote prices must be within [0, 1]")
        if self.yes_bid is not None and self.yes_ask is not None and self.yes_ask < self.yes_bid:
            raise ValueError("Yes ask must not be below Yes bid")
        if self.no_bid is not None and self.no_ask is not None and self.no_ask < self.no_bid:
            raise ValueError("No ask must not be below No bid")
        if self.volume is not None and self.volume < 0:
            raise ValueError("prediction quote volume must be non-negative")
        if (
            self.source_timestamp is None
            and self.source_timestamp_kind is not SourceTimestampKind.UNAVAILABLE
        ):
            raise ValueError("missing source timestamp must be classified unavailable")
        if (
            self.source_timestamp is not None
            and self.source_timestamp_kind is SourceTimestampKind.UNAVAILABLE
        ):
            raise ValueError("available source timestamp requires explicit semantics")


@dataclass(frozen=True, slots=True)
class KalshiNativeQuote:
    """Official Kalshi quote identified only by native event/market tickers."""

    asset: Asset
    series: str
    ticker: str
    event_ticker: str
    source_timestamp: datetime | None
    source_timestamp_kind: SourceTimestampKind
    received_timestamp: datetime
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    last_trade: Decimal | None
    volume: Decimal | None
    yes_bid_depth: tuple[OrderBookLevel, ...]
    no_bid_depth: tuple[OrderBookLevel, ...]
    source: str
    freshness: FreshnessState
    executability: ExecutabilityClassification
    evidence_urls: tuple[str, ...]
    role: DataRole = field(init=False, default=DataRole.CONTRACT_MARKET_QUOTE)

    def __post_init__(self) -> None:
        if not all((self.series, self.ticker, self.event_ticker, self.source)):
            raise ValueError("Kalshi-native quote identifiers must not be empty")
        if not self.event_ticker.startswith(f"{self.series}-") or not self.ticker.startswith(
            f"{self.event_ticker}-"
        ):
            raise ValueError("Kalshi-native quote ticker hierarchy is inconsistent")
        timestamps = (self.source_timestamp, self.received_timestamp)
        if any(
            value is not None and (value.tzinfo is None or value.utcoffset() is None)
            for value in timestamps
        ):
            raise ValueError("Kalshi-native quote timestamps must be timezone-aware")
        prices = (self.yes_bid, self.yes_ask, self.no_bid, self.no_ask, self.last_trade)
        if any(
            value is not None and (not value.is_finite() or not Decimal(0) <= value <= Decimal(1))
            for value in prices
        ):
            raise ValueError("Kalshi-native quote prices must be finite and within [0, 1]")
        if self.yes_bid is not None and self.yes_ask is not None and self.yes_ask < self.yes_bid:
            raise ValueError("Yes ask must not be below Yes bid")
        if self.no_bid is not None and self.no_ask is not None and self.no_ask < self.no_bid:
            raise ValueError("No ask must not be below No bid")
        if self.volume is not None and (not self.volume.is_finite() or self.volume < 0):
            raise ValueError("Kalshi-native quote volume must be finite and non-negative")
        if (
            self.source_timestamp is None
            and self.source_timestamp_kind is not SourceTimestampKind.UNAVAILABLE
        ):
            raise ValueError("missing source timestamp must be classified unavailable")
        if (
            self.source_timestamp is not None
            and self.source_timestamp_kind is SourceTimestampKind.UNAVAILABLE
        ):
            raise ValueError("available source timestamp requires explicit semantics")


@dataclass(frozen=True, slots=True)
class MarketTick:
    """Normalized top-of-book market observation."""

    symbol: str
    price: Decimal
    bid: Decimal
    ask: Decimal
    received_at: datetime
    exchange_time: datetime | None = None
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    last_size: Decimal | None = None
    volume_24h: Decimal | None = None
    role: DataRole = field(init=False, default=DataRole.PREDICTIVE_MARKET_DATA)

    def __post_init__(self) -> None:
        timestamps = (self.received_at, self.exchange_time)
        if any(
            timestamp is not None and (timestamp.tzinfo is None or timestamp.utcoffset() is None)
            for timestamp in timestamps
        ):
            raise ValueError("market tick timestamps must be timezone-aware")
        if self.price <= 0 or self.bid < 0 or self.ask < self.bid:
            raise ValueError("market tick prices must form a valid non-negative book")
        sizes = (self.bid_size, self.ask_size, self.last_size, self.volume_24h)
        if any(value is not None and value < 0 for value in sizes):
            raise ValueError("market tick sizes must be non-negative")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class UnderlyingObservation:
    """Provider-neutral predictive input with explicit provenance and two clocks."""

    asset: Asset
    provider: UnderlyingProvider
    symbol: str
    feed_id: str
    price: Decimal
    source_timestamp: datetime
    received_timestamp: datetime
    confidence: Decimal | None
    provenance: str
    freshness: FreshnessState
    role: DataRole = field(init=False, default=DataRole.PREDICTIVE_MARKET_DATA)

    def __post_init__(self) -> None:
        if not self.symbol or not self.feed_id or not self.provenance:
            raise ValueError("underlying identifiers and provenance must not be empty")
        for value in (self.source_timestamp, self.received_timestamp):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("underlying timestamps must be timezone-aware")
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("underlying price must be finite and positive")
        if self.confidence is not None and (not self.confidence.is_finite() or self.confidence < 0):
            raise ValueError("underlying confidence must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class SecondaryUnderlyingObservation:
    """A secondary predictive input that can never replace primary data implicitly."""

    asset: Asset
    provider: UnderlyingProvider
    instrument: str
    price: Decimal
    price_semantics: SecondaryPriceSemantics
    source_timestamp: datetime
    received_timestamp: datetime
    provenance: str
    freshness: FreshnessState
    source_event_id: str
    bid: Decimal | None = None
    ask: Decimal | None = None
    role: DataRole = field(init=False, default=DataRole.PREDICTIVE_MARKET_DATA)

    def __post_init__(self) -> None:
        if not self.instrument or not self.provenance or not self.source_event_id:
            raise ValueError("secondary underlying identity and provenance must not be empty")
        for timestamp in (self.source_timestamp, self.received_timestamp):
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("secondary underlying timestamps must be timezone-aware")
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("secondary underlying price must be finite and positive")
        if (self.bid is None) != (self.ask is None):
            raise ValueError("secondary bid and ask must be present together")
        if self.provider is UnderlyingProvider.BINANCE_SPOT:
            if (
                self.asset is not Asset.BNB
                or self.instrument != "BNBUSDT"
                or self.price_semantics is not SecondaryPriceSemantics.AGGREGATE_TRADE
                or self.bid is not None
            ):
                raise ValueError("Binance secondary must be exact BNBUSDT aggregate trades")
        elif self.provider is UnderlyingProvider.HYPERLIQUID_PERP:
            if (
                self.asset is not Asset.HYPE
                or self.instrument != "HYPE"
                or self.price_semantics is not SecondaryPriceSemantics.BBO_MIDPOINT
                or self.bid is None
                or self.ask is None
                or self.bid <= 0
                or self.ask < self.bid
                or self.price != (self.bid + self.ask) / 2
            ):
                raise ValueError("Hyperliquid secondary must be exact HYPE BBO midpoint")
        else:
            raise ValueError("provider is not approved for secondary underlying data")


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
