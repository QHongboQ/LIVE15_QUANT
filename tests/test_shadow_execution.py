from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from live15_quant.demo_execution import DemoSynchronizedQuote
from live15_quant.execution import ContractOutcome, ExecutionAction, ExecutionOrderRequest
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
from live15_quant.paper_execution import KalshiPaperExecutionProvider
from live15_quant.paper_storage import PaperStore
from live15_quant.risk import HardRiskLimits, ImmutableHardRiskLayer
from live15_quant.shadow_execution import (
    DEMO_REAL_WRITE_FROZEN_PROVIDER_BLOCKER,
    KalshiRemoteExecutor,
    RemoteExecutionFrozenError,
    ShadowEnvironment,
    ShadowExecutor,
    ShadowExitAction,
    evaluate_shadow_exit,
    live_ws_prediction_quote,
)

NOW = datetime(2026, 8, 24, 7, 0, tzinfo=UTC)


def _quote(*, received: datetime = NOW) -> PredictionMarketQuote:
    return PredictionMarketQuote(
        asset=Asset.BTC,
        robinhood_event_id="KXBTC15M-example",
        robinhood_contract_id="KXBTC15M-example",
        venue=Venue.KALSHI,
        venue_series="KXBTC15M",
        venue_ticker="KXBTC15M-example",
        mapping_confidence=MappingConfidence.VERIFIED,
        source_timestamp=received,
        source_timestamp_kind=SourceTimestampKind.EXCHANGE_EVENT_TIME,
        received_timestamp=received,
        yes_bid=Decimal("0.60"),
        yes_ask=Decimal("0.70"),
        no_bid=Decimal("0.30"),
        no_ask=Decimal("0.40"),
        last_trade=None,
        volume=None,
        yes_bid_depth=(OrderBookLevel(Decimal("0.60"), Decimal("2")),),
        no_bid_depth=(OrderBookLevel(Decimal("0.30"), Decimal("2")),),
        source="kalshi_ws_live_projection",
        freshness=FreshnessState.FRESH,
        executability=ExecutabilityClassification.OFFICIAL_VENUE_ORDER_BOOK,
        evidence_urls=(),
    )


def _executor(path: Path, *, order_limit: Decimal = Decimal("1")) -> ShadowExecutor:
    store = PaperStore(path, account_id="shadow-test", starting_cash=Decimal("10"))
    provider = KalshiPaperExecutionProvider(
        store=store,
        account_id="shadow-test",
        starting_cash=Decimal("10"),
        risk=ImmutableHardRiskLayer(
            HardRiskLimits(
                order_limit,
                Decimal("2"),
                Decimal("2"),
                Decimal("5"),
                3,
            )
        ),
    )
    return ShadowExecutor(provider, store)


def _request(*, outcome: ContractOutcome, price: Decimal, client_id: str) -> ExecutionOrderRequest:
    return ExecutionOrderRequest(
        account_id="shadow-test",
        asset=Asset.BTC,
        event_id="KXBTC15M-example",
        contract_id="KXBTC15M-example",
        outcome=outcome,
        action=ExecutionAction.BUY,
        quantity=Decimal("1"),
        limit_price=price,
        client_order_id=client_id,
    )


def test_shadow_executor_fills_at_executable_ask_and_isolated_ledger(tmp_path: Path) -> None:
    executor = _executor(tmp_path / "shadow.sqlite3")
    try:
        executor.update_quote(_quote())
        status = executor.submit_order(
            _request(outcome=ContractOutcome.YES, price=Decimal("0.70"), client_id="yes-entry")
        )
        assert executor.environment is ShadowEnvironment.SHADOW_SIMULATED
        assert status.filled_quantity == Decimal("1")
        assert status.average_fill_price == Decimal("0.70")
        assert len(executor.get_orders()) == len(executor.get_fills()) == 1
        assert executor.get_positions()[0].outcome is ContractOutcome.YES
        assert executor.get_balance().account_id == "shadow-test"
    finally:
        executor.close()


def test_shadow_no_price_or_stale_live_projection_fails_closed() -> None:
    live = DemoSynchronizedQuote(
        ticker="KXBTC15M-example",
        received_timestamp=NOW - timedelta(seconds=5),
        synchronized=True,
        yes_bid=Decimal("0.60"),
        yes_ask=Decimal("0.70"),
        no_bid=Decimal("0.30"),
        no_ask=Decimal("0.40"),
        source="LIVE_KALSHI_WS",
        book_received_timestamp=NOW - timedelta(seconds=5),
        live_book_read_at=NOW,
        subscription_id=1,
        sequence=1,
        yes_bid_depth=((Decimal("0.60"), Decimal("2")),),
        no_bid_depth=((Decimal("0.30"), Decimal("2")),),
    )
    assert (
        live_ws_prediction_quote(
            asset=Asset.BTC,
            ticker=live.ticker,
            series="KXBTC15M",
            quote=live,
            now=NOW,
            max_quote_age=timedelta(seconds=2),
        )
        is None
    )


