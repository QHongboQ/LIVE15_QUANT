"""Shared offline/live feature calculation with explicit as-of semantics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from types import MappingProxyType

from live15_quant.feature_registry import FEATURE_BY_NAME, MissingReason
from live15_quant.kalshi_lifecycle import KalshiLifecycle
from live15_quant.models import Asset, FreshnessState
from live15_quant.records import (
    CoinbaseTickRecord,
    KalshiFeatureMarketRecord,
    KalshiNativeQuoteRecord,
)

COINBASE_PRODUCT_BY_ASSET: Mapping[Asset, str] = MappingProxyType(
    {
        Asset.BTC: "BTC-USD",
        Asset.ETH: "ETH-USD",
        Asset.XRP: "XRP-USD",
        Asset.SOL: "SOL-USD",
        Asset.DOGE: "DOGE-USD",
    }
)


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    """Decision offsets are configuration; calculation has no fixed real-world schedule."""

    time_remaining: tuple[timedelta, ...]
    quote_max_age: timedelta = timedelta(seconds=15)
    underlying_max_age: timedelta = timedelta(seconds=15)

    def __post_init__(self) -> None:
        if not self.time_remaining:
            raise ValueError("sampling policy requires at least one decision offset")
        seconds = tuple(decimal_seconds(offset) for offset in self.time_remaining)
        if any(value <= 0 or value > Decimal(15 * 60) for value in seconds):
            raise ValueError("decision offsets must be within the 15-minute window")
        if len(set(seconds)) != len(seconds):
            raise ValueError("decision offsets must be unique")
        if (
            decimal_seconds(self.quote_max_age) <= 0
            or decimal_seconds(self.underlying_max_age) <= 0
        ):
            raise ValueError("source age limits must be positive")

    def decision_times(self, window_start: datetime, window_end: datetime) -> tuple[datetime, ...]:
        _aware(window_start, "window_start")
        _aware(window_end, "window_end")
        decisions = {
            window_end - offset
            for offset in self.time_remaining
            if window_start <= window_end - offset < window_end
        }
        return tuple(sorted(decision.astimezone(UTC) for decision in decisions))


@dataclass(frozen=True, slots=True)
class FeatureObservation:
    name: str
    value: Decimal | None
    missing_reason: MissingReason | None
    source_timestamp: datetime | None

    def __post_init__(self) -> None:
        if self.name not in FEATURE_BY_NAME:
            raise ValueError(f"unregistered feature: {self.name}")
        if (self.value is None) == (self.missing_reason is None):
            raise ValueError("feature must have exactly one of value or missing reason")
        if self.value is not None and not self.value.is_finite():
            raise ValueError("feature values must be finite Decimals")
        if self.source_timestamp is not None:
            _aware(self.source_timestamp, "feature source timestamp")


@dataclass(frozen=True, slots=True)
class FeatureVector:
    decision_timestamp: datetime
    observations: tuple[FeatureObservation, ...]

    def __post_init__(self) -> None:
        _aware(self.decision_timestamp, "decision_timestamp")
        names = tuple(item.name for item in self.observations)
        if len(set(names)) != len(names) or set(names) != set(FEATURE_BY_NAME):
            raise ValueError("feature vector must contain every registered feature exactly once")
        if any(
            item.source_timestamp is not None and item.source_timestamp > self.decision_timestamp
            for item in self.observations
        ):
            raise ValueError("future feature timestamp detected")

    def by_name(self) -> Mapping[str, FeatureObservation]:
        return MappingProxyType({item.name: item for item in self.observations})


@dataclass(frozen=True, slots=True)
class FeatureInputs:
    market: KalshiFeatureMarketRecord
    quotes: tuple[KalshiNativeQuoteRecord, ...]
    ticks: tuple[CoinbaseTickRecord, ...]
    decision_timestamp: datetime


class FeatureEngine:
    """Calculate only values available as of one decision timestamp."""

    def __init__(self, policy: SamplingPolicy) -> None:
        self.policy = policy

    def compute(self, inputs: FeatureInputs) -> FeatureVector:
        decision = _aware(inputs.decision_timestamp, "decision_timestamp")
        market = inputs.market
        if not market.window_start <= decision < market.window_end:
            raise ValueError("decision timestamp must be inside the market window")
        if market.fetched_timestamp > decision:
            raise ValueError("future market metadata detected")
        if market.lifecycle not in {KalshiLifecycle.OPEN, KalshiLifecycle.PAUSED}:
            raise ValueError("terminal or future lifecycle cannot enter feature calculation")

        product = COINBASE_PRODUCT_BY_ASSET.get(market.asset)
        ticks = tuple(
            sorted(
                (
                    tick
                    for tick in inputs.ticks
                    if product is not None
                    and tick.product == product
                    and tick.received_timestamp <= decision
                    and (tick.exchange_timestamp is None or tick.exchange_timestamp <= decision)
                ),
                key=lambda tick: (tick.received_timestamp, tick.row_id),
            )
        )
        quotes = tuple(
            sorted(
                (
                    quote
                    for quote in inputs.quotes
                    if quote.ticker == market.ticker
                    and quote.asset is market.asset
                    and quote.series == market.series
                    and quote.event_ticker == market.event_ticker
                    and quote.received_timestamp <= decision
                    and (quote.source_timestamp is None or quote.source_timestamp <= decision)
                    and quote.received_timestamp < market.window_end
                ),
                key=lambda quote: (quote.received_timestamp, quote.row_id),
            )
        )
        values: dict[str, FeatureObservation] = {}

        def present(name: str, value: Decimal, source: datetime | None = None) -> None:
            values[name] = FeatureObservation(name, value, None, source)

        def missing(name: str, reason: MissingReason) -> None:
            values[name] = FeatureObservation(name, None, reason, None)

        present("target_price", market.target, market.fetched_timestamp)
        present(
            "time_remaining_seconds",
            decimal_seconds(market.window_end - decision),
            decision,
        )

        latest_tick = ticks[-1] if ticks else None
        tick_reason: MissingReason | None = None
        if product is None:
            tick_reason = MissingReason.SOURCE_UNAVAILABLE
        elif latest_tick is None:
            tick_reason = MissingReason.SOURCE_UNAVAILABLE
        elif _too_old(
            decision,
            latest_tick.received_timestamp,
            latest_tick.exchange_timestamp,
            self.policy.underlying_max_age,
        ):
            tick_reason = MissingReason.STALE
        if tick_reason is not None:
            for name in (
                "underlying_price",
                "absolute_distance_to_target",
                "signed_distance_to_target",
                "normalized_distance_to_target",
            ):
                missing(name, tick_reason)
        else:
            assert latest_tick is not None
            signed = latest_tick.price - market.target
            present("underlying_price", latest_tick.price, latest_tick.received_timestamp)
            present("absolute_distance_to_target", abs(signed), latest_tick.received_timestamp)
            present("signed_distance_to_target", signed, latest_tick.received_timestamp)
            present(
                "normalized_distance_to_target",
                signed / market.target,
                latest_tick.received_timestamp,
            )

        returns: dict[int, Decimal | None] = {}
        for seconds in (15, 30, 60, 120, 300):
            name = f"return_{seconds}s"
            if tick_reason is not None:
                missing(name, tick_reason)
                returns[seconds] = None
                continue
            assert latest_tick is not None
            boundary = decision - timedelta(seconds=seconds)
            earlier = _latest_tick_at_or_before(ticks, boundary)
            if earlier is None or _too_old(
                boundary,
                earlier.received_timestamp,
                earlier.exchange_timestamp,
                self.policy.underlying_max_age,
            ):
                missing(name, MissingReason.NOT_ENOUGH_LOOKBACK)
                returns[seconds] = None
            else:
                value = latest_tick.price / earlier.price - Decimal(1)
                present(name, value, latest_tick.received_timestamp)
                returns[seconds] = value

        if returns[15] is None or returns[30] is None:
            missing("return_acceleration", _derived_missing(values, "return_15s", "return_30s"))
        else:
            present(
                "return_acceleration",
                returns[15] - (returns[30] - returns[15]),
                latest_tick.received_timestamp if latest_tick else None,
            )
        if any(returns[seconds] is None for seconds in (15, 30, 60)):
            missing(
                "return_momentum",
                _derived_missing(values, "return_15s", "return_30s", "return_60s"),
            )
        else:
            present(
                "return_momentum",
                sum((returns[15], returns[30], returns[60]), Decimal(0)),  # type: ignore[arg-type]
                latest_tick.received_timestamp if latest_tick else None,
            )

        volatilities: dict[int, Decimal | None] = {}
        for seconds in (60, 120, 300):
            name = f"realized_volatility_{seconds}s"
            if tick_reason is not None:
                missing(name, tick_reason)
                volatilities[seconds] = None
                continue
            window_ticks = _ticks_since(ticks, decision - timedelta(seconds=seconds))
            window_covered = bool(
                window_ticks
                and window_ticks[0].received_timestamp
                <= decision - timedelta(seconds=seconds) + self.policy.underlying_max_age
            )
            volatility = _realized_volatility(window_ticks) if window_covered else None
            if volatility is None:
                missing(name, MissingReason.NOT_ENOUGH_LOOKBACK)
            else:
                present(name, volatility, latest_tick.received_timestamp if latest_tick else None)
            volatilities[seconds] = volatility

        range_ticks = _ticks_since(ticks, decision - timedelta(seconds=60))
        if tick_reason is not None:
            missing("price_range_60s", tick_reason)
        elif (
            len(range_ticks) < 2
            or latest_tick is None
            or range_ticks[0].received_timestamp
            > decision - timedelta(seconds=60) + self.policy.underlying_max_age
        ):
            missing("price_range_60s", MissingReason.NOT_ENOUGH_LOOKBACK)
        else:
            prices = tuple(tick.price for tick in range_ticks)
            present(
                "price_range_60s",
                (max(prices) - min(prices)) / latest_tick.price,
                latest_tick.received_timestamp,
            )
        if volatilities[60] is None or volatilities[300] is None:
            reason = _derived_missing(values, "realized_volatility_60s", "realized_volatility_300s")
            missing("volatility_change", reason)
            missing("volatility_regime_ratio", reason)
        else:
            present(
                "volatility_change",
                volatilities[60] - volatilities[300],
                latest_tick.received_timestamp if latest_tick else None,
            )
            if volatilities[300] == 0:
                missing("volatility_regime_ratio", MissingReason.TRULY_MISSING)
            else:
                present(
                    "volatility_regime_ratio",
                    volatilities[60] / volatilities[300],
                    latest_tick.received_timestamp if latest_tick else None,
                )
        normalized = values["normalized_distance_to_target"]
        if normalized.value is None or volatilities[300] is None:
            missing(
                "distance_volatility_ratio",
                _derived_missing(
                    values,
                    "normalized_distance_to_target",
                    "realized_volatility_300s",
                ),
            )
        elif volatilities[300] == 0:
            missing("distance_volatility_ratio", MissingReason.TRULY_MISSING)
        else:
            present(
                "distance_volatility_ratio",
                normalized.value / volatilities[300],
                latest_tick.received_timestamp if latest_tick else None,
            )

        latest_quote = quotes[-1] if quotes else None
        quote_reason: MissingReason | None = None
        if latest_quote is None:
            quote_reason = MissingReason.SOURCE_UNAVAILABLE
        elif latest_quote.freshness is FreshnessState.STALE or _too_old(
            decision,
            latest_quote.received_timestamp,
            latest_quote.source_timestamp,
            self.policy.quote_max_age,
        ):
            quote_reason = MissingReason.STALE
        elif latest_quote.freshness is FreshnessState.UNKNOWN:
            quote_reason = MissingReason.SOURCE_UNAVAILABLE
        if latest_quote is None:
            missing("quote_age_seconds", MissingReason.SOURCE_UNAVAILABLE)
        else:
            present(
                "quote_age_seconds",
                decimal_seconds(decision - latest_quote.received_timestamp),
                latest_quote.received_timestamp,
            )

        side_names = ("yes_bid", "yes_ask", "no_bid", "no_ask")
        for name in side_names:
            if quote_reason is not None:
                missing(name, quote_reason)
            else:
                assert latest_quote is not None
                value = getattr(latest_quote, name)
                if value is None:
                    missing(name, MissingReason.MARKET_SIDE_UNAVAILABLE)
                else:
                    present(name, value, latest_quote.received_timestamp)
        if quote_reason is not None:
            missing("last_trade", quote_reason)
        elif latest_quote is None or latest_quote.last_trade is None:
            missing("last_trade", MissingReason.TRULY_MISSING)
        else:
            present("last_trade", latest_quote.last_trade, latest_quote.received_timestamp)

        if values["yes_bid"].value is None or values["yes_ask"].value is None:
            reason = _derived_missing(values, "yes_bid", "yes_ask")
            missing("yes_spread", reason)
            missing("yes_midpoint", reason)
        else:
            yes_bid = values["yes_bid"].value
            yes_ask = values["yes_ask"].value
            assert yes_bid is not None and yes_ask is not None and latest_quote is not None
            present("yes_spread", yes_ask - yes_bid, latest_quote.received_timestamp)
            present(
                "yes_midpoint", (yes_bid + yes_ask) / Decimal(2), latest_quote.received_timestamp
            )

        _book_features(
            values,
            quotes,
            latest_quote,
            quote_reason,
            self.policy.quote_max_age,
            present,
            missing,
        )
        for target, source in (
            ("market_probability_lower", "yes_bid"),
            ("market_probability_upper", "yes_ask"),
            ("market_probability_midpoint", "yes_midpoint"),
            ("market_probability_width", "yes_spread"),
        ):
            observation = values[source]
            if observation.value is None:
                assert observation.missing_reason is not None
                missing(target, observation.missing_reason)
            else:
                present(target, observation.value, observation.source_timestamp)

        return FeatureVector(
            decision_timestamp=decision,
            observations=tuple(values[name] for name in FEATURE_BY_NAME),
        )


def _book_features(
    values, quotes, latest_quote, quote_reason, book_change_max_age, present, missing
) -> None:
    names = (
        "yes_top_depth",
        "no_top_depth",
        "yes_cumulative_depth",
        "no_cumulative_depth",
        "top_depth_imbalance",
        "orderbook_imbalance",
        "depth_ratio",
        "spread_depth_interaction",
        "yes_top_depth_change",
        "no_top_depth_change",
    )
    if quote_reason is not None:
        for name in names:
            missing(name, quote_reason)
        return
    assert latest_quote is not None
    yes_top = latest_quote.yes_bid_depth[0].quantity if latest_quote.yes_bid_depth else None
    no_top = latest_quote.no_bid_depth[0].quantity if latest_quote.no_bid_depth else None
    yes_total = sum((level.quantity for level in latest_quote.yes_bid_depth), Decimal(0))
    no_total = sum((level.quantity for level in latest_quote.no_bid_depth), Decimal(0))
    source = latest_quote.received_timestamp
    if yes_top is None:
        missing("yes_top_depth", MissingReason.MARKET_SIDE_UNAVAILABLE)
    else:
        present("yes_top_depth", yes_top, source)
    if no_top is None:
        missing("no_top_depth", MissingReason.MARKET_SIDE_UNAVAILABLE)
    else:
        present("no_top_depth", no_top, source)
    if not latest_quote.yes_bid_depth:
        missing("yes_cumulative_depth", MissingReason.MARKET_SIDE_UNAVAILABLE)
    else:
        present("yes_cumulative_depth", yes_total, source)
    if not latest_quote.no_bid_depth:
        missing("no_cumulative_depth", MissingReason.MARKET_SIDE_UNAVAILABLE)
    else:
        present("no_cumulative_depth", no_total, source)
    if yes_top is None or no_top is None:
        missing("top_depth_imbalance", MissingReason.MARKET_SIDE_UNAVAILABLE)
    elif yes_top + no_top == 0:
        missing("top_depth_imbalance", MissingReason.TRULY_MISSING)
    else:
        present("top_depth_imbalance", (yes_top - no_top) / (yes_top + no_top), source)
    if not latest_quote.yes_bid_depth or not latest_quote.no_bid_depth:
        missing("orderbook_imbalance", MissingReason.MARKET_SIDE_UNAVAILABLE)
        missing("depth_ratio", MissingReason.MARKET_SIDE_UNAVAILABLE)
    elif yes_total + no_total == 0 or no_total == 0:
        missing("orderbook_imbalance", MissingReason.TRULY_MISSING)
        missing("depth_ratio", MissingReason.TRULY_MISSING)
    else:
        present("orderbook_imbalance", (yes_total - no_total) / (yes_total + no_total), source)
        present("depth_ratio", yes_total / no_total, source)
    spread = values["yes_spread"]
    if spread.value is None:
        assert spread.missing_reason is not None
        missing("spread_depth_interaction", spread.missing_reason)
    elif yes_total + no_total == 0:
        missing("spread_depth_interaction", MissingReason.TRULY_MISSING)
    else:
        present("spread_depth_interaction", spread.value / (yes_total + no_total), source)

    previous = quotes[-2] if len(quotes) >= 2 else None
    for side in ("yes", "no"):
        name = f"{side}_top_depth_change"
        current_levels = getattr(latest_quote, f"{side}_bid_depth")
        previous_levels = getattr(previous, f"{side}_bid_depth") if previous is not None else ()
        if previous is None or _too_old(
            latest_quote.received_timestamp,
            previous.received_timestamp,
            previous.source_timestamp,
            book_change_max_age,
        ):
            missing(name, MissingReason.NOT_ENOUGH_LOOKBACK)
        elif not current_levels or not previous_levels:
            missing(name, MissingReason.MARKET_SIDE_UNAVAILABLE)
        else:
            present(name, current_levels[0].quantity - previous_levels[0].quantity, source)


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def decimal_seconds(value: timedelta) -> Decimal:
    return Decimal(value.days * 86400 + value.seconds) + Decimal(value.microseconds) / Decimal(
        1_000_000
    )


def _latest_tick_at_or_before(
    ticks: tuple[CoinbaseTickRecord, ...], boundary: datetime
) -> CoinbaseTickRecord | None:
    return next(
        (
            tick
            for tick in reversed(ticks)
            if tick.received_timestamp <= boundary
            and (tick.exchange_timestamp is None or tick.exchange_timestamp <= boundary)
        ),
        None,
    )


def _too_old(
    reference: datetime,
    received: datetime,
    source: datetime | None,
    maximum_age: timedelta,
) -> bool:
    return reference - received > maximum_age or (
        source is not None and reference - source > maximum_age
    )


def _ticks_since(
    ticks: tuple[CoinbaseTickRecord, ...], boundary: datetime
) -> tuple[CoinbaseTickRecord, ...]:
    return tuple(tick for tick in ticks if tick.received_timestamp >= boundary)


def _realized_volatility(ticks: tuple[CoinbaseTickRecord, ...]) -> Decimal | None:
    if len(ticks) < 3:
        return None
    returns = tuple(
        current.price / previous.price - Decimal(1) for previous, current in pairwise(ticks)
    )
    mean = sum(returns, Decimal(0)) / Decimal(len(returns))
    variance = sum(((value - mean) ** 2 for value in returns), Decimal(0)) / Decimal(len(returns))
    return variance.sqrt()


def _derived_missing(values: Mapping[str, FeatureObservation], *dependencies: str) -> MissingReason:
    reasons = tuple(values[name].missing_reason for name in dependencies)
    priority = (
        MissingReason.STALE,
        MissingReason.SOURCE_UNAVAILABLE,
        MissingReason.MARKET_SIDE_UNAVAILABLE,
        MissingReason.NOT_ENOUGH_LOOKBACK,
        MissingReason.TRULY_MISSING,
    )
    return next(reason for reason in priority if reason in reasons)
