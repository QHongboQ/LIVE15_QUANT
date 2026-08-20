"""Provider-independent hard-risk contracts for future execution.

No limit values are chosen here. A future approved configuration must supply them,
and execution adapters must never own or mutate them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from live15_quant.execution import (
    ExecutionAccountState,
    ExecutionAction,
    ExecutionOrderRequest,
    ExecutionPosition,
)
from live15_quant.models import FreshnessState


class RiskBlockReason(StrEnum):
    """Non-overridable reasons a future hard-risk layer can reject an order."""

    MAX_ORDER_NOTIONAL = "max_order_notional"
    MAX_EVENT_EXPOSURE = "max_event_exposure"
    MAX_DAILY_LOSS = "max_daily_loss"
    MAX_TOTAL_EXPOSURE = "max_total_exposure"
    CONSECUTIVE_LOSS_HALT = "consecutive_loss_halt"
    STALE_QUOTE = "stale_quote"
    DATA_SOURCE_UNHEALTHY = "data_source_unhealthy"
    FILL_STATE_UNCERTAIN = "fill_state_uncertain"
    MISSING_BID_ASK = "missing_bid_ask"
    MAPPING_UNCERTAIN = "mapping_uncertain"
    KILL_SWITCH = "kill_switch"
    INSUFFICIENT_BUYING_POWER = "insufficient_buying_power"


@dataclass(frozen=True, slots=True)
class HardRiskLimits:
    """Required, immutable limits; intentionally has no project defaults."""

    max_order_notional: Decimal
    max_event_exposure: Decimal
    max_daily_loss: Decimal
    max_total_exposure: Decimal
    consecutive_loss_halt_count: int

    def __post_init__(self) -> None:
        values = (
            self.max_order_notional,
            self.max_event_exposure,
            self.max_daily_loss,
            self.max_total_exposure,
        )
        if any(not value.is_finite() or value <= 0 for value in values):
            raise ValueError("hard-risk monetary limits must be positive")
        if self.consecutive_loss_halt_count <= 0:
            raise ValueError("consecutive loss halt count must be positive")


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    """Current safety signals supplied independently of the execution provider."""

    quote_freshness: FreshnessState
    data_sources_healthy: bool
    fill_state_certain: bool
    event_exposure: Decimal
    consecutive_losses: int
    total_exposure: Decimal = Decimal(0)
    daily_pnl: Decimal = Decimal(0)
    quote_fields_present: bool = True
    mapping_verified: bool = True
    kill_switch_active: bool = False
    estimated_fees: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        observations = (
            self.event_exposure,
            self.total_exposure,
            self.daily_pnl,
            self.estimated_fees,
        )
        if any(not value.is_finite() for value in observations):
            raise ValueError("risk observations must be finite")
        if (
            self.event_exposure < 0
            or self.total_exposure < 0
            or self.estimated_fees < 0
            or self.consecutive_losses < 0
        ):
            raise ValueError("risk observations must be non-negative")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Explicit allow/block result produced before any provider submission."""

    allowed: bool
    reasons: tuple[RiskBlockReason, ...]

    def __post_init__(self) -> None:
        if self.allowed and self.reasons:
            raise ValueError("allowed decisions must not contain block reasons")
        if not self.allowed and not self.reasons:
            raise ValueError("blocked decisions require at least one reason")


class HardRiskLayer(Protocol):
    """External gate that must run before an ExecutionProvider write method."""

    @property
    def limits(self) -> HardRiskLimits: ...

    def evaluate(
        self,
        request: ExecutionOrderRequest,
        account: ExecutionAccountState,
        position: ExecutionPosition | None,
        snapshot: RiskSnapshot,
    ) -> RiskDecision: ...


@dataclass(frozen=True, slots=True)
class ImmutableHardRiskLayer:
    """Deterministic, provider-independent hard gate for paper and future execution."""

    _limits: HardRiskLimits

    def __init__(self, limits: HardRiskLimits) -> None:
        object.__setattr__(self, "_limits", limits)

    @property
    def limits(self) -> HardRiskLimits:
        return self._limits

    def evaluate(
        self,
        request: ExecutionOrderRequest,
        account: ExecutionAccountState,
        position: ExecutionPosition | None,
        snapshot: RiskSnapshot,
    ) -> RiskDecision:
        del position
        reasons: list[RiskBlockReason] = []
        price = Decimal(1) if request.limit_price is None else request.limit_price
        notional = request.quantity * price
        order_cost = notional + snapshot.estimated_fees
        if snapshot.kill_switch_active:
            reasons.append(RiskBlockReason.KILL_SWITCH)
        if snapshot.quote_freshness is not FreshnessState.FRESH:
            reasons.append(RiskBlockReason.STALE_QUOTE)
        if not snapshot.quote_fields_present:
            reasons.append(RiskBlockReason.MISSING_BID_ASK)
        if not snapshot.mapping_verified:
            reasons.append(RiskBlockReason.MAPPING_UNCERTAIN)
        if not snapshot.data_sources_healthy:
            reasons.append(RiskBlockReason.DATA_SOURCE_UNHEALTHY)
        if not snapshot.fill_state_certain:
            reasons.append(RiskBlockReason.FILL_STATE_UNCERTAIN)
        if request.action is ExecutionAction.BUY:
            if snapshot.consecutive_losses >= self._limits.consecutive_loss_halt_count:
                reasons.append(RiskBlockReason.CONSECUTIVE_LOSS_HALT)
            if snapshot.daily_pnl <= -self._limits.max_daily_loss:
                reasons.append(RiskBlockReason.MAX_DAILY_LOSS)
            if order_cost > self._limits.max_order_notional:
                reasons.append(RiskBlockReason.MAX_ORDER_NOTIONAL)
            if snapshot.event_exposure + notional > self._limits.max_event_exposure:
                reasons.append(RiskBlockReason.MAX_EVENT_EXPOSURE)
            if snapshot.total_exposure + notional > self._limits.max_total_exposure:
                reasons.append(RiskBlockReason.MAX_TOTAL_EXPOSURE)
            if order_cost > account.buying_power:
                reasons.append(RiskBlockReason.INSUFFICIENT_BUYING_POWER)
        unique = tuple(dict.fromkeys(reasons))
        return RiskDecision(allowed=not unique, reasons=unique)
