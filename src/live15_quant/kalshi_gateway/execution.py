"""V2 order primitives behind an explicit write-enable boundary."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from live15_quant.kalshi_gateway.client import KalshiEnvironment


class KalshiWriteDisabledError(RuntimeError):
    """Raised before SDK construction when mutation is not explicitly enabled."""


@dataclass(frozen=True, slots=True)
class GatewayOrderIntent:
    ticker: str
    client_order_id: str
    side: Literal["bid", "ask"]
    count: Decimal
    price: Decimal
    time_in_force: Literal["fill_or_kill", "good_till_canceled", "immediate_or_cancel"]
    self_trade_prevention_type: Literal["taker_at_cross", "maker"] = "taker_at_cross"
    post_only: bool | None = None
    cancel_order_on_pause: bool | None = None
    reduce_only: bool | None = None
    subaccount: int | None = None
    exchange_index: int | None = None

    def __post_init__(self) -> None:
        if (
            not self.ticker
            or not self.client_order_id
            or self.count <= 0
            or self.price <= 0
            or self.price >= 1
            or (self.exchange_index is not None and self.exchange_index < 0)
        ):
            raise ValueError("Kalshi V2 order intent is invalid")

    def sdk_kwargs(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "client_order_id": self.client_order_id,
            "side": self.side,
            "count": self.count,
            "price": self.price,
            "time_in_force": self.time_in_force,
            "self_trade_prevention_type": self.self_trade_prevention_type,
            "post_only": self.post_only,
            "cancel_order_on_pause": self.cancel_order_on_pause,
            "reduce_only": self.reduce_only,
            "subaccount": self.subaccount,
            "exchange_index": self.exchange_index,
        }


def _order_models() -> tuple[type[Any], type[Any], type[Any]]:
    try:
        from kalshi.models.orders import (
            AmendOrderV2Request,
            CreateOrderV2Request,
            DecreaseOrderV2Request,
        )
    except ImportError as error:  # pragma: no cover - deployment failure path
        raise KalshiWriteDisabledError("kalshi-sdk==12.0.0 is unavailable") from error
    return CreateOrderV2Request, AmendOrderV2Request, DecreaseOrderV2Request


class KalshiExecutionGateway:
    """No mutation can cross this boundary unless the caller enabled it explicitly."""

    def __init__(
        self,
        sdk_client: Any,
        *,
        environment: KalshiEnvironment,
        write_enabled: bool = False,
    ) -> None:
        self._client = sdk_client
        self.environment = environment
        self.write_enabled = write_enabled

    def _require_write(self) -> None:
        if not self.write_enabled:
            raise KalshiWriteDisabledError("Kalshi SDK write is disabled")

    def create(self, intent: GatewayOrderIntent) -> Any:
        self._require_write()
        create_request, _, _ = _order_models()
        request = create_request(**intent.sdk_kwargs())
        return self._client.orders.create_v2(request=request)

    def cancel(
        self,
        order_id: str,
        *,
        exchange_index: int | None,
        market_ticker: str | None = None,
    ) -> Any:
        self._require_write()
        return self._client.orders.cancel_v2(
            order_id,
            exchange_index=exchange_index,
            market_ticker=market_ticker,
        )

    def amend(self, order_id: str, **request_fields: object) -> Any:
        self._require_write()
        _, amend_request, _ = _order_models()
        return self._client.orders.amend_v2(
            order_id,
            request=amend_request(**request_fields),
        )

    def decrease(self, order_id: str, **request_fields: object) -> Any:
        self._require_write()
        _, _, decrease_request = _order_models()
        return self._client.orders.decrease_v2(
            order_id,
            request=decrease_request(**request_fields),
        )
