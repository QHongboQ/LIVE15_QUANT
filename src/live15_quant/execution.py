"""Typed boundary for a future event-contract execution adapter.

This module contains contracts only. It has no Robinhood implementation, credentials,
network calls, or order-routing code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from live15_quant.models import Asset


class ContractOutcome(StrEnum):
    """The explicit event-contract side; never inferred from a complementary price."""

    YES = "yes"
    NO = "no"


class ExecutionAction(StrEnum):
    """Requested position direction at the execution boundary."""

    BUY = "buy"
    SELL = "sell"


class ExecutionMode(StrEnum):
    """Whether an adapter can affect money; Milestone 5 implements PAPER only."""

    PAPER = "paper"
    DEMO = "demo"
    PRODUCTION = "production"


class ExecutionOrderState(StrEnum):
    """Provider-neutral lifecycle for a submitted order."""

    NOT_SUBMITTED = "not_submitted"
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ExecutionCapabilities:
    """Capabilities proven by a provider's current official schema."""

    event_contract_discovery: bool
    event_contract_quotes: bool
    event_contract_positions: bool
    event_contract_orders: bool
    event_contract_cancellation: bool
    mode: ExecutionMode = ExecutionMode.PAPER


@dataclass(frozen=True, slots=True)
class ExecutionAccountState:
    """Minimal account state needed by an external hard-risk layer."""

    account_id: str
    buying_power: Decimal
    total_exposure: Decimal
    daily_realized_pnl: Decimal
    currency: str
    received_timestamp: datetime

    def __post_init__(self) -> None:
        if not self.account_id or not self.currency:
            raise ValueError("execution account identifiers must not be empty")
        monetary = (self.buying_power, self.total_exposure, self.daily_realized_pnl)
        if any(not value.is_finite() for value in monetary):
            raise ValueError("execution account monetary values must be finite")
        if self.total_exposure < 0:
            raise ValueError("total exposure must be non-negative")
        if self.received_timestamp.tzinfo is None or self.received_timestamp.utcoffset() is None:
            raise ValueError("account timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ExecutionPosition:
    """One explicitly identified Yes or No position."""

    asset: Asset
    event_id: str
    contract_id: str
    outcome: ContractOutcome
    quantity: Decimal
    average_price: Decimal | None
    received_timestamp: datetime

    def __post_init__(self) -> None:
        if not self.event_id or not self.contract_id:
            raise ValueError("position event and contract identifiers must not be empty")
        if not self.quantity.is_finite() or self.quantity < 0:
            raise ValueError("position quantity must be non-negative")
        if self.average_price is not None and (
            not self.average_price.is_finite() or not Decimal(0) <= self.average_price <= Decimal(1)
        ):
            raise ValueError("position average price must be within [0, 1]")
        if self.received_timestamp.tzinfo is None or self.received_timestamp.utcoffset() is None:
            raise ValueError("position timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ExecutionOrderRequest:
    """Provider-neutral intent; a hard-risk layer must approve it before submission."""

    account_id: str
    asset: Asset
    event_id: str
    contract_id: str
    outcome: ContractOutcome
    action: ExecutionAction
    quantity: Decimal
    limit_price: Decimal | None
    client_order_id: str

    def __post_init__(self) -> None:
        if not all((self.account_id, self.event_id, self.contract_id, self.client_order_id)):
            raise ValueError("order identifiers must not be empty")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if self.limit_price is not None and (
            not self.limit_price.is_finite() or not Decimal(0) <= self.limit_price <= Decimal(1)
        ):
            raise ValueError("order limit price must be within [0, 1]")


@dataclass(frozen=True, slots=True)
class ExecutionOrderStatus:
    """Current provider-reported order and fill state."""

    provider_order_id: str
    client_order_id: str
    state: ExecutionOrderState
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    received_timestamp: datetime

    def __post_init__(self) -> None:
        if not self.provider_order_id or not self.client_order_id:
            raise ValueError("order status identifiers must not be empty")
        if not self.requested_quantity.is_finite() or self.requested_quantity <= 0:
            raise ValueError("requested quantity must be positive")
        if (
            not self.filled_quantity.is_finite()
            or not Decimal(0) <= self.filled_quantity <= self.requested_quantity
        ):
            raise ValueError("filled quantity must be within the requested quantity")
        if self.average_fill_price is not None and (
            not self.average_fill_price.is_finite()
            or not Decimal(0) <= self.average_fill_price <= Decimal(1)
        ):
            raise ValueError("average fill price must be within [0, 1]")
        if self.received_timestamp.tzinfo is None or self.received_timestamp.utcoffset() is None:
            raise ValueError("order status timestamp must be timezone-aware")


class ExecutionProvider(Protocol):
    """Provider boundary implemented locally by paper; no Demo/Production adapter exists."""

    @property
    def capabilities(self) -> ExecutionCapabilities: ...

    def get_account_state(self, account_id: str) -> ExecutionAccountState: ...

    def get_position(
        self,
        event_id: str,
        contract_id: str,
        outcome: ContractOutcome | None = None,
    ) -> ExecutionPosition | None: ...

    def submit_order(self, request: ExecutionOrderRequest) -> ExecutionOrderStatus: ...

    def cancel_order(self, account_id: str, provider_order_id: str) -> ExecutionOrderStatus: ...

    def close_or_reduce_position(self, request: ExecutionOrderRequest) -> ExecutionOrderStatus: ...

    def get_order_status(self, account_id: str, provider_order_id: str) -> ExecutionOrderStatus: ...
