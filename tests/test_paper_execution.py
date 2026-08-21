from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from live15_quant.execution import ContractOutcome, ExecutionAction, ExecutionOrderState
from live15_quant.fees import KalshiTakerFeeModel
from live15_quant.models import (
    Asset,
    ExecutabilityClassification,
    FreshnessState,
    MappingConfidence,
    OrderBookLevel,
    PredictionMarketQuote,
    SourceTimestampKind,
    Venue,
)
from live15_quant.paper import (
    KalshiOrderBookFillSimulator,
    PaperDecisionType,
    PaperExecutionReason,
    PaperPortfolio,
    PaperPositionStatus,
    StrategyDecision,
    TimeInForce,
)
from live15_quant.paper_execution import (
    KalshiPaperExecutionProvider,
    PaperExecutionError,
    PaperReplayReader,
)
from live15_quant.paper_storage import PaperStorageError, PaperStore
from live15_quant.risk import HardRiskLimits, ImmutableHardRiskLayer, RiskBlockReason
from live15_quant.storage import RecorderStorageError, RecorderStore

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def quote(*, freshness: FreshnessState = FreshnessState.FRESH) -> PredictionMarketQuote:
    return PredictionMarketQuote(
        asset=Asset.BTC,
        robinhood_event_id="event-1",
        robinhood_contract_id="contract-1",
        venue=Venue.KALSHI,
        venue_series="KXBTC15M",
        venue_ticker="KXBTC15M-TEST",
        mapping_confidence=MappingConfidence.VERIFIED,
        source_timestamp=NOW,
        source_timestamp_kind=SourceTimestampKind.HTTP_RESPONSE_DATE,
        received_timestamp=NOW + timedelta(milliseconds=20),
        yes_bid=Decimal("0.5100"),
        yes_ask=Decimal("0.5200"),
        no_bid=Decimal("0.4800"),
        no_ask=Decimal("0.4900"),
        last_trade=Decimal("0.5150"),
        volume=Decimal("100.000"),
        yes_bid_depth=(
            OrderBookLevel(Decimal("0.5100"), Decimal("2.5")),
            OrderBookLevel(Decimal("0.5000"), Decimal("10")),
        ),
        no_bid_depth=(
            OrderBookLevel(Decimal("0.4800"), Decimal("1.25")),
            OrderBookLevel(Decimal("0.4700"), Decimal("4.5")),
        ),
        source="https://external-api.kalshi.com/trade-api/v2/markets/test",
        freshness=freshness,
        executability=ExecutabilityClassification.OFFICIAL_VENUE_ORDER_BOOK,
        evidence_urls=("https://docs.kalshi.com",),
    )


def decision(
    decision_id: str,
    kind: PaperDecisionType = PaperDecisionType.BUY_YES,
    *,
    quantity: str = "1",
    limit: str = "0.52",
    tif: TimeInForce = TimeInForce.IMMEDIATE_OR_CANCEL,
) -> StrategyDecision:
    outcome = ContractOutcome.NO if kind is PaperDecisionType.BUY_NO else ContractOutcome.YES
    return StrategyDecision(
        decision_id=decision_id,
        signal_timestamp=NOW,
        asset=Asset.BTC,
        event_id="event-1",
        contract_id="contract-1",
        decision=kind,
        outcome=outcome,
        quantity=Decimal(quantity),
        limit_price=Decimal(limit),
        time_in_force=tif,
    )


def provider(
    tmp_path, *, max_order: str = "100", kill_switch: bool = False, starting_cash: str = "1000"
):
    store = PaperStore(
        tmp_path / "paper.sqlite3", account_id="paper", starting_cash=Decimal(starting_cash)
    )
    limits = HardRiskLimits(
        max_order_notional=Decimal(max_order),
        max_event_exposure=Decimal("200"),
        max_daily_loss=Decimal("100"),
        max_total_exposure=Decimal("500"),
        consecutive_loss_halt_count=3,
    )
    return store, KalshiPaperExecutionProvider(
        store=store,
        account_id="paper",
        starting_cash=Decimal(starting_cash),
        risk=ImmutableHardRiskLayer(limits),
        kill_switch=kill_switch,
    )


def test_full_fill_uses_yes_ask_and_preserves_decimal(tmp_path) -> None:
    store, adapter = provider(tmp_path)
    result = adapter.execute(decision("full", quantity="1.2500"), quote())

    assert result.state is ExecutionOrderState.FILLED
    assert result.average_fill_price == Decimal("0.5200")
    assert result.filled_quantity == Decimal("1.2500")
    assert adapter.portfolio.position("event-1", ContractOutcome.YES).quantity == Decimal("1.2500")
    assert next(store.replay_fills()).quantity == Decimal("1.2500")
    assert store.latest_order(result.order_id).venue_ticker == "KXBTC15M-TEST"
    store.close()


