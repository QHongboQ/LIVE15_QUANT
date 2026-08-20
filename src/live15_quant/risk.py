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
        if any(value <= 0 for value in values):
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

    def __post_init__(self) -> None:
        if self.event_exposure < 0 or self.consecutive_losses < 0:
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
