from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from live15_quant.execution import (
    ContractOutcome,
    ExecutionAccountState,
    ExecutionAction,
    ExecutionCapabilities,
    ExecutionOrderRequest,
    ExecutionProvider,
)
from live15_quant.models import Asset, FreshnessState
from live15_quant.risk import HardRiskLimits, RiskBlockReason, RiskDecision, RiskSnapshot


def order_request() -> ExecutionOrderRequest:
    return ExecutionOrderRequest(
        account_id="opaque-account",
        asset=Asset.BTC,
        event_id="event-id",
        contract_id="contract-id",
        outcome=ContractOutcome.YES,
        action=ExecutionAction.BUY,
        quantity=Decimal("2"),
        limit_price=Decimal("0.5100"),
        client_order_id="client-order-id",
    )


def test_execution_boundary_is_protocol_only_and_has_no_risk_limits() -> None:
    assert getattr(ExecutionProvider, "_is_protocol", False) is True
    assert "limits" not in ExecutionProvider.__dict__
    assert "evaluate" not in ExecutionProvider.__dict__


def test_capabilities_can_explicitly_report_event_contracts_unsupported() -> None:
    capabilities = ExecutionCapabilities(False, False, False, False, False)

    assert capabilities.event_contract_discovery is False
    assert capabilities.event_contract_orders is False


def test_account_state_preserves_negative_buying_power_for_risk_layer() -> None:
    state = ExecutionAccountState(
        account_id="opaque-account",
        buying_power=Decimal("-1.25"),
        total_exposure=Decimal("0"),
        daily_realized_pnl=Decimal("-2.50"),
        currency="USD",
        received_timestamp=datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert state.buying_power == Decimal("-1.25")


def test_order_request_rejects_invalid_quantity_and_probability() -> None:
    with pytest.raises(ValueError, match="quantity"):
        replace(order_request(), quantity=Decimal("0"))

    with pytest.raises(ValueError, match="limit price"):
        replace(order_request(), limit_price=Decimal("1.01"))


def test_hard_risk_limits_are_required_and_immutable() -> None:
    limits = HardRiskLimits(
        max_order_notional=Decimal("1"),
        max_event_exposure=Decimal("2"),
        max_daily_loss=Decimal("3"),
        max_total_exposure=Decimal("4"),
        consecutive_loss_halt_count=5,
    )

    with pytest.raises(FrozenInstanceError):
        limits.max_order_notional = Decimal("99")  # type: ignore[misc]


def test_risk_state_has_explicit_staleness_source_and_fill_guards() -> None:
    snapshot = RiskSnapshot(
        quote_freshness=FreshnessState.STALE,
        data_sources_healthy=False,
        fill_state_certain=False,
        event_exposure=Decimal("0"),
        consecutive_losses=0,
    )
    decision = RiskDecision(
        allowed=False,
        reasons=(
            RiskBlockReason.STALE_QUOTE,
            RiskBlockReason.DATA_SOURCE_UNHEALTHY,
            RiskBlockReason.FILL_STATE_UNCERTAIN,
        ),
    )

    assert snapshot.quote_freshness is FreshnessState.STALE
    assert decision.allowed is False


def test_risk_decision_cannot_be_ambiguous() -> None:
    with pytest.raises(ValueError, match="blocked decisions"):
        RiskDecision(allowed=False, reasons=())

    with pytest.raises(ValueError, match="allowed decisions"):
        RiskDecision(allowed=True, reasons=(RiskBlockReason.STALE_QUOTE,))