def test_partial_fill_and_fok_no_fill_follow_real_depth() -> None:
    simulator = KalshiOrderBookFillSimulator()
    partial = simulator.simulate(
        order_id="partial",
        decision=decision("partial", quantity="2", limit="0.52"),
        quote=quote(),
        submit_timestamp=NOW,
    )
    no_fill = simulator.simulate(
        order_id="fok",
        decision=decision("fok", quantity="2", limit="0.52", tif=TimeInForce.FILL_OR_KILL),
        quote=quote(),
        submit_timestamp=NOW,
    )

    assert partial.reason is PaperExecutionReason.PARTIAL_FILL
    assert partial.state is ExecutionOrderState.CANCELLED
    assert partial.filled_quantity == Decimal("1.25")
    assert no_fill.reason is PaperExecutionReason.NO_FILL
    assert no_fill.filled_quantity == 0


def test_gtc_partial_fill_remains_open_and_cancel_preserves_fill_accounting(tmp_path) -> None:
    store, adapter = provider(tmp_path)
    partial = adapter.execute(
        decision(
            "gtc-partial",
            quantity="2",
            limit="0.52",
            tif=TimeInForce.GOOD_TILL_CANCELED,
        ),
        quote(),
    )

    assert partial.state is ExecutionOrderState.PARTIALLY_FILLED
    before = store.latest_order(partial.order_id)
    cancelled = adapter.cancel_order("paper", partial.order_id)
    after = store.latest_order(partial.order_id)

    assert cancelled.state is ExecutionOrderState.CANCELLED
    assert after.filled_quantity == before.filled_quantity
    assert after.average_fill_price == before.average_fill_price
    assert after.fees == before.fees
    store.close()


def test_no_fill_and_price_moved_are_explicit() -> None:
    simulator = KalshiOrderBookFillSimulator()
    no_fill = simulator.simulate(
        order_id="ioc",
        decision=decision("ioc", limit="0.51"),
        quote=quote(),
        submit_timestamp=NOW,
    )
    moved_quote = replace(quote(), yes_ask=Decimal("0.53"))
    moved = simulator.simulate(
        order_id="moved",
        decision=decision("moved", limit="0.53"),
        quote=moved_quote,
        submit_timestamp=NOW,
    )

    assert no_fill.state is ExecutionOrderState.CANCELLED
    assert no_fill.filled_quantity == 0
    assert moved.reason is PaperExecutionReason.PRICE_MOVED


def test_gtc_no_fill_can_be_cancelled(tmp_path) -> None:
    store, adapter = provider(tmp_path)
    resting = adapter.execute(
        decision(
            "resting",
            limit="0.51",
            tif=TimeInForce.GOOD_TILL_CANCELED,
        ),
        quote(),
    )
    cancel = StrategyDecision(
        decision_id="cancel-resting",
        signal_timestamp=NOW,
        asset=Asset.BTC,
        event_id="event-1",
        contract_id="contract-1",
        decision=PaperDecisionType.CANCEL,
        outcome=None,
        quantity=Decimal(0),
        limit_price=None,
        target_order_id=resting.order_id,
    )
    cancelled = adapter.execute(cancel, None)

    assert resting.state is ExecutionOrderState.OPEN
    assert cancelled.state is ExecutionOrderState.CANCELLED
    assert store.latest_order(resting.order_id).state is ExecutionOrderState.CANCELLED
    store.close()


def test_buy_no_uses_no_ask_derived_from_yes_bid_depth() -> None:
    simulator = KalshiOrderBookFillSimulator()
    buy_no = decision("buy-no", PaperDecisionType.BUY_NO, quantity="1", limit="0.49")
    result = simulator.simulate(
        order_id="buy-no", decision=buy_no, quote=quote(), submit_timestamp=NOW
    )

    assert result.average_fill_price == Decimal("0.4900")
    assert result.reason is PaperExecutionReason.FULL_FILL


