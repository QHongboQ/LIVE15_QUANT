"""Typed boundary for venue-native secondary predictive observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from live15_quant.models import (
    Asset,
    FreshnessState,
    SecondaryPriceSemantics,
    SecondaryUnderlyingObservation,
    UnderlyingProvider,
)
from live15_quant.providers.low_latency import BenchmarkTick, LowLatencyProvider
from live15_quant.records import (
    SecondaryUnderlyingObservationRecord,
    UnderlyingObservationRecord,
)


def _seconds(delta_microseconds: int) -> Decimal:
    return Decimal(delta_microseconds) / Decimal(1_000_000)


def _age_seconds(later: datetime, earlier: datetime) -> Decimal:
    delta = later - earlier
    return _seconds((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds)


def secondary_from_benchmark_tick(
    tick: BenchmarkTick,
    *,
    max_source_age_seconds: float,
) -> SecondaryUnderlyingObservation:
    """Convert only the two approved exact benchmark identities into runtime truth."""

    age = _age_seconds(tick.socket_received_timestamp, tick.source_timestamp)
    max_age = Decimal(str(max_source_age_seconds))
    freshness = (
        FreshnessState.UNKNOWN
        if age < 0
        else FreshnessState.FRESH
        if age <= max_age
        else FreshnessState.STALE
    )
    if tick.provider is LowLatencyProvider.BINANCE_SPOT:
        provider = UnderlyingProvider.BINANCE_SPOT
        semantics = SecondaryPriceSemantics.AGGREGATE_TRADE
    elif tick.provider is LowLatencyProvider.HYPERLIQUID_PERP:
        provider = UnderlyingProvider.HYPERLIQUID_PERP
        semantics = SecondaryPriceSemantics.BBO_MIDPOINT
    else:
        raise ValueError("benchmark provider is not an approved runtime secondary")
    return SecondaryUnderlyingObservation(
        asset=tick.asset,
        provider=provider,
        instrument=tick.instrument_id,
        price=tick.price,
        price_semantics=semantics,
        bid=tick.bid,
        ask=tick.ask,
        source_timestamp=tick.source_timestamp,
        received_timestamp=tick.socket_received_timestamp,
        provenance=tick.provenance,
        freshness=freshness,
        source_event_id=tick.source_event_id,
    )


@dataclass(frozen=True, slots=True)
class SecondaryFeatureBoundary:
    """Leakage-safe metadata boundary; intentionally absent from the feature registry."""

    asset: Asset
    decision_timestamp: datetime
    secondary_price: Decimal | None
    secondary_bid: Decimal | None
    secondary_ask: Decimal | None
    secondary_age_seconds: Decimal | None
    primary_secondary_price_diff: Decimal | None
    primary_secondary_age_diff: Decimal | None
    missing_reason: str | None


def build_secondary_feature_boundary(
    primary: UnderlyingObservationRecord | None,
    secondary: SecondaryUnderlyingObservationRecord | None,
    *,
    asset: Asset,
    decision_timestamp: datetime,
) -> SecondaryFeatureBoundary:
    """Expose only observations received by decision time; never perform source fallback."""

    if decision_timestamp.tzinfo is None or decision_timestamp.utcoffset() is None:
        raise ValueError("decision timestamp must be timezone-aware")
    if primary is None or secondary is None:
        return SecondaryFeatureBoundary(
            asset, decision_timestamp, None, None, None, None, None, None, "source_unavailable"
        )
    if primary.asset is not asset or secondary.asset is not asset:
        raise ValueError("primary/secondary asset identity mismatch")
    if (
        primary.received_timestamp > decision_timestamp
        or secondary.received_timestamp > decision_timestamp
    ):
        return SecondaryFeatureBoundary(
            asset,
            decision_timestamp,
            None,
            None,
            None,
            None,
            None,
            None,
            "future_observation_unavailable",
        )
    primary_age = _age_seconds(decision_timestamp, primary.received_timestamp)
    secondary_age = _age_seconds(decision_timestamp, secondary.received_timestamp)
    return SecondaryFeatureBoundary(
        asset=asset,
        decision_timestamp=decision_timestamp,
        secondary_price=secondary.price,
        secondary_bid=secondary.bid,
        secondary_ask=secondary.ask,
        secondary_age_seconds=secondary_age,
        primary_secondary_price_diff=secondary.price - primary.price,
        primary_secondary_age_diff=secondary_age - primary_age,
        missing_reason=None,
    )
