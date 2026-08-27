"""Read-only Production Kalshi account projection for the Control Center."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from live15_quant.control_center_models import (
    AccountFillResponse,
    AccountOrderResponse,
    AccountPositionResponse,
    AccountProfileResponse,
    AccountReadResponse,
    AccountSummaryResponse,
)
from live15_quant.kalshi_gateway.client import (
    KalshiEnvironment,
    KalshiGatewayConfig,
    KalshiGatewayError,
    build_sdk_client,
    production_credentials,
)
from live15_quant.kalshi_gateway.portfolio import KalshiPortfolioGateway


class AccountGateway(Protocol):
    def balance(self, *, exchange_index: int | None = None) -> Any: ...
    def positions(
        self, *, ticker: str | None = None, exchange_index: int | None = None
    ) -> tuple[Any, ...]: ...
    def orders(
        self, *, ticker: str | None = None, exchange_index: int | None = None
    ) -> tuple[Any, ...]: ...
    def fills(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
        exchange_index: int | None = None,
    ) -> tuple[Any, ...]: ...


def _get(item: Any, *names: str) -> Any:
    for name in names:
        if isinstance(item, dict) and name in item:
            return item[name]
        value = getattr(item, name, None)
        if value is not None:
            return value
    return None


def _int(item: Any, *names: str) -> int | None:
    value = _get(item, *names)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dt(item: Any, *names: str) -> datetime | None:
    value = _get(item, *names)
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value) / (1000 if value > 10_000_000_000 else 1)
        return datetime.fromtimestamp(timestamp, UTC)
    return None


class ProductionAccountService:
    """Builds the existing SDK gateway lazily; never serializes credentials."""

    def __init__(self, settings: Any, *, gateway: AccountGateway | None = None, clock=None) -> None:
        self.settings = settings
        self._gateway = gateway
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def profiles() -> list[AccountProfileResponse]:
        return [
            AccountProfileResponse(
                key="production_primary",
                label="Production · Primary",
                environment="PRODUCTION",
                is_primary=True,
            )
        ]

    def _gateway_or_raise(self) -> AccountGateway:
        if self._gateway is not None:
            return self._gateway
        credentials = production_credentials(self.settings)
        config = KalshiGatewayConfig.for_environment(KalshiEnvironment.PRODUCTION)
        self._gateway = KalshiPortfolioGateway(build_sdk_client(config, credentials=credentials))
        return self._gateway

    def read(self, profile: str = "production_primary") -> AccountReadResponse:
        observed = self._clock()
        if profile != "production_primary":
            return self._unavailable(profile, observed, "unknown account profile")
        try:
            gateway = self._gateway_or_raise()
            balance = gateway.balance()
            positions = tuple(gateway.positions())
            orders = tuple(gateway.orders())
            fills = tuple(gateway.fills())
        except Exception as error:
            status = (
                "AUTH_ERROR"
                if isinstance(error, (KalshiGatewayError, PermissionError))
                else "UNAVAILABLE"
            )
            return self._unavailable(profile, observed, status)
        balance_cents = _int(balance, "balance", "balance_cents")
        portfolio_cents = _int(balance, "portfolio_value", "portfolio_value_cents")
        summary = AccountSummaryResponse(
            profile=profile,
            status="AVAILABLE",
            observed_at=observed,
            balance_cents=balance_cents,
            portfolio_value_cents=portfolio_cents,
        )
        return AccountReadResponse(
            profile=profile,
            status="AVAILABLE",
            observed_at=observed,
            summary=summary,
            positions=[self._position(item) for item in positions],
            orders=[self._order(item) for item in orders],
            fills=[self._fill(item) for item in fills],
        )

    def _unavailable(self, profile: str, observed: datetime, message: str) -> AccountReadResponse:
        summary = AccountSummaryResponse(
            profile=profile, status="UNAVAILABLE", observed_at=observed, message=message
        )
        return AccountReadResponse(
            profile=profile,
            status="UNAVAILABLE",
            observed_at=observed,
            summary=summary,
            message=message,
        )

    @staticmethod
    def _position(item: Any) -> AccountPositionResponse:
        return AccountPositionResponse(
            ticker=str(_get(item, "ticker", "market_ticker") or "UNKNOWN"),
            position=_int(item, "position", "quantity", "market_position"),
            market_exposure_cents=_int(item, "market_exposure", "market_exposure_cents"),
            realized_pnl_cents=_int(item, "realized_pnl", "realized_pnl_cents"),
            fees_cents=_int(item, "fees", "fees_cents"),
        )

    @staticmethod
    def _order(item: Any) -> AccountOrderResponse:
        return AccountOrderResponse(
            order_id=str(_get(item, "order_id", "id") or "UNKNOWN"),
            ticker=_get(item, "ticker", "market_ticker"),
            status=_get(item, "status"),
            side=_get(item, "side"),
            action=_get(item, "action"),
            count=_int(item, "count", "quantity"),
            remaining_count=_int(item, "remaining_count"),
            yes_price_cents=_int(item, "yes_price"),
            no_price_cents=_int(item, "no_price"),
            created_at=_dt(item, "created_time", "created_at"),
        )

    @staticmethod
    def _fill(item: Any) -> AccountFillResponse:
        return AccountFillResponse(
            trade_id=str(_get(item, "trade_id", "id") or "UNKNOWN"),
            order_id=_get(item, "order_id"),
            ticker=_get(item, "ticker", "market_ticker"),
            side=_get(item, "side"),
            action=_get(item, "action"),
            count=_int(item, "count", "quantity"),
            yes_price_cents=_int(item, "yes_price"),
            no_price_cents=_int(item, "no_price"),
            created_at=_dt(item, "created_time", "created_at"),
        )