def test_stale_missing_and_uncertain_inputs_are_hard_blocked(tmp_path) -> None:
    store, adapter = provider(tmp_path)
    stale = adapter.execute(decision("stale"), quote(freshness=FreshnessState.STALE))
    uncertain = adapter.execute(decision("uncertain"), quote(), fill_state_certain=False)
    missing = adapter.execute(decision("missing"), replace(quote(), yes_ask=None))
    source = adapter.execute(decision("source"), None)
    kill_store, killed_adapter = provider(tmp_path / "kill", kill_switch=True)
    killed = killed_adapter.execute(decision("kill"), quote())

    assert stale.reason is PaperExecutionReason.RISK_BLOCKED
    assert RiskBlockReason.STALE_QUOTE in stale.risk.reasons
    assert RiskBlockReason.FILL_STATE_UNCERTAIN in uncertain.risk.reasons
    assert RiskBlockReason.MISSING_BID_ASK in missing.risk.reasons
    assert RiskBlockReason.DATA_SOURCE_UNHEALTHY in source.risk.reasons
    assert RiskBlockReason.KILL_SWITCH in killed.risk.reasons
    assert store.counts()["paper_fills"] == 0
    store.close()
    kill_store.close()


def test_buying_power_reserves_fees_and_never_allows_negative_cash(tmp_path) -> None:
    store, adapter = provider(tmp_path, starting_cash="0.52")

    result = adapter.execute(decision("fee-reserve", quantity="1"), quote())

    assert result.reason is PaperExecutionReason.RISK_BLOCKED
    assert RiskBlockReason.INSUFFICIENT_BUYING_POWER in result.risk.reasons
    assert adapter.portfolio.cash == Decimal("0.52")
    assert store.counts()["paper_fills"] == 0
    store.close()


def test_add_reduce_and_close_update_portfolio(tmp_path) -> None:
    store, adapter = provider(tmp_path)
    adapter.execute(decision("open", quantity="1"), quote())
    adapter.execute(decision("add", PaperDecisionType.ADD, quantity="1"), quote())
    adapter.execute(
        decision("reduce", PaperDecisionType.REDUCE, quantity="0.5", limit="0.51"), quote()
    )
    adapter.execute(decision("close", PaperDecisionType.CLOSE, quantity="1", limit="0.51"), quote())

    assert adapter.portfolio.position("event-1", ContractOutcome.YES) is None
    assert adapter.portfolio.realized_pnl < 0
    assert adapter.portfolio.fees_paid > 0
    assert store.counts()["paper_fills"] == 4
    store.close()


def test_complex_position_accounting_reconciles_cash_pnl_fees_and_exposure(tmp_path) -> None:
    store, adapter = provider(tmp_path)
    open_result = adapter.execute(decision("math-open", quantity="1"), quote())
    richer_ask = replace(
        quote(),
        yes_ask=Decimal("0.5400"),
        no_bid=Decimal("0.4600"),
        no_bid_depth=(OrderBookLevel(Decimal("0.4600"), Decimal("2")),),
    )
    add_result = adapter.execute(
        decision("math-add", PaperDecisionType.ADD, quantity="1", limit="0.54"), richer_ask
    )
    profitable_bid = replace(
        quote(),
        yes_bid=Decimal("0.5500"),
        yes_ask=Decimal("0.5600"),
        no_bid=Decimal("0.4400"),
        yes_bid_depth=(OrderBookLevel(Decimal("0.5500"), Decimal("2")),),
        no_bid_depth=(OrderBookLevel(Decimal("0.4400"), Decimal("2")),),
    )
    reduce_result = adapter.execute(
        decision("math-reduce", PaperDecisionType.REDUCE, quantity="0.5", limit="0.55"),
        profitable_bid,
    )
    losing_bid = replace(
        quote(),
        yes_bid=Decimal("0.5000"),
        yes_bid_depth=(OrderBookLevel(Decimal("0.5000"), Decimal("2")),),
    )
    close_result = adapter.execute(
        decision("math-close", PaperDecisionType.CLOSE, quantity="1", limit="0.50"),
        losing_bid,
    )
    fees = sum(result.fees for result in (open_result, add_result, reduce_result, close_result))
    expected_realized = (
        (Decimal("0.55") - Decimal("0.53")) * Decimal("0.5")
        + (Decimal("0.50") - Decimal("0.53")) * Decimal("1.5")
        - fees
    )
    state = adapter.portfolio.state({}, close_result.fills[-1].fill_timestamp)

    assert adapter.portfolio.position("event-1", ContractOutcome.YES) is None
    assert state.realized_pnl == expected_realized
    assert state.cash == Decimal("1000") + expected_realized
    assert state.unrealized_pnl == 0
    assert state.total_exposure == 0
    assert state.daily_pnl == expected_realized
    assert state.fees_paid == fees
    store.close()


