"""Local-only Kalshi paper adapter; it has no authenticated network write path."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from live15_quant.execution import (
    ContractOutcome,
    ExecutionAccountState,
    ExecutionAction,
    ExecutionCapabilities,
    ExecutionMode,
    ExecutionOrderRequest,
    ExecutionOrderState,
    ExecutionOrderStatus,
    ExecutionPosition,
)
from live15_quant.fees import FeeComputation, KalshiTakerFeeModel
from live15_quant.models import (
    ExecutabilityClassification,
    MappingConfidence,
    PredictionMarketQuote,
    Venue,
)
from live15_quant.paper import (
    KalshiOrderBookFillSimulator,
    PaperDecisionType,
    PaperExecutionReason,
    PaperExecutionResult,
    PaperPortfolio,
    PaperPositionStatus,
    SimulatedFill,
    StrategyDecision,
)
from live15_quant.paper_storage import (
    PaperFillRecord,
    PaperOrderRecord,
    PaperPortfolioRecord,
    PaperSettlementRecord,
    PaperStore,
)
from live15_quant.risk import (
    ImmutableHardRiskLayer,
    RiskBlockReason,
    RiskDecision,
    RiskSnapshot,
)


class PaperExecutionError(RuntimeError):
    pass


class KalshiPaperExecutionProvider:
    """Project official venue quotes into a deterministic local paper ledger."""

    def __init__(
        self,
        *,
        store: PaperStore,
        account_id: str,
        starting_cash: Decimal,
        risk: ImmutableHardRiskLayer,
        simulator: KalshiOrderBookFillSimulator | None = None,
        kill_switch: bool = False,
    ) -> None:
        self._store = store
        self._risk = risk
        self._portfolio = PaperPortfolio(account_id, starting_cash)
        self._simulator = simulator or KalshiOrderBookFillSimulator()
        self._kill_switch = kill_switch
        self._quotes: dict[tuple[str, str], PredictionMarketQuote] = {}
        replay_group: list[SimulatedFill] = []
        replay_order_id: str | None = None
        for record in store.replay_fills():
            fill = self._fill_from_record(record)
            if replay_order_id is not None and fill.order_id != replay_order_id:
                self._apply_fill_group(replay_group)
                replay_group = []
            replay_order_id = fill.order_id
            replay_group.append(fill)
        self._apply_fill_group(replay_group)
        for event_id in store.settled_event_ids():
            settlement = store.settlement_for_event(event_id)
            assert settlement is not None
            self._portfolio.settle_event(
                event_id,
                outcome_yes=settlement.outcome_yes,
                settlement_timestamp=settlement.settlement_timestamp,
            )
        for event_id in store.pending_event_ids():
            self._portfolio.mark_pending_settlement(event_id)

    def _apply_fill_group(self, fills: list[SimulatedFill]) -> None:
        if not fills:
            return
        realized_before = self._portfolio.realized_pnl
        for fill in fills:
            self._portfolio.apply_fill(fill)
        if any(fill.action is ExecutionAction.SELL for fill in fills):
            self._portfolio.record_exit_order_pnl(self._portfolio.realized_pnl - realized_before)

    @staticmethod
    def _fill_from_record(record: PaperFillRecord) -> SimulatedFill:
        fee = FeeComputation(
            trade_fee=record.trade_fee,
            rounding_fee=record.rounding_fee,
            rebate=record.rebate,
            net_fee=record.net_fee,
            assumption=record.fee_assumption,
        )
        return SimulatedFill(
            fill_id=record.fill_id,
            order_id=record.order_id,
            asset=record.asset,
            event_id=record.event_id,
            contract_id=record.contract_id,
            fill_timestamp=record.fill_timestamp,
            outcome=record.outcome,
            action=record.action,
            quantity=record.quantity,
            price=record.price,
            fee=fee,
            spread=record.spread,
            slippage=record.slippage,
        )

    @property
    def capabilities(self) -> ExecutionCapabilities:
        return ExecutionCapabilities(True, True, True, True, True, ExecutionMode.PAPER)

    @property
    def portfolio(self) -> PaperPortfolio:
        return self._portfolio

    def update_quote(self, quote: PredictionMarketQuote) -> None:
        self._quotes[(quote.robinhood_event_id, quote.robinhood_contract_id)] = quote

    def _marks(self) -> dict[tuple[str, ContractOutcome], Decimal]:
        marks: dict[tuple[str, ContractOutcome], Decimal] = {}
        for (event_id, _), quote in self._quotes.items():
            if quote.yes_bid is not None:
                marks[(event_id, ContractOutcome.YES)] = quote.yes_bid
            if quote.no_bid is not None:
                marks[(event_id, ContractOutcome.NO)] = quote.no_bid
        return marks

    def get_account_state(self, account_id: str) -> ExecutionAccountState:
        if account_id != self._portfolio.account_id:
            raise PaperExecutionError("unknown paper account")
        now = datetime.now(UTC)
        state = self._portfolio.state(self._marks(), now)
        return ExecutionAccountState(
            account_id=account_id,
            buying_power=state.cash,
            total_exposure=state.total_exposure,
            daily_realized_pnl=state.daily_realized_pnl,
            currency="USD",
            received_timestamp=now,
        )

    def get_position(
        self,
        event_id: str,
        contract_id: str,
        outcome: ContractOutcome | None = None,
    ) -> ExecutionPosition | None:
        now = datetime.now(UTC)
        matches: list[ExecutionPosition] = []
        outcomes = tuple(ContractOutcome) if outcome is None else (outcome,)
        for candidate in outcomes:
            position = self._portfolio.position(event_id, candidate)
            if position is not None and position.contract_id == contract_id:
                matches.append(
                    ExecutionPosition(
                        asset=position.asset,
                        event_id=event_id,
                        contract_id=contract_id,
                        outcome=candidate,
                        quantity=position.quantity,
                        average_price=position.average_cost,
                        received_timestamp=now,
                    )
                )
        if len(matches) > 1:
            raise PaperExecutionError("position outcome is ambiguous")
        return matches[0] if matches else None

    @staticmethod
    def _status(record: PaperOrderRecord) -> ExecutionOrderStatus:
        return ExecutionOrderStatus(
            provider_order_id=record.order_id,
            client_order_id=record.decision_id,
            state=record.state,
            requested_quantity=record.requested_quantity,
            filled_quantity=record.filled_quantity,
            average_fill_price=record.average_fill_price,
            received_timestamp=datetime.now(UTC),
        )

    def get_order_status(self, account_id: str, provider_order_id: str) -> ExecutionOrderStatus:
        if account_id != self._portfolio.account_id:
            raise PaperExecutionError("unknown paper account")
        record = self._store.latest_order(provider_order_id)
        if record is None:
            raise PaperExecutionError("unknown paper order")
        return self._status(record)

    def cancel_order(self, account_id: str, provider_order_id: str) -> ExecutionOrderStatus:
        if account_id != self._portfolio.account_id:
            raise PaperExecutionError("unknown paper account")
        record = self._store.latest_order(provider_order_id)
        if record is None:
            raise PaperExecutionError("unknown paper order")
        if record.state not in {ExecutionOrderState.OPEN, ExecutionOrderState.PARTIALLY_FILLED}:
            return self._status(record)
        self._store.append_order_event(
            order_id=provider_order_id,
            timestamp=datetime.now(UTC),
            state=ExecutionOrderState.CANCELLED,
            reason=PaperExecutionReason.CANCELLED,
            filled_quantity=record.filled_quantity,
            average_fill_price=record.average_fill_price,
            fees=record.fees,
        )
        return self.get_order_status(account_id, provider_order_id)

    def expire_event(self, event_id: str) -> None:
        """Drop transient quote/strategy state and preserve positions as pending settlement."""

        self._portfolio.mark_pending_settlement(event_id)
        self._quotes = {key: quote for key, quote in self._quotes.items() if key[0] != event_id}

    def settle_event(
        self, *, event_id: str, outcome_yes: bool, settlement_timestamp: datetime
    ) -> bool:
        """Apply Kalshi finalized truth once and persist the resulting local accounting."""

        if settlement_timestamp.tzinfo is None or settlement_timestamp.utcoffset() is None:
            raise PaperExecutionError("paper settlement timestamp must be timezone-aware")
        settlement_timestamp = settlement_timestamp.astimezone(UTC)
        existing = self._store.settlement_for_event(event_id)
        if existing is not None:
            if (
                existing.outcome_yes != outcome_yes
                or existing.settlement_timestamp != settlement_timestamp.astimezone(UTC)
            ):
                raise PaperExecutionError("paper settlement conflicts with official truth")
            return False
        settlement = self._portfolio.settle_event(
            event_id,
            outcome_yes=outcome_yes,
            settlement_timestamp=settlement_timestamp,
        )
        inserted = self._store.append_settlement(settlement)
        self._store.append_portfolio(self._portfolio.state(self._marks(), settlement_timestamp))
        return inserted

    def settlement_record(self, event_id: str) -> PaperSettlementRecord | None:
        """Bounded local-ledger read used by forward reconciliation only."""

        return self._store.settlement_for_event(event_id)

    def _existing_result(self, decision: StrategyDecision) -> PaperExecutionResult | None:
        record = self._store.order_for_decision(decision.decision_id)
        if record is None:
            return None
        return PaperExecutionResult(
            order_id=record.order_id,
            decision=decision,
            submit_timestamp=record.submit_timestamp,
            quote_timestamp=record.quote_timestamp,
            state=record.state,
            reason=record.reason,
            requested_quantity=record.requested_quantity,
            filled_quantity=record.filled_quantity,
            requested_price=record.requested_price,
            average_fill_price=record.average_fill_price,
            spread=None,
            slippage=None,
            fees=record.fees,
            fills=(),
            risk=None,
            venue_ticker=record.venue_ticker,
            quote_source=record.quote_source,
        )

    def execute(
        self,
        decision: StrategyDecision,
        quote: PredictionMarketQuote | None,
        *,
        source_healthy: bool = True,
        fill_state_certain: bool = True,
    ) -> PaperExecutionResult:
        if self._store.decision_exists(decision.decision_id) and not self._store.decision_matches(
            decision
        ):
            raise PaperExecutionError("decision ID conflicts with persisted immutable intent")
        existing = self._existing_result(decision)
        if existing is not None:
            return existing
        now = datetime.now(UTC)
        if self._store.decision_exists(decision.decision_id):
            if decision.decision is PaperDecisionType.HOLD:
                return PaperExecutionResult(
                    None,
                    decision,
                    now,
                    None,
                    ExecutionOrderState.NOT_SUBMITTED,
                    PaperExecutionReason.HOLD,
                    Decimal(0),
                    Decimal(0),
                    None,
                    None,
                    None,
                    None,
                    Decimal(0),
                    (),
                    None,
                )
            if decision.decision is PaperDecisionType.CANCEL:
                assert decision.target_order_id is not None
                status = self.cancel_order(self._portfolio.account_id, decision.target_order_id)
                return PaperExecutionResult(
                    status.provider_order_id,
                    decision,
                    now,
                    None,
                    status.state,
                    PaperExecutionReason.CANCELLED,
                    Decimal(0),
                    Decimal(0),
                    None,
                    None,
                    None,
                    None,
                    Decimal(0),
                    (),
                    None,
                )
            raise PaperExecutionError("decision exists without a recoverable order")
        if decision.decision is PaperDecisionType.HOLD:
            self._store.append_decision(decision)
            return PaperExecutionResult(
                None,
                decision,
                now,
                None,
                ExecutionOrderState.NOT_SUBMITTED,
                PaperExecutionReason.HOLD,
                Decimal(0),
                Decimal(0),
                None,
                None,
                None,
                None,
                Decimal(0),
                (),
                None,
            )
        if decision.decision is PaperDecisionType.CANCEL:
            self._store.append_decision(decision)
            assert decision.target_order_id is not None
            status = self.cancel_order(self._portfolio.account_id, decision.target_order_id)
            return PaperExecutionResult(
                status.provider_order_id,
                decision,
                now,
                None,
                status.state,
                PaperExecutionReason.CANCELLED,
                Decimal(0),
                Decimal(0),
                None,
                None,
                None,
                None,
                Decimal(0),
                (),
                None,
            )
        assert decision.outcome is not None and decision.limit_price is not None
        order_id = "paper-" + hashlib.sha256(decision.decision_id.encode()).hexdigest()[:24]
        if quote is None:
            risk = RiskDecision(
                allowed=False,
                reasons=(
                    RiskBlockReason.DATA_SOURCE_UNHEALTHY,
                    RiskBlockReason.MISSING_BID_ASK,
                ),
            )
            result = PaperExecutionResult(
                order_id,
                decision,
                now,
                None,
                ExecutionOrderState.REJECTED,
                PaperExecutionReason.RISK_BLOCKED,
                decision.quantity,
                Decimal(0),
                decision.limit_price,
                None,
                None,
                None,
                Decimal(0),
                (),
                risk,
            )
            self._store.append_execution(result, risk)
            self._store.append_portfolio(self._portfolio.state(self._marks(), now))
            return result
        if (
            quote.asset is not decision.asset
            or quote.robinhood_event_id != decision.event_id
            or quote.robinhood_contract_id != decision.contract_id
            or quote.venue is not Venue.KALSHI
            or quote.executability is not ExecutabilityClassification.OFFICIAL_VENUE_ORDER_BOOK
        ):
            raise PaperExecutionError("quote instrument does not match the paper decision")
        self.update_quote(quote)
        action = (
            ExecutionAction.BUY
            if decision.decision
            in {PaperDecisionType.BUY_YES, PaperDecisionType.BUY_NO, PaperDecisionType.ADD}
            else ExecutionAction.SELL
        )
        current = self._portfolio.position(decision.event_id, decision.outcome)
        if current is not None and current.status is PaperPositionStatus.PENDING_SETTLEMENT:
            raise PaperExecutionError("pending-settlement positions cannot be traded")
        if decision.decision is PaperDecisionType.ADD and current is None:
            raise PaperExecutionError("paper add requires an open position")
        quantity = (
            current.quantity
            if decision.decision is PaperDecisionType.CLOSE and current
            else decision.quantity
        )
        if action is ExecutionAction.SELL and (current is None or quantity > current.quantity):
            raise PaperExecutionError("paper reduce/close exceeds position")
        effective = replace(decision, quantity=quantity)
        request = ExecutionOrderRequest(
            self._portfolio.account_id,
            decision.asset,
            decision.event_id,
            decision.contract_id,
            decision.outcome,
            action,
            quantity,
            decision.limit_price,
            order_id,
        )
        account = self.get_account_state(self._portfolio.account_id)
        state = self._portfolio.state(self._marks(), now)
        event_exposure = sum(
            (
                item.average_cost * item.quantity
                for item in state.positions
                if item.event_id == decision.event_id
            ),
            Decimal(0),
        )
        side_present = (
            quote.yes_ask
            if action is ExecutionAction.BUY and decision.outcome is ContractOutcome.YES
            else quote.no_ask
            if action is ExecutionAction.BUY
            else quote.yes_bid
            if decision.outcome is ContractOutcome.YES
            else quote.no_bid
        ) is not None
        risk_snapshot = RiskSnapshot(
            quote_freshness=quote.freshness,
            data_sources_healthy=source_healthy,
            fill_state_certain=fill_state_certain,
            event_exposure=event_exposure,
            consecutive_losses=state.consecutive_losses,
            total_exposure=state.total_exposure,
            daily_pnl=state.daily_pnl,
            quote_fields_present=side_present,
            mapping_verified=quote.mapping_confidence is MappingConfidence.VERIFIED,
            kill_switch_active=self._kill_switch,
            estimated_fees=(
                KalshiTakerFeeModel.conservative_reserve(
                    quantity,
                    max(len(quote.yes_bid_depth), len(quote.no_bid_depth), 1),
                )
                if action is ExecutionAction.BUY
                else Decimal(0)
            ),
        )
        position = self.get_position(decision.event_id, decision.contract_id, decision.outcome)
        risk = self._risk.evaluate(request, account, position, risk_snapshot)
        if not risk.allowed:
            result = PaperExecutionResult(
                order_id,
                effective,
                now,
                quote.source_timestamp,
                ExecutionOrderState.REJECTED,
                PaperExecutionReason.RISK_BLOCKED,
                quantity,
                Decimal(0),
                decision.limit_price,
                None,
                None,
                None,
                Decimal(0),
                (),
                risk,
                quote.venue_ticker,
                quote.source,
            )
        else:
            result = replace(
                self._simulator.simulate(
                    order_id=order_id, decision=effective, quote=quote, submit_timestamp=now
                ),
                risk=risk,
            )
        inserted = set(self._store.append_execution(result, risk))
        self._apply_fill_group([fill for fill in result.fills if fill.fill_id in inserted])
        self._store.append_portfolio(self._portfolio.state(self._marks(), now))
        return result

    def submit_order(self, request: ExecutionOrderRequest) -> ExecutionOrderStatus:
        quote = self._quotes.get((request.event_id, request.contract_id))
        decision = StrategyDecision(
            decision_id=request.client_order_id,
            signal_timestamp=datetime.now(UTC),
            asset=request.asset,
            event_id=request.event_id,
            contract_id=request.contract_id,
            decision=(
                PaperDecisionType.BUY_YES
                if request.action is ExecutionAction.BUY and request.outcome is ContractOutcome.YES
                else PaperDecisionType.BUY_NO
                if request.action is ExecutionAction.BUY
                else PaperDecisionType.REDUCE
            ),
            outcome=request.outcome,
            quantity=request.quantity,
            limit_price=request.limit_price,
        )
        result = self.execute(decision, quote)
        assert result.order_id is not None
        return self.get_order_status(request.account_id, result.order_id)

    def close_or_reduce_position(self, request: ExecutionOrderRequest) -> ExecutionOrderStatus:
        if request.action is not ExecutionAction.SELL:
            raise PaperExecutionError("close/reduce requires a sell action")
        return self.submit_order(request)


class PaperReplayReader:
    """Deterministic row-id tie-broken reader for paper orders and fills."""

    def __init__(self, store: PaperStore) -> None:
        self._store = store

    def orders(self) -> tuple[PaperOrderRecord, ...]:
        return tuple(self._store.replay_orders())

    def fills(self) -> tuple[PaperFillRecord, ...]:
        return tuple(self._store.replay_fills())

    def portfolios(self) -> tuple[PaperPortfolioRecord, ...]:
        return tuple(self._store.replay_portfolios())
