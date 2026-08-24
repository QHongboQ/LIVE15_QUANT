"""Strictly local Shadow execution boundary and deterministic exit evaluation.

The module deliberately keeps remote execution frozen.  ``ShadowExecutor`` is a
thin, typed facade over the existing append-only paper ledger, whereas
``KalshiRemoteExecutor`` is read-only unless a future, separately approved
activation removes the explicit provider blocker.  No class in this module can
fall back from Shadow to Demo or Production.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from live15_quant.demo_execution import DemoSynchronizedQuote
from live15_quant.execution import (
    ContractOutcome,
    ExecutionAccountState,
    ExecutionAction,
    ExecutionCapabilities,
    ExecutionMode,
    ExecutionOrderRequest,
    ExecutionOrderStatus,
    ExecutionPosition,
)
from live15_quant.fees import KalshiTakerFeeModel
from live15_quant.models import (
    ExecutabilityClassification,
    FreshnessState,
    MappingConfidence,
    OrderBookLevel,
    PredictionMarketQuote,
    SourceTimestampKind,
    Venue,
)
from live15_quant.paper import PaperExecutionResult, StrategyDecision
from live15_quant.paper_execution import KalshiPaperExecutionProvider
from live15_quant.paper_storage import PaperFillRecord, PaperOrderRecord, PaperStore

DEMO_REAL_WRITE_FROZEN_PROVIDER_BLOCKER = "DEMO_REAL_WRITE_FROZEN_PROVIDER_BLOCKER"


class ShadowEnvironment(StrEnum):
    """Every backend fact has an explicit, non-interchangeable environment."""

    REAL_REMOTE = "REAL_REMOTE"
    SHADOW_SIMULATED = "SHADOW_SIMULATED"


class ShadowExitAction(StrEnum):
    """Mutually exclusive deterministic exit-candidate outcomes."""

    NO_ACTION = "no_action"
    TAKE_PROFIT = "take_profit"
    CUT_LOSS = "cut_loss"
    EDGE_REVERSAL = "edge_reversal"
    HOLD_TO_SETTLEMENT = "hold_to_settlement"
    DATA_UNAVAILABLE = "data_unavailable"


class ShadowExecutionError(RuntimeError):
    """A local Shadow interface or provenance invariant failed."""


class RemoteExecutionFrozenError(ShadowExecutionError):
    """The documented Demo write blocker is intentionally fail-closed."""


class ExecutionInterface(Protocol):
    """The shared execution surface for Shadow and a future remote backend."""

    environment: ShadowEnvironment

    def submit_order(self, request: ExecutionOrderRequest) -> ExecutionOrderStatus: ...

    def get_orders(self) -> tuple[object, ...]: ...

    def get_fills(self) -> tuple[object, ...]: ...

    def get_positions(self) -> tuple[ExecutionPosition, ...]: ...

    def get_balance(self) -> ExecutionAccountState: ...


class RemoteReadInterface(Protocol):
    """Read-only remote facts; deliberately separate from a write client."""

    def get_orders(self) -> tuple[object, ...]: ...

    def get_fills(self) -> tuple[object, ...]: ...

    def get_positions(self) -> tuple[ExecutionPosition, ...]: ...

    def get_balance(self) -> ExecutionAccountState: ...


class KalshiRemoteExecutor:
    """Future remote backend, frozen while the Demo provider rejects writes.

    It exposes only supplied remote read truth.  A submit attempt always fails
    before a network call, so selecting this backend cannot accidentally issue
    a Demo or Production order.
    """

    environment = ShadowEnvironment.REAL_REMOTE
    write_state = DEMO_REAL_WRITE_FROZEN_PROVIDER_BLOCKER

    def __init__(self, reader: RemoteReadInterface) -> None:
        self._reader = reader

    def submit_order(self, request: ExecutionOrderRequest) -> ExecutionOrderStatus:
        del request
        raise RemoteExecutionFrozenError(DEMO_REAL_WRITE_FROZEN_PROVIDER_BLOCKER)

    def get_orders(self) -> tuple[object, ...]:
        return self._reader.get_orders()

    def get_fills(self) -> tuple[object, ...]:
        return self._reader.get_fills()

    def get_positions(self) -> tuple[ExecutionPosition, ...]:
        return self._reader.get_positions()

    def get_balance(self) -> ExecutionAccountState:
        return self._reader.get_balance()


class ShadowExecutor:
    """Append-only simulated executor backed by one isolated ``PaperStore``."""

    environment = ShadowEnvironment.SHADOW_SIMULATED

    def __init__(self, provider: KalshiPaperExecutionProvider, store: PaperStore) -> None:
        self._provider = provider
        self._store = store

    @property
    def capabilities(self) -> ExecutionCapabilities:
        base = self._provider.capabilities
        return ExecutionCapabilities(
            base.event_contract_discovery,
            base.event_contract_quotes,
            base.event_contract_positions,
            base.event_contract_orders,
            base.event_contract_cancellation,
            ExecutionMode.PAPER,
        )

    @property
    def portfolio(self):  # type: ignore[no-untyped-def]
        return self._provider.portfolio

    def update_quote(self, quote: PredictionMarketQuote) -> None:
        self._provider.update_quote(quote)

    def execute(
        self, decision: StrategyDecision, quote: PredictionMarketQuote | None
    ) -> PaperExecutionResult:
        return self._provider.execute(decision, quote)

    def submit_order(self, request: ExecutionOrderRequest) -> ExecutionOrderStatus:
        return self._provider.submit_order(request)

    def get_orders(self) -> tuple[PaperOrderRecord, ...]:
        return tuple(self._store.replay_orders())

    def get_fills(self) -> tuple[PaperFillRecord, ...]:
        return tuple(self._store.replay_fills())

    def get_positions(self) -> tuple[ExecutionPosition, ...]:
        now = datetime.now(UTC)
        state = self._provider.portfolio.state({}, now)
        return tuple(
            ExecutionPosition(
                asset=item.asset,
                event_id=item.event_id,
                contract_id=item.contract_id,
                outcome=item.outcome,
                quantity=item.quantity,
                average_price=item.average_cost,
                received_timestamp=now,
            )
            for item in state.positions
        )

    def get_balance(self) -> ExecutionAccountState:
        return self._provider.get_account_state(self._provider.portfolio.account_id)

    def get_position(
        self, event_id: str, contract_id: str, outcome: ContractOutcome | None = None
    ) -> ExecutionPosition | None:
        return self._provider.get_position(event_id, contract_id, outcome)

    def settle_event(
        self, *, event_id: str, outcome_yes: bool, settlement_timestamp: datetime
    ) -> bool:
        return self._provider.settle_event(
            event_id=event_id, outcome_yes=outcome_yes, settlement_timestamp=settlement_timestamp
        )

    def settlement_record(self, event_id: str):  # type: ignore[no-untyped-def]
        return self._provider.settlement_record(event_id)

    def close(self) -> None:
        """Close only this simulated portfolio's local ledger connection."""

        self._store.close()