def test_buy_and_close_no_use_no_ask_then_no_bid(tmp_path) -> None:
    store, adapter = provider(tmp_path)
    opened = adapter.execute(
        decision("no-open", PaperDecisionType.BUY_NO, quantity="1", limit="0.49"), quote()
    )
    closed = adapter.execute(
        replace(
            decision("no-close", PaperDecisionType.CLOSE, quantity="1", limit="0.48"),
            outcome=ContractOutcome.NO,
        ),
        quote(),
    )

    assert opened.average_fill_price == Decimal("0.4900")
    assert closed.average_fill_price == Decimal("0.4800")
    assert adapter.portfolio.position("event-1", ContractOutcome.NO) is None
    store.close()


def test_multi_level_losing_exit_counts_as_one_consecutive_loss(tmp_path) -> None:
    store, adapter = provider(tmp_path)
    adapter.execute(decision("deep-open", quantity="3", limit="0.53"), quote())
    result = adapter.execute(
        decision("deep-close", PaperDecisionType.CLOSE, quantity="1", limit="0.50"),
        quote(),
    )

    assert len(result.fills) == 2
    assert adapter.portfolio.consecutive_losses == 1
    store.close()


def test_restart_duplicate_protection_and_deterministic_replay(tmp_path) -> None:
    store, adapter = provider(tmp_path)
    first = adapter.execute(decision("once"), quote())
    duplicate = adapter.execute(decision("once"), quote())
    store.close()

    reopened = PaperStore(
        tmp_path / "paper.sqlite3", account_id="paper", starting_cash=Decimal("1000")
    )
    limits = HardRiskLimits(Decimal("100"), Decimal("200"), Decimal("100"), Decimal("500"), 3)
    recovered = KalshiPaperExecutionProvider(
        store=reopened,
        account_id="paper",
        starting_cash=Decimal("1000"),
        risk=ImmutableHardRiskLayer(limits),
    )
    replay = PaperReplayReader(reopened)

    assert first.order_id == duplicate.order_id
    assert reopened.counts()["paper_fills"] == 1
    assert recovered.portfolio.position("event-1", ContractOutcome.YES).quantity == Decimal("1")
    assert [item.row_id for item in replay.fills()] == sorted(
        item.row_id for item in replay.fills()
    )
    assert replay.portfolios()[-1].cash == recovered.portfolio.cash
    assert reopened.integrity_check() == "ok"
    reopened.close()


def test_duplicate_decision_id_with_changed_intent_fails_closed(tmp_path) -> None:
    store, adapter = provider(tmp_path)
    adapter.execute(decision("immutable-decision", quantity="1"), quote())

    with pytest.raises(PaperExecutionError, match="conflicts"):
        adapter.execute(decision("immutable-decision", quantity="1.01"), quote())

    assert store.counts()["paper_decisions"] == 1
    assert store.counts()["paper_orders"] == 1
    assert store.counts()["paper_fills"] == 1
    store.close()


def test_pending_settlement_survives_restart_without_coinbase_substitute(tmp_path) -> None:
    store, adapter = provider(tmp_path)
    adapter.execute(decision("pending"), quote())
    realized_before = adapter.portfolio.realized_pnl
    adapter.portfolio.mark_pending_settlement("event-1")
    assert adapter.portfolio.realized_pnl == realized_before
    with pytest.raises(PaperExecutionError, match="cannot be traded"):
        adapter.execute(
            decision("pending-reduce", PaperDecisionType.REDUCE, quantity="0.5", limit="0.51"),
            quote(),
        )
    store.append_portfolio(adapter.portfolio.state({}, NOW + timedelta(minutes=15)))
    store.close()

    reopened = PaperStore(
        tmp_path / "paper.sqlite3", account_id="paper", starting_cash=Decimal("1000")
    )
    recovered = KalshiPaperExecutionProvider(
        store=reopened,
        account_id="paper",
        starting_cash=Decimal("1000"),
        risk=ImmutableHardRiskLayer(
            HardRiskLimits(Decimal("100"), Decimal("200"), Decimal("100"), Decimal("500"), 3)
        ),
    )

    assert (
        recovered.portfolio.position("event-1", ContractOutcome.YES).status
        is PaperPositionStatus.PENDING_SETTLEMENT
    )
    reopened.close()


def test_malformed_paper_decimal_fails_replay_loudly(tmp_path) -> None:
    store, adapter = provider(tmp_path)
    adapter.execute(decision("malformed"), quote())
    store._connection.execute("UPDATE paper_fills SET price='not-a-decimal'")
    store._connection.commit()

    with pytest.raises(PaperStorageError, match="decimal"):
        tuple(store.replay_fills())
    store.close()


