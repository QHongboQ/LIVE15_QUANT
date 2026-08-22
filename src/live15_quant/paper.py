"""Typed local-only paper execution driven by official Kalshi order books."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from live15_quant.execution import (
    ContractOutcome,
    ExecutionAction,
    ExecutionOrderState,
)
from live15_quant.fees import FeeComputation, KalshiTakerFeeModel
from live15_quant.models import (
    Asset,
    DataRole,
    OrderBookLevel,
    PredictionMarketQuote,
)
from live15_quant.risk import RiskDecision


class PaperDecisionType(StrEnum):
    BUY_YES = "buy_yes"
    BUY_NO = "buy_no"
    HOLD = "hold"
    ADD = "add"
    REDUCE = "reduce"
    CLOSE = "close"
    CANCEL = "cancel"


class TimeInForce(StrEnum):
    GOOD_TILL_CANCELED = "good_till_canceled"
    IMMEDIATE_OR_CANCEL = "immediate_or_cancel"
    FILL_OR_KILL = "fill_or_kill"


class PaperExecutionReason(StrEnum):
    FULL_FILL = "full_fill"
    PARTIAL_FILL = "partial_fill"
    NO_FILL = "no_fill"
    PRICE_MOVED = "price_moved"
    RISK_BLOCKED = "risk_blocked"
    HOLD = "hold"
    CANCELLED = "cancelled"
    FILL_UNCERTAIN = "fill_uncertain"


class PaperPositionStatus(StrEnum):
    OPEN = "open"
    PENDING_SETTLEMENT = "pending_settlement"


@dataclass(frozen=True, slots=True)
class PaperSettlement:
    """Official settlement applied to a local paper position exactly once."""

    event_id: str
    outcome_yes: bool
    settlement_timestamp: datetime
    realized_pnl: Decimal


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    decision_id: str
    signal_timestamp: datetime
    asset: Asset
    event_id: str
    contract_id: str
    decision: PaperDecisionType
    outcome: ContractOutcome | None
    quantity: Decimal
    limit_price: Decimal | None
    time_in_force: TimeInForce = TimeInForce.IMMEDIATE_OR_CANCEL
    target_order_id: str | None = None
    role: DataRole = field(init=False, default=DataRole.PAPER_EXECUTION)

    def __post_init__(self) -> None:
        if not all((self.decision_id, self.event_id, self.contract_id)):
            raise ValueError("strategy decision identifiers must not be empty")
        if self.signal_timestamp.tzinfo is None or self.signal_timestamp.utcoffset() is None:
            raise ValueError("signal timestamp must be timezone-aware")
        if self.decision in {PaperDecisionType.HOLD, PaperDecisionType.CANCEL}:
            if self.quantity != 0 or self.outcome is not None or self.limit_price is not None:
                raise ValueError("hold/cancel must not carry an executable order")
        elif (
            not self.quantity.is_finite()
            or self.quantity <= 0
            or self.outcome is None
            or self.limit_price is None
        ):
            raise ValueError("executable decisions require side, positive quantity, and limit")
        if self.decision is PaperDecisionType.BUY_YES and self.outcome is not ContractOutcome.YES:
            raise ValueError("BUY_YES requires the Yes outcome")
        if self.decision is PaperDecisionType.BUY_NO and self.outcome is not ContractOutcome.NO:
            raise ValueError("BUY_NO requires the No outcome")
        if self.limit_price is not None and (
            not self.limit_price.is_finite() or not Decimal(0) <= self.limit_price <= Decimal(1)
        ):
            raise ValueError("decision limit price must be within [0, 1]")
        if self.decision is PaperDecisionType.CANCEL and not self.target_order_id:
            raise ValueError("cancel requires a target order")
        if self.decision is not PaperDecisionType.CANCEL and self.target_order_id is not None:
            raise ValueError("only cancel may target an existing order")


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    fill_id: str
    order_id: str
    asset: Asset
    event_id: str
    contract_id: str
    fill_timestamp: datetime
    outcome: ContractOutcome
    action: ExecutionAction
    quantity: Decimal
    price: Decimal
    fee: FeeComputation
    spread: Decimal
    slippage: Decimal


@dataclass(frozen=True, slots=True)
class PaperExecutionResult:
    order_id: str | None
    decision: StrategyDecision
    submit_timestamp: datetime
    quote_timestamp: datetime | None
    state: ExecutionOrderState
    reason: PaperExecutionReason
    requested_quantity: Decimal
    filled_quantity: Decimal
    requested_price: Decimal | None
    average_fill_price: Decimal | None
    spread: Decimal | None
    slippage: Decimal | None
    fees: Decimal
    fills: tuple[SimulatedFill, ...]
    risk: RiskDecision | None
    venue_ticker: str | None = None
    quote_source: str | None = None


@dataclass(frozen=True, slots=True)
class PaperPosition:
    asset: Asset
    event_id: str
    contract_id: str
    outcome: ContractOutcome
    quantity: Decimal
    average_cost: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal
    status: PaperPositionStatus = PaperPositionStatus.OPEN


@dataclass(frozen=True, slots=True)
class PaperPortfolioState:
    account_id: str
    cash: Decimal
    positions: tuple[PaperPosition, ...]
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    total_exposure: Decimal
    daily_realized_pnl: Decimal
    daily_pnl: Decimal
    consecutive_losses: int
    fill_state_certain: bool
    as_of: datetime


class PaperPortfolio:
    """In-memory projection rebuilt deterministically from persisted fills."""

    def __init__(self, account_id: str, starting_cash: Decimal) -> None:
        if not account_id or not starting_cash.is_finite() or starting_cash < 0:
            raise ValueError("invalid paper portfolio")
        self.account_id = account_id
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self._positions: dict[tuple[str, ContractOutcome], PaperPosition] = {}
        self.realized_pnl = Decimal(0)
        self.daily_realized_pnl = Decimal(0)
        self._daily_date = datetime.now(UTC).date()
        self.fees_paid = Decimal(0)
        self.consecutive_losses = 0
        self.fill_state_certain = True

    def position(self, event_id: str, outcome: ContractOutcome) -> PaperPosition | None:
        return self._positions.get((event_id, outcome))

    def apply_fill(self, fill: SimulatedFill) -> None:
        fill_date = fill.fill_timestamp.astimezone(UTC).date()
        if fill_date != self._daily_date:
            self._daily_date = fill_date
            self.daily_realized_pnl = Decimal(0)
        key = (fill.event_id, fill.outcome)
        current = self._positions.get(key)
        fee = fill.fee.net_fee
        self.fees_paid += fee
        if fill.action is ExecutionAction.BUY:
            old_qty = Decimal(0) if current is None else current.quantity
            old_cost = Decimal(0) if current is None else current.average_cost * old_qty
            new_qty = old_qty + fill.quantity
            average_cost = (old_cost + fill.price * fill.quantity) / new_qty
            position = PaperPosition(
                asset=fill.asset,
                event_id=fill.event_id,
                contract_id=fill.contract_id,
                outcome=fill.outcome,
                quantity=new_qty,
                average_cost=average_cost,
                realized_pnl=Decimal(0) if current is None else current.realized_pnl,
                fees_paid=(Decimal(0) if current is None else current.fees_paid) + fee,
                status=PaperPositionStatus.OPEN,
            )
            self.cash -= fill.price * fill.quantity + fee
            self.realized_pnl -= fee
            self.daily_realized_pnl -= fee
            self._positions[key] = position
            return
        if current is None or fill.quantity > current.quantity:
            raise ValueError("paper sell exceeds the open position")
        gross = (fill.price - current.average_cost) * fill.quantity
        net = gross - fee
        new_qty = current.quantity - fill.quantity
        self.cash += fill.price * fill.quantity - fee
        self.realized_pnl += net
        self.daily_realized_pnl += net
        if new_qty == 0:
            self._positions.pop(key)
        else:
            self._positions[key] = PaperPosition(
                asset=current.asset,
                event_id=current.event_id,
                contract_id=current.contract_id,
                outcome=current.outcome,
                quantity=new_qty,
                average_cost=current.average_cost,
                realized_pnl=current.realized_pnl + net,
                fees_paid=current.fees_paid + fee,
                status=current.status,
            )

    def record_exit_order_pnl(self, net_pnl: Decimal) -> None:
        if net_pnl < 0:
            self.consecutive_losses += 1
        elif net_pnl > 0:
            self.consecutive_losses = 0

    def mark_pending_settlement(self, event_id: str) -> None:
        """Freeze open positions without inventing a settlement result."""

        for key, position in tuple(self._positions.items()):
            if position.event_id == event_id:
                self._positions[key] = replace(
                    position, status=PaperPositionStatus.PENDING_SETTLEMENT
                )

    def settle_event(
        self, event_id: str, *, outcome_yes: bool, settlement_timestamp: datetime
    ) -> PaperSettlement:
        """Apply only official contract truth; underlying prices are never a proxy."""

        if settlement_timestamp.tzinfo is None or settlement_timestamp.utcoffset() is None:
            raise ValueError("paper settlement timestamp must be timezone-aware")
        realized_before = self.realized_pnl
        for key, position in tuple(self._positions.items()):
            if position.event_id != event_id:
                continue
            won = (position.outcome is ContractOutcome.YES) == outcome_yes
            payout = position.quantity if won else Decimal(0)
            # Entry notional was intentionally excluded from realized PnL at buy time;
            # recognize it exactly once when authoritative Kalshi settlement arrives.
            realized = payout - position.average_cost * position.quantity
            self.cash += payout
            self.realized_pnl += realized
            if settlement_timestamp.astimezone(UTC).date() == self._daily_date:
                self.daily_realized_pnl += realized
            self._positions.pop(key)
            self.record_exit_order_pnl(realized)
        return PaperSettlement(
            event_id=event_id,
            outcome_yes=outcome_yes,
            settlement_timestamp=settlement_timestamp.astimezone(UTC),
            realized_pnl=self.realized_pnl - realized_before,
        )

    def state(
        self, marks: dict[tuple[str, ContractOutcome], Decimal], as_of: datetime
    ) -> PaperPortfolioState:
        daily_realized = (
            self.daily_realized_pnl
            if as_of.astimezone(UTC).date() == self._daily_date
            else Decimal(0)
        )
        unrealized = sum(
            (
                (marks.get(key, position.average_cost) - position.average_cost) * position.quantity
                for key, position in self._positions.items()
            ),
            Decimal(0),
        )
        exposure = sum(
            (position.average_cost * position.quantity for position in self._positions.values()),
            Decimal(0),
        )
        return PaperPortfolioState(
            account_id=self.account_id,
            cash=self.cash,
            positions=tuple(
                sorted(self._positions.values(), key=lambda item: (item.event_id, item.outcome))
            ),
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            fees_paid=self.fees_paid,
            total_exposure=exposure,
            daily_realized_pnl=daily_realized,
            daily_pnl=daily_realized + unrealized,
            consecutive_losses=self.consecutive_losses,
            fill_state_certain=self.fill_state_certain,
            as_of=as_of,
        )


class KalshiOrderBookFillSimulator:
    """Consume only executable venue depth; never use midpoint prices."""

    def __init__(self, fee_model: KalshiTakerFeeModel | None = None) -> None:
        self._fees = fee_model or KalshiTakerFeeModel()

    @staticmethod
    def _book(
        quote: PredictionMarketQuote, outcome: ContractOutcome, action: ExecutionAction
    ) -> tuple[tuple[OrderBookLevel, ...], Decimal | None, Decimal | None]:
        if outcome is ContractOutcome.YES:
            bid, ask = quote.yes_bid, quote.yes_ask
            if action is ExecutionAction.BUY:
                levels = tuple(
                    sorted(
                        (
                            OrderBookLevel(Decimal(1) - item.price, item.quantity)
                            for item in quote.no_bid_depth
                        ),
                        key=lambda item: item.price,
                    )
                )
            else:
                levels = tuple(
                    sorted(quote.yes_bid_depth, key=lambda item: item.price, reverse=True)
                )
        else:
            bid, ask = quote.no_bid, quote.no_ask
            if action is ExecutionAction.BUY:
                levels = tuple(
                    sorted(
                        (
                            OrderBookLevel(Decimal(1) - item.price, item.quantity)
                            for item in quote.yes_bid_depth
                        ),
                        key=lambda item: item.price,
                    )
                )
            else:
                levels = tuple(
                    sorted(quote.no_bid_depth, key=lambda item: item.price, reverse=True)
                )
        return levels, bid, ask

    def simulate(
        self,
        *,
        order_id: str,
        decision: StrategyDecision,
        quote: PredictionMarketQuote,
        submit_timestamp: datetime,
    ) -> PaperExecutionResult:
        assert decision.outcome is not None and decision.limit_price is not None
        action = (
            ExecutionAction.BUY
            if decision.decision
            in {PaperDecisionType.BUY_YES, PaperDecisionType.BUY_NO, PaperDecisionType.ADD}
            else ExecutionAction.SELL
        )
        levels, bid, ask = self._book(quote, decision.outcome, action)
        top = ask if action is ExecutionAction.BUY else bid
        spread = None if bid is None or ask is None else ask - bid
        if top is None or not levels or levels[0].price != top:
            return PaperExecutionResult(
                order_id=order_id,
                decision=decision,
                submit_timestamp=submit_timestamp,
                quote_timestamp=quote.source_timestamp,
                state=ExecutionOrderState.REJECTED,
                reason=PaperExecutionReason.PRICE_MOVED,
                requested_quantity=decision.quantity,
                filled_quantity=Decimal(0),
                requested_price=decision.limit_price,
                average_fill_price=None,
                spread=spread,
                slippage=None,
                fees=Decimal(0),
                fills=(),
                risk=None,
                venue_ticker=quote.venue_ticker,
                quote_source=quote.source,
            )
        eligible = tuple(
            level
            for level in levels
            if (
                level.price <= decision.limit_price
                if action is ExecutionAction.BUY
                else level.price >= decision.limit_price
            )
        )
        available = sum((level.quantity for level in eligible), Decimal(0))
        if decision.time_in_force is TimeInForce.FILL_OR_KILL and available < decision.quantity:
            eligible = ()
        remaining = decision.quantity
        fills: list[SimulatedFill] = []
        try:
            for index, level in enumerate(eligible):
                if remaining <= 0:
                    break
                quantity = min(remaining, level.quantity)
                if quantity <= 0:
                    continue
                fee = self._fees.compute(
                    order_id=order_id, quantity=quantity, price=level.price, action=action
                )
                slippage = level.price - top if action is ExecutionAction.BUY else top - level.price
                fills.append(
                    SimulatedFill(
                        fill_id=f"{order_id}:{index}",
                        order_id=order_id,
                        asset=decision.asset,
                        event_id=decision.event_id,
                        contract_id=decision.contract_id,
                        fill_timestamp=submit_timestamp,
                        outcome=decision.outcome,
                        action=action,
                        quantity=quantity,
                        price=level.price,
                        fee=fee,
                        spread=spread or Decimal(0),
                        slippage=slippage,
                    )
                )
                remaining -= quantity
        finally:
            # This simulator evaluates one observed book snapshot atomically. It never carries
            # an exchange order forward, so its per-order fee accumulator must not leak.
            self._fees.finish_order(order_id)
        filled = decision.quantity - remaining
        if filled == decision.quantity:
            state, reason = ExecutionOrderState.FILLED, PaperExecutionReason.FULL_FILL
        elif filled > 0:
            state = (
                ExecutionOrderState.PARTIALLY_FILLED
                if decision.time_in_force is TimeInForce.GOOD_TILL_CANCELED
                else ExecutionOrderState.CANCELLED
            )
            reason = PaperExecutionReason.PARTIAL_FILL
        else:
            state, reason = (
                (ExecutionOrderState.OPEN, PaperExecutionReason.NO_FILL)
                if decision.time_in_force is TimeInForce.GOOD_TILL_CANCELED
                else (ExecutionOrderState.CANCELLED, PaperExecutionReason.NO_FILL)
            )
        total_value = sum((item.price * item.quantity for item in fills), Decimal(0))
        average = None if filled == 0 else total_value / filled
        fees = sum((item.fee.net_fee for item in fills), Decimal(0))
        slippage = (
            None
            if filled == 0
            else sum((item.slippage * item.quantity for item in fills), Decimal(0)) / filled
        )
        return PaperExecutionResult(
            order_id=order_id,
            decision=decision,
            submit_timestamp=submit_timestamp,
            quote_timestamp=quote.source_timestamp,
            state=state,
            reason=reason,
            requested_quantity=decision.quantity,
            filled_quantity=filled,
            requested_price=decision.limit_price,
            average_fill_price=average,
            spread=spread,
            slippage=slippage,
            fees=fees,
            fills=tuple(fills),
            risk=None,
            venue_ticker=quote.venue_ticker,
            quote_source=quote.source,
        )


class DeterministicDummyStrategy:
    """State-machine signals for repeatable paper plumbing tests; not a predictive model."""

    def __init__(self) -> None:
        self._step: dict[str, int] = {}

    def set_step(self, event_id: str, step: int) -> None:
        if step < 0:
            raise ValueError("strategy step must be non-negative")
        self._step[event_id] = step

    def forget(self, event_id: str) -> None:
        self._step.pop(event_id, None)

    def decide(self, quote: PredictionMarketQuote, now: datetime) -> StrategyDecision:
        step = self._step.get(quote.robinhood_event_id, 0)
        self._step[quote.robinhood_event_id] = step + 1
        sequence = (
            PaperDecisionType.BUY_YES,
            PaperDecisionType.HOLD,
            PaperDecisionType.ADD,
            PaperDecisionType.REDUCE,
            PaperDecisionType.CLOSE,
        )
        decision = sequence[min(step, len(sequence) - 1)]
        outcome = None if decision is PaperDecisionType.HOLD else ContractOutcome.YES
        quantity = Decimal(0) if decision is PaperDecisionType.HOLD else Decimal("0.25")
        limit = None
        if outcome is not None:
            limit = (
                quote.yes_ask
                if decision in {PaperDecisionType.BUY_YES, PaperDecisionType.ADD}
                else quote.yes_bid
            )
        digest = hashlib.sha256(f"{quote.robinhood_event_id}:{step}".encode()).hexdigest()[:16]
        return StrategyDecision(
            decision_id=f"dummy-{digest}",
            signal_timestamp=now,
            asset=quote.asset,
            event_id=quote.robinhood_event_id,
            contract_id=quote.robinhood_contract_id,
            decision=decision,
            outcome=outcome,
            quantity=quantity,
            limit_price=limit,
        )


def utc_now() -> datetime:
    return datetime.now(UTC)
