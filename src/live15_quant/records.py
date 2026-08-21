"""Versioned recorder records independent from live provider objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from live15_quant.kalshi_lifecycle import KalshiLifecycle, KalshiResult
from live15_quant.models import (
    Asset,
    DataRole,
    ExecutabilityClassification,
    FreshnessState,
    LifecycleState,
    MappingConfidence,
    OrderBookLevel,
    RecorderDiagnosticKind,
    RecorderEventSeverity,
    RecorderEventType,
    SecondaryPriceSemantics,
    SourceTimestampKind,
    SupportLevel,
    UnderlyingProvider,
    Venue,
)

SCHEMA_VERSION = 7


@dataclass(frozen=True, slots=True)
class RobinhoodSnapshotRecord:
    """One unaggregated public Robinhood event observation."""

    row_id: int
    schema_version: int
    asset: Asset
    event_id: str
    contract_id: str
    start_time: datetime
    end_time: datetime
    fetched_timestamp: datetime
    seconds_remaining: int
    target_price: Decimal
    displayed_yes: Decimal | None
    displayed_no: Decimal | None
    quote_availability: SupportLevel
    lifecycle: LifecycleState
    freshness: FreshnessState
    venue: str | None
    settlement_benchmark: str
    settlement_method: str
    settlement_decimal_places: int | None
    settlement_source_url: str
    settlement_benchmark_data_url: str
    settlement_data_access: SupportLevel
    settlement_access_notes: str
    settlement_role: DataRole
    source_age_seconds: int | None
    venue_candidates: tuple[str, ...]
    source_url: str
    role: DataRole


@dataclass(frozen=True, slots=True)
class CoinbaseTickRecord:
    """One unaggregated Coinbase predictive-market observation."""

    row_id: int
    schema_version: int
    exchange_timestamp: datetime | None
    received_timestamp: datetime
    product: str
    price: Decimal
    bid: Decimal
    ask: Decimal
    spread: Decimal
    bid_size: Decimal | None
    ask_size: Decimal | None
    last_size: Decimal | None
    volume_24h: Decimal | None
    role: DataRole


@dataclass(frozen=True, slots=True)
class UnderlyingObservationRecord:
    """One deduplicated provider-specific predictive observation."""

    row_id: int
    schema_version: int
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
    role: DataRole


@dataclass(frozen=True, slots=True)
class SecondaryUnderlyingObservationRecord:
    """One immutable venue-native secondary predictive observation."""

    row_id: int
    schema_version: int
    asset: Asset
    provider: UnderlyingProvider
    instrument: str
    price: Decimal
    price_semantics: SecondaryPriceSemantics
    bid: Decimal | None
    ask: Decimal | None
    source_timestamp: datetime
    received_timestamp: datetime
    persisted_timestamp: datetime | None
    source_receive_latency_ms: Decimal
    receive_persist_latency_ms: Decimal | None
    provenance: str
    freshness: FreshnessState
    source_event_id: str
    role: DataRole


@dataclass(frozen=True, slots=True)
class RobinhoodDiagnosticRecord:
    """A non-training observation retained for recorder/upstream diagnosis."""

    row_id: int
    schema_version: int
    kind: RecorderDiagnosticKind
    asset: Asset
    event_id: str
    contract_id: str
    observed_timestamp: datetime
    event_end_time: datetime
    related_event_id: str | None
    source_url: str


@dataclass(frozen=True, slots=True)
class RecorderEventRecord:
    """Bounded operational diagnostic, isolated from all training tables."""

    row_id: int
    schema_version: int
    observed_timestamp: datetime
    severity: RecorderEventSeverity
    event_type: RecorderEventType
    asset: Asset | None
    source: str | None
    error_type: str | None
    message: str


@dataclass(frozen=True, slots=True)
class PredictionQuoteRecord:
    """One deduplicated official venue quote observation."""

    row_id: int
    schema_version: int
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
    role: DataRole


@dataclass(frozen=True, slots=True)
class KalshiMarketRecord:
    row_id: int
    schema_version: int
    asset: Asset
    series: str
    ticker: str
    event_ticker: str
    window_start: datetime
    window_end: datetime
    target: Decimal
    lifecycle: KalshiLifecycle
    official_status: str
    fetched_timestamp: datetime
    source_url: str
    rules_primary: str
    rules_secondary: str
    settlement_timer_seconds: int
    determination_result: KalshiResult | None


@dataclass(frozen=True, slots=True)
class KalshiSettlementRecord:
    row_id: int
    schema_version: int
    asset: Asset
    series: str
    ticker: str
    event_ticker: str
    window_start: datetime
    window_end: datetime
    target: Decimal
    result: KalshiResult
    settlement_timestamp: datetime
    settlement_value: Decimal | None
    expiration_value: str | None
    official_source: str
    fetched_timestamp: datetime
    role: DataRole


@dataclass(frozen=True, slots=True)
class KalshiFeatureMarketRecord:
    """Official metadata safe for pre-settlement feature construction."""

    row_id: int
    schema_version: int
    asset: Asset
    series: str
    ticker: str
    event_ticker: str
    window_start: datetime
    window_end: datetime
    target: Decimal
    lifecycle: KalshiLifecycle
    official_status: str
    fetched_timestamp: datetime
    source_url: str
    rules_primary: str
    rules_secondary: str
    settlement_timer_seconds: int


@dataclass(frozen=True, slots=True)
class KalshiNativeQuoteRecord:
    row_id: int
    schema_version: int
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
    role: DataRole


@dataclass(frozen=True, slots=True)
class TrainingLabelExample:
    """Feature observations and final label are structurally separated."""

    ticker: str
    decision_timestamp: datetime
    market: KalshiFeatureMarketRecord
    observations: tuple[KalshiNativeQuoteRecord, ...]
    label: KalshiSettlementRecord