def test_add_without_position_and_mismatched_instrument_leave_no_decision(tmp_path) -> None:
    store, adapter = provider(tmp_path)
    with pytest.raises(PaperExecutionError, match="open position"):
        adapter.execute(decision("orphan-add", PaperDecisionType.ADD), quote())
    with pytest.raises(PaperExecutionError, match="exceeds position"):
        adapter.execute(decision("orphan-reduce", PaperDecisionType.REDUCE), quote())
    with pytest.raises(PaperExecutionError, match="instrument"):
        adapter.execute(decision("bad-instrument"), replace(quote(), robinhood_event_id="other"))

    assert store.counts()["paper_decisions"] == 0
    store.close()


def test_fee_model_uses_quadratic_formula_and_order_accumulator() -> None:
    model = KalshiTakerFeeModel()
    fee = model.compute(
        order_id="fee",
        quantity=Decimal("1"),
        price=Decimal("0.5"),
        action=ExecutionAction.BUY,
    )

    assert fee.trade_fee == Decimal("0.0175")
    assert fee.net_fee >= fee.trade_fee
    assert "assumed" in fee.assumption


def test_fee_rounding_applies_general_multiplier_and_order_accumulator() -> None:
    model = KalshiTakerFeeModel()
    fees = [
        model.compute(
            order_id="official-example",
            quantity=Decimal("0.30"),
            price=Decimal("0.50"),
            action=ExecutionAction.BUY,
        )
        for _ in range(3)
    ]

    assert [item.trade_fee for item in fees] == [Decimal("0.0053")] * 3
    assert [item.net_fee for item in fees] == [
        Decimal("0.0100"),
        Decimal("0.0100"),
        Decimal("0.0000"),
    ]
    assert model.pending_order_count == 1
    model.finish_order("official-example")
    assert model.pending_order_count == 0


def test_fill_simulator_releases_fee_accumulator() -> None:
    model = KalshiTakerFeeModel()
    simulator = KalshiOrderBookFillSimulator(model)

    simulator.simulate(
        order_id="bounded-fee-state",
        decision=decision("bounded-fee-state", quantity="2", limit="0.53"),
        quote=quote(),
        submit_timestamp=NOW,
    )

    assert model.pending_order_count == 0


def test_paper_and_raw_stores_reject_the_same_database(tmp_path) -> None:
    raw_path = tmp_path / "raw.sqlite3"
    with RecorderStore(raw_path):
        pass
    with pytest.raises(PaperStorageError, match="cannot share"):
        PaperStore(raw_path, account_id="paper", starting_cash=Decimal("1000"))

    paper_path = tmp_path / "paper.sqlite3"
    with PaperStore(paper_path, account_id="paper", starting_cash=Decimal("1000")):
        pass
    with pytest.raises(RecorderStorageError, match="cannot share"):
        RecorderStore(paper_path)


def test_paper_store_integrity_includes_foreign_keys(tmp_path) -> None:
    store = PaperStore(
        tmp_path / "paper.sqlite3",
        account_id="paper",
        starting_cash=Decimal("1000"),
    )

    assert store.integrity_check() == "ok"
    store.close()


def test_hard_risk_gate_instance_cannot_be_reconfigured() -> None:
    layer = ImmutableHardRiskLayer(
        HardRiskLimits(Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"), 5)
    )

    with pytest.raises(FrozenInstanceError):
        layer._limits = HardRiskLimits(  # type: ignore[misc]
            Decimal("9"), Decimal("9"), Decimal("9"), Decimal("9"), 9
        )


@pytest.mark.parametrize("invalid", [Decimal("NaN"), Decimal("Infinity")])
def test_paper_decimal_boundaries_reject_non_finite_values(invalid: Decimal) -> None:
    with pytest.raises(ValueError):
        decision("non-finite", quantity=str(invalid))
    with pytest.raises(ValueError):
        KalshiTakerFeeModel().compute(
            order_id="non-finite",
            quantity=Decimal("1"),
            price=invalid,
            action=ExecutionAction.BUY,
        )
    with pytest.raises(ValueError):
        PaperPortfolio("paper", invalid)


def test_ticker_or_contract_mismatch_is_rejected(tmp_path) -> None:
    store, adapter = provider(tmp_path)
    with pytest.raises(PaperExecutionError, match="instrument"):
        adapter.execute(decision("bad"), replace(quote(), robinhood_event_id="other"))
    store.close()
