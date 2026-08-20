"""Documented Kalshi fee assumptions for local paper fills only."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from live15_quant.execution import ExecutionAction

KALSHI_FEE_SCHEDULE_URL = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"
KALSHI_FEE_ROUNDING_URL = "https://docs.kalshi.com/getting_started/fee_rounding"
KALSHI_SERIES_FEE_CHANGES_URL = "https://external-api.kalshi.com/trade-api/v2/series/fee_changes"

_CENT = Decimal("0.01")
_CENTICENT = Decimal("0.0001")


def _ceil(value: Decimal, quantum: Decimal) -> Decimal:
    return (value / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum


def _floor(value: Decimal, quantum: Decimal) -> Decimal:
    return (value / quantum).to_integral_value(rounding=ROUND_FLOOR) * quantum


@dataclass(frozen=True, slots=True)
class FeeComputation:
    """One fill's documented trade fee and cent-alignment adjustments."""

    trade_fee: Decimal
    rounding_fee: Decimal
    rebate: Decimal
    net_fee: Decimal
    assumption: str


class KalshiTakerFeeModel:
    """General quadratic taker fee with official 2026 fixed-point rounding.

    The ten audited 15-minute series returned no series-specific fee changes on
    2026-08-20. This model therefore applies the published general taker
    multiplier. It does not claim to predict future fee changes or maker rebates.
    """

    multiplier = Decimal("0.07")
    assumption = (
        "general quadratic taker fee assumed because the official fee_changes endpoint "
        "returned no overrides for all ten audited 15-minute series on 2026-08-20"
    )

    def __init__(self) -> None:
        self._rounding_accumulator: dict[str, Decimal] = {}

    @classmethod
    def conservative_reserve(cls, quantity: Decimal, max_fills: int) -> Decimal:
        """Upper-bound general trade fee, cent alignment, and per-fill ceiling overhead."""

        if not quantity.is_finite() or quantity <= 0 or max_fills <= 0:
            raise ValueError("fee reserve inputs must be positive")
        maximum_quadratic_fee = cls.multiplier * quantity * Decimal("0.25")
        return _ceil(maximum_quadratic_fee, _CENTICENT) + _CENT + _CENTICENT * max_fills

    def compute(
        self,
        *,
        order_id: str,
        quantity: Decimal,
        price: Decimal,
        action: ExecutionAction,
    ) -> FeeComputation:
        if (
            not order_id
            or not quantity.is_finite()
            or quantity <= 0
            or not price.is_finite()
            or not Decimal(0) <= price <= Decimal(1)
        ):
            raise ValueError("invalid fee inputs")
        raw_trade_fee = self.multiplier * quantity * price * (Decimal(1) - price)
        trade_fee = _ceil(raw_trade_fee, _CENTICENT)
        revenue = price * quantity * (Decimal(-1) if action is ExecutionAction.BUY else Decimal(1))
        balance_change = revenue - trade_fee
        rounded_balance_change = _floor(balance_change, _CENT)
        rounding_fee = balance_change - rounded_balance_change
        accumulated = self._rounding_accumulator.get(order_id, Decimal(0)) + rounding_fee
        rebate = _floor(accumulated, _CENT)
        self._rounding_accumulator[order_id] = accumulated - rebate
        net_fee = trade_fee + rounding_fee - rebate
        if net_fee < 0:
            raise ValueError("fee rounding produced a negative fee")
        return FeeComputation(
            trade_fee=trade_fee,
            rounding_fee=rounding_fee,
            rebate=rebate,
            net_fee=net_fee,
            assumption=self.assumption,
        )

    def finish_order(self, order_id: str) -> None:
        """Release an order accumulator after its one-shot local simulation ends."""

        self._rounding_accumulator.pop(order_id, None)

    @property
    def pending_order_count(self) -> int:
        """Expose bounded-state health without revealing fee internals."""

        return len(self._rounding_accumulator)
