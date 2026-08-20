from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from live15_quant.config import load_settings
from live15_quant.execution import ContractOutcome
from live15_quant.paper import PaperDecisionType, StrategyDecision
from live15_quant.paper_execution import KalshiPaperExecutionProvider
from live15_quant.paper_storage import PaperStore
from live15_quant.providers.kalshi import KalshiOfficialQuoteProvider
from live15_quant.providers.robinhood_15min import Robinhood15MinuteProvider
from live15_quant.risk import HardRiskLimits, ImmutableHardRiskLayer

pytestmark = pytest.mark.smoke


@pytest.mark.skipif(os.getenv("LIVE15_RUN_SMOKE") != "1", reason="set LIVE15_RUN_SMOKE=1")
def test_live_kalshi_quote_drives_only_local_paper_fill(tmp_path) -> None:
    settings = load_settings()
    contracts = Robinhood15MinuteProvider(settings).discover()
    provider = KalshiOfficialQuoteProvider(settings)
    quote = None
    best_quantity = None
    outcome = None
    for _ in range(4):
        for candidate in provider.quotes(contracts):
            if candidate.yes_ask is not None:
                best_quantity = (
                    candidate.no_bid_depth[0].quantity
                    if candidate.no_bid_depth
                    and Decimal(1) - candidate.no_bid_depth[0].price == candidate.yes_ask
                    else None
                )
                outcome = ContractOutcome.YES
            if best_quantity is None and candidate.no_ask is not None:
                best_quantity = (
                    candidate.yes_bid_depth[0].quantity
                    if candidate.yes_bid_depth
                    and Decimal(1) - candidate.yes_bid_depth[0].price == candidate.no_ask
                    else None
                )
                outcome = ContractOutcome.NO
            if best_quantity is not None:
                quote = candidate
                break
        if quote is not None:
            break
        time.sleep(0.25)
    if quote is None or outcome is None:
        pytest.skip("official mapped books currently have no internally consistent ask depth")
    if best_quantity is None or best_quantity <= 0:
        pytest.fail("Kalshi top-of-book and orderbook depth are inconsistent")
    quantity = min(best_quantity, Decimal("0.01"))
    decision = StrategyDecision(
        decision_id="online-paper-smoke",
        signal_timestamp=datetime.now(UTC),
        asset=quote.asset,
        event_id=quote.robinhood_event_id,
        contract_id=quote.robinhood_contract_id,
        decision=(
            PaperDecisionType.BUY_YES
            if outcome is ContractOutcome.YES
            else PaperDecisionType.BUY_NO
        ),
        outcome=outcome,
        quantity=quantity,
        limit_price=quote.yes_ask if outcome is ContractOutcome.YES else quote.no_ask,
    )
    with PaperStore(
        tmp_path / "paper-smoke.sqlite3",
        account_id="smoke-paper",
        starting_cash=Decimal("100"),
    ) as store:
        adapter = KalshiPaperExecutionProvider(
            store=store,
            account_id="smoke-paper",
            starting_cash=Decimal("100"),
            risk=ImmutableHardRiskLayer(
                HardRiskLimits(Decimal("10"), Decimal("10"), Decimal("10"), Decimal("20"), 3)
            ),
        )
        result = adapter.execute(decision, quote)
        assert result.filled_quantity == quantity
        expected = quote.yes_ask if outcome is ContractOutcome.YES else quote.no_ask
        assert result.average_fill_price == expected
        assert store.integrity_check() == "ok"