@dataclass(frozen=True, slots=True)
class ShadowExitPolicy:
    """Fixed, deliberately small dynamic-exit candidate policy.

    This is an execution-policy diagnostic, not a model parameter.  It never
    changes a model probability, entry threshold, or hard-risk limit.
    """

    take_profit: Decimal = Decimal("0.05")
    stop_loss: Decimal = Decimal("0.05")
    reversal_margin: Decimal = Decimal("0.00")
    hold_to_settlement_within: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        if self.hold_to_settlement_within <= timedelta(0):
            raise ValueError("exit hold-to-settlement threshold must be positive")
        if any(
            not value.is_finite() or value < 0
            for value in (self.take_profit, self.stop_loss, self.reversal_margin)
        ):
            raise ValueError("exit policy values must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ShadowExitEvaluation:
    """A fully reproducible candidate exit decision, never a remote fact."""

    action: ShadowExitAction
    current_executable_bid: Decimal | None
    close_now_ev: Decimal | None
    hold_ev: Decimal | None
    mark_change: Decimal | None
    reason: str


def evaluate_shadow_exit(
    *,
    entry_price: Decimal,
    outcome: ContractOutcome,
    quote: PredictionMarketQuote | None,
    fair_probability: Decimal | None,
    now: datetime,
    window_end: datetime,
    policy: ShadowExitPolicy | None = None,
) -> ShadowExitEvaluation:
    """Compare executable close value with hold value using only current inputs.

    The entry price is sunk for the EV comparison but retained as the explicit
    take-profit/stop-loss mark.  No midpoint, stale quote, synthetic fill, or
    future settlement is used.
    """

    active_policy = policy or ShadowExitPolicy()
    if (
        quote is None
        or fair_probability is None
        or quote.freshness is not FreshnessState.FRESH
        or quote.executability is not ExecutabilityClassification.OFFICIAL_VENUE_ORDER_BOOK
        or quote.received_timestamp > now
        or now.tzinfo is None
        or now.utcoffset() is None
        or window_end.tzinfo is None
        or window_end.utcoffset() is None
    ):
        return ShadowExitEvaluation(
            ShadowExitAction.DATA_UNAVAILABLE,
            None,
            None,
            None,
            None,
            "quote_or_probability_unavailable",
        )
    bid = quote.yes_bid if outcome is ContractOutcome.YES else quote.no_bid
    if bid is None:
        return ShadowExitEvaluation(
            ShadowExitAction.DATA_UNAVAILABLE, None, None, None, None, "no_executable_exit_bid"
        )
    if not Decimal(0) <= fair_probability <= Decimal(1):
        return ShadowExitEvaluation(
            ShadowExitAction.DATA_UNAVAILABLE, bid, None, None, None, "invalid_fair_probability"
        )
    hold_probability = (
        fair_probability if outcome is ContractOutcome.YES else Decimal(1) - fair_probability
    )
    # A dynamic close is a taker sell at the executable bid.  Reserve its
    # documented fee before comparing close-now against continued exposure;
    # the actual simulator independently recomputes and persists this fee if a
    # future policy elects to execute the candidate.
    exit_fee_model = KalshiTakerFeeModel()
    exit_fee = exit_fee_model.compute(
        order_id="shadow-exit-evaluation",
        quantity=Decimal(1),
        price=bid,
        action=ExecutionAction.SELL,
    ).net_fee
    exit_fee_model.finish_order("shadow-exit-evaluation")
    close_now_ev = bid - exit_fee
    mark_change = close_now_ev - entry_price
    if now.astimezone(UTC) >= window_end.astimezone(UTC) - active_policy.hold_to_settlement_within:
        action, reason = ShadowExitAction.HOLD_TO_SETTLEMENT, "near_expiry"
    elif mark_change >= active_policy.take_profit:
        action, reason = ShadowExitAction.TAKE_PROFIT, "executable_take_profit"
    elif mark_change <= -active_policy.stop_loss:
        action, reason = ShadowExitAction.CUT_LOSS, "executable_stop_loss"
    elif hold_probability <= close_now_ev - active_policy.reversal_margin:
        action, reason = ShadowExitAction.EDGE_REVERSAL, "hold_ev_not_above_executable_close"
    else:
        action, reason = ShadowExitAction.NO_ACTION, "continue"
    return ShadowExitEvaluation(action, bid, close_now_ev, hold_probability, mark_change, reason)


def live_ws_prediction_quote(
    *,
    asset,
    ticker: str,
    series: str,
    quote: DemoSynchronizedQuote,
    now: datetime,
    max_quote_age: timedelta,
) -> PredictionMarketQuote | None:
    """Convert the atomic Recorder projection into a fillable official quote.

    The function has no SQLite fallback.  A missing, stale, malformed, or wrong
    ticker live projection is simply unavailable and must fail closed.
    """

    if (
        quote.source != "LIVE_KALSHI_WS"
        or not quote.synchronized
        or quote.ticker != ticker
        or quote.book_received_timestamp is None
        or quote.book_received_timestamp > now
        or now - quote.book_received_timestamp > max_quote_age
        or not quote.yes_bid_depth
        or not quote.no_bid_depth
    ):
        return None
    yes_depth = tuple(OrderBookLevel(price, quantity) for price, quantity in quote.yes_bid_depth)
    no_depth = tuple(OrderBookLevel(price, quantity) for price, quantity in quote.no_bid_depth)
    return PredictionMarketQuote(
        asset=asset,
        robinhood_event_id=ticker,
        robinhood_contract_id=ticker,
        venue=Venue.KALSHI,
        venue_series=series,
        venue_ticker=ticker,
        mapping_confidence=MappingConfidence.VERIFIED,
        source_timestamp=quote.book_received_timestamp,
        source_timestamp_kind=SourceTimestampKind.EXCHANGE_EVENT_TIME,
        received_timestamp=quote.book_received_timestamp,
        yes_bid=quote.yes_bid,
        yes_ask=quote.yes_ask,
        no_bid=quote.no_bid,
        no_ask=quote.no_ask,
        last_trade=None,
        volume=None,
        yes_bid_depth=yes_depth,
        no_bid_depth=no_depth,
        source="kalshi_ws_live_projection",
        freshness=FreshnessState.FRESH,
        executability=ExecutabilityClassification.OFFICIAL_VENUE_ORDER_BOOK,
        evidence_urls=(),
    )
