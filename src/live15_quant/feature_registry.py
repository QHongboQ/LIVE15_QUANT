"""Versioned, human-readable definitions for leak-safe model features."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

FEATURE_SCHEMA_VERSION = "1.0.0"


class FeatureFamily(StrEnum):
    CONTRACT_GEOMETRY = "contract_geometry"
    UNDERLYING_RETURN = "underlying_return"
    VOLATILITY = "volatility"
    CONTRACT_MARKET = "contract_market"
    MICROSTRUCTURE = "microstructure"
    MARKET_IMPLIED = "market_implied"


class MissingReason(StrEnum):
    TRULY_MISSING = "truly_missing"
    STALE = "stale"
    NOT_ENOUGH_LOOKBACK = "not_enough_lookback"
    SOURCE_UNAVAILABLE = "source_unavailable"
    MARKET_CLOSED = "market_closed"
    MARKET_SIDE_UNAVAILABLE = "market_side_unavailable"


class MissingPolicy(StrEnum):
    PRESERVE_NULL_WITH_REASON = "preserve_null_with_reason"


class TimestampSemantics(StrEnum):
    DECISION_TIME = "decision_time"
    FETCHED_ASOF = "fetched_asof"
    RECEIVED_ASOF_AND_SOURCE_ASOF = "received_asof_and_source_asof"
    RECEIVED_ASOF = "received_asof"


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    name: str
    family: FeatureFamily
    unit: str
    formula: str
    lookback_seconds: int
    missing_policy: MissingPolicy
    timestamp_semantics: TimestampSemantics

    def __post_init__(self) -> None:
        if not self.name or not self.unit or not self.formula:
            raise ValueError("feature definitions require name, unit, and formula")
        if self.lookback_seconds < 0:
            raise ValueError("feature lookback must be non-negative")


def _definition(
    name: str,
    family: FeatureFamily,
    unit: str,
    formula: str,
    lookback_seconds: int = 0,
    timestamp_semantics: TimestampSemantics = TimestampSemantics.DECISION_TIME,
) -> FeatureDefinition:
    return FeatureDefinition(
        name=name,
        family=family,
        unit=unit,
        formula=formula,
        lookback_seconds=lookback_seconds,
        missing_policy=MissingPolicy.PRESERVE_NULL_WITH_REASON,
        timestamp_semantics=timestamp_semantics,
    )


FEATURE_REGISTRY = (
    _definition(
        "underlying_price",
        FeatureFamily.CONTRACT_GEOMETRY,
        "quote_currency",
        "latest predictive underlying price known at decision time",
        timestamp_semantics=TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
    ),
    _definition(
        "target_price",
        FeatureFamily.CONTRACT_GEOMETRY,
        "quote_currency",
        "official contract target last observed by decision time",
        timestamp_semantics=TimestampSemantics.FETCHED_ASOF,
    ),
    _definition(
        "absolute_distance_to_target",
        FeatureFamily.CONTRACT_GEOMETRY,
        "quote_currency",
        "abs(underlying_price - target_price)",
        timestamp_semantics=TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
    ),
    _definition(
        "signed_distance_to_target",
        FeatureFamily.CONTRACT_GEOMETRY,
        "quote_currency",
        "underlying_price - target_price",
        timestamp_semantics=TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
    ),
    _definition(
        "normalized_distance_to_target",
        FeatureFamily.CONTRACT_GEOMETRY,
        "ratio",
        "(underlying_price - target_price) / target_price",
        timestamp_semantics=TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
    ),
    _definition(
        "time_remaining_seconds",
        FeatureFamily.CONTRACT_GEOMETRY,
        "seconds",
        "window_end - decision_timestamp",
    ),
    _definition(
        "distance_volatility_ratio",
        FeatureFamily.CONTRACT_GEOMETRY,
        "ratio",
        "normalized_distance_to_target / realized_volatility_300s",
        300,
        TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
    ),
    *(
        _definition(
            f"return_{seconds}s",
            FeatureFamily.UNDERLYING_RETURN,
            "ratio",
            f"latest_price / price_asof(decision-{seconds}s) - 1",
            seconds,
            TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
        )
        for seconds in (15, 30, 60, 120, 300)
    ),
    _definition(
        "return_acceleration",
        FeatureFamily.UNDERLYING_RETURN,
        "ratio",
        "return_15s - (return_30s - return_15s)",
        30,
        TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
    ),
    _definition(
        "return_momentum",
        FeatureFamily.UNDERLYING_RETURN,
        "ratio",
        "return_15s + return_30s + return_60s",
        60,
        TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
    ),
    *(
        _definition(
            f"realized_volatility_{seconds}s",
            FeatureFamily.VOLATILITY,
            "return_standard_deviation",
            "population standard deviation of consecutive simple returns",
            seconds,
            TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
        )
        for seconds in (60, 120, 300)
    ),
    _definition(
        "price_range_60s",
        FeatureFamily.VOLATILITY,
        "ratio",
        "(max(price)-min(price))/latest_price over trailing 60s",
        60,
        TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
    ),
    _definition(
        "volatility_change",
        FeatureFamily.VOLATILITY,
        "return_standard_deviation",
        "realized_volatility_60s - realized_volatility_300s",
        300,
        TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
    ),
    _definition(
        "volatility_regime_ratio",
        FeatureFamily.VOLATILITY,
        "ratio",
        "realized_volatility_60s / realized_volatility_300s",
        300,
        TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
    ),
    *(
        _definition(
            name,
            FeatureFamily.CONTRACT_MARKET,
            unit,
            formula,
            0,
            TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
        )
        for name, unit, formula in (
            ("yes_bid", "dollars_per_contract", "latest official Yes bid"),
            ("yes_ask", "dollars_per_contract", "latest official Yes ask"),
            ("no_bid", "dollars_per_contract", "latest official No bid"),
            ("no_ask", "dollars_per_contract", "latest official No ask"),
            ("yes_spread", "dollars_per_contract", "yes_ask - yes_bid"),
            ("yes_midpoint", "dollars_per_contract", "(yes_bid + yes_ask) / 2"),
            ("last_trade", "dollars_per_contract", "latest official last trade"),
            ("quote_age_seconds", "seconds", "decision_timestamp - quote received_timestamp"),
        )
    ),
    *(
        _definition(
            name,
            FeatureFamily.MICROSTRUCTURE,
            unit,
            formula,
            0,
            TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
        )
        for name, unit, formula in (
            ("yes_top_depth", "contracts", "quantity at best explicit Yes bid level"),
            ("no_top_depth", "contracts", "quantity at best explicit No bid level"),
            ("yes_cumulative_depth", "contracts", "sum of retained explicit Yes bid depth"),
            ("no_cumulative_depth", "contracts", "sum of retained explicit No bid depth"),
            (
                "top_depth_imbalance",
                "ratio",
                "(yes_top_depth-no_top_depth)/(yes_top_depth+no_top_depth)",
            ),
            (
                "orderbook_imbalance",
                "ratio",
                "(yes_cumulative_depth-no_cumulative_depth)/(yes_cumulative_depth+no_cumulative_depth)",
            ),
            ("depth_ratio", "ratio", "yes_cumulative_depth / no_cumulative_depth"),
            (
                "spread_depth_interaction",
                "dollars_per_contract_per_contract",
                "yes_spread / (yes_cumulative_depth + no_cumulative_depth)",
            ),
            (
                "yes_top_depth_change",
                "contracts",
                "latest Yes top depth minus previous observed Yes top depth",
            ),
            (
                "no_top_depth_change",
                "contracts",
                "latest No top depth minus previous observed No top depth",
            ),
        )
    ),
    *(
        _definition(
            name,
            FeatureFamily.MARKET_IMPLIED,
            "dollars_per_contract",
            formula,
            0,
            TimestampSemantics.RECEIVED_ASOF_AND_SOURCE_ASOF,
        )
        for name, formula in (
            (
                "market_probability_lower",
                "latest executable Yes bid; not asserted to be true probability",
            ),
            (
                "market_probability_upper",
                "latest executable Yes ask; not asserted to be true probability",
            ),
            ("market_probability_midpoint", "(Yes bid + Yes ask) / 2; descriptive only"),
            ("market_probability_width", "Yes ask - Yes bid"),
        )
    ),
)

FEATURE_BY_NAME = {definition.name: definition for definition in FEATURE_REGISTRY}

if len(FEATURE_BY_NAME) != len(FEATURE_REGISTRY):
    raise RuntimeError("feature registry contains duplicate names")