def test_shadow_buy_no_uses_economically_correct_executable_no_ask(tmp_path: Path) -> None:
    executor = _executor(tmp_path / "shadow.sqlite3")
    try:
        executor.update_quote(_quote())
        status = executor.submit_order(
            _request(outcome=ContractOutcome.NO, price=Decimal("0.40"), client_id="no-entry")
        )
        assert status.filled_quantity == Decimal("1")
        assert status.average_fill_price == Decimal("0.40")
    finally:
        executor.close()


def test_shadow_price_move_does_not_create_a_synthetic_fill(tmp_path: Path) -> None:
    executor = _executor(tmp_path / "shadow.sqlite3")
    try:
        executor.update_quote(_quote())
        status = executor.submit_order(
            _request(outcome=ContractOutcome.YES, price=Decimal("0.69"), client_id="moved")
        )
        assert status.filled_quantity == 0
        assert executor.get_fills() == ()
    finally:
        executor.close()


def test_shadow_settlement_uses_explicit_official_outcome_once(tmp_path: Path) -> None:
    executor = _executor(tmp_path / "shadow.sqlite3")
    try:
        executor.update_quote(_quote())
        executor.submit_order(
            _request(outcome=ContractOutcome.YES, price=Decimal("0.70"), client_id="entry")
        )
        assert executor.settle_event(
            event_id="KXBTC15M-example", outcome_yes=True, settlement_timestamp=NOW
        )
        assert not executor.settle_event(
            event_id="KXBTC15M-example", outcome_yes=True, settlement_timestamp=NOW
        )
        assert executor.get_positions() == ()
    finally:
        executor.close()


@pytest.mark.parametrize(
    ("quote", "probability", "entry_price", "window_end", "expected"),
    (
        (
            _quote(),
            Decimal("0.90"),
            Decimal("0.50"),
            NOW + timedelta(minutes=3),
            ShadowExitAction.TAKE_PROFIT,
        ),
        (
            _quote(),
            Decimal("0.20"),
            Decimal("0.70"),
            NOW + timedelta(minutes=3),
            ShadowExitAction.CUT_LOSS,
        ),
        (
            _quote(),
            Decimal("0.55"),
            Decimal("0.60"),
            NOW + timedelta(minutes=3),
            ShadowExitAction.EDGE_REVERSAL,
        ),
        (
            _quote(),
            Decimal("0.90"),
            Decimal("0.50"),
            NOW + timedelta(seconds=20),
            ShadowExitAction.HOLD_TO_SETTLEMENT,
        ),
        (
            None,
            Decimal("0.90"),
            Decimal("0.50"),
            NOW + timedelta(minutes=3),
            ShadowExitAction.DATA_UNAVAILABLE,
        ),
    ),
)
def test_deterministic_dynamic_exit_rules(
    quote: PredictionMarketQuote | None,
    probability: Decimal,
    entry_price: Decimal,
    window_end: datetime,
    expected: ShadowExitAction,
) -> None:
    assert (
        evaluate_shadow_exit(
            entry_price=entry_price,
            outcome=ContractOutcome.YES,
            quote=quote,
            fair_probability=probability,
            now=NOW,
            window_end=window_end,
        ).action
        is expected
    )


def test_exit_comparison_reserves_executable_taker_fee() -> None:
    evaluation = evaluate_shadow_exit(
        entry_price=Decimal("0.50"),
        outcome=ContractOutcome.YES,
        quote=_quote(),
        fair_probability=Decimal("0.90"),
        now=NOW,
        window_end=NOW + timedelta(minutes=3),
    )
    fee_model = KalshiTakerFeeModel()
    expected_fee = fee_model.compute(
        order_id="test-exit",
        quantity=Decimal("1"),
        price=Decimal("0.60"),
        action=ExecutionAction.SELL,
    ).net_fee
    fee_model.finish_order("test-exit")
    assert evaluation.close_now_ev == Decimal("0.60") - expected_fee
    assert evaluation.mark_change == evaluation.close_now_ev - Decimal("0.50")


def test_risk_cap_blocks_shadow_order_without_remote_contamination(tmp_path: Path) -> None:
    executor = _executor(tmp_path / "shadow.sqlite3", order_limit=Decimal("0.50"))
    try:
        executor.update_quote(_quote())
        status = executor.submit_order(
            _request(outcome=ContractOutcome.YES, price=Decimal("0.70"), client_id="blocked")
        )
        assert status.filled_quantity == 0
        assert executor.get_fills() == ()
    finally:
        executor.close()


def test_remote_executor_is_frozen_before_any_write() -> None:
    class Reader:
        def get_orders(self):
            return ()

        def get_fills(self):
            return ()

        def get_positions(self):
            return ()

        def get_balance(self):
            return None

    executor = KalshiRemoteExecutor(Reader())
    assert executor.write_state == DEMO_REAL_WRITE_FROZEN_PROVIDER_BLOCKER
    with pytest.raises(RemoteExecutionFrozenError, match=DEMO_REAL_WRITE_FROZEN_PROVIDER_BLOCKER):
        executor.submit_order(
            _request(outcome=ContractOutcome.YES, price=Decimal("0.70"), client_id="never-send")
        )
