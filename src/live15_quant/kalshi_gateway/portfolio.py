"""Authenticated portfolio reads and narrow compatibility shims."""

from __future__ import annotations

from typing import Any


class KalshiPortfolioError(RuntimeError):
    """Raised when official account truth cannot be resolved unambiguously."""


class KalshiPortfolioGateway:
    def __init__(self, sdk_client: Any) -> None:
        self._client = sdk_client

    def balance(self, *, exchange_index: int | None = None) -> Any:
        return self._client.portfolio.balance(exchange_index=exchange_index)

    def positions(
        self, *, ticker: str | None = None, exchange_index: int | None = None
    ) -> tuple[Any, ...]:
        return tuple(
            self._client.portfolio.positions_all(
                ticker=ticker,
                exchange_index=exchange_index,
                max_pages=100,
            )
        )

    def orders(
        self, *, ticker: str | None = None, exchange_index: int | None = None
    ) -> tuple[Any, ...]:
        return tuple(
            self._client.orders.list_all(
                ticker=ticker,
                exchange_index=exchange_index,
                max_pages=100,
            )
        )

    def fills(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
        exchange_index: int | None = None,
    ) -> tuple[Any, ...]:
        return tuple(
            self._client.portfolio.fills_all(
                ticker=ticker,
                order_id=order_id,
                exchange_index=exchange_index,
                max_pages=100,
            )
        )

    def order(
        self,
        order_id: str,
        *,
        ticker: str,
        exchange_index: int | None,
    ) -> Any:
        """Use SDK get first, then the proven list-based compatibility path on 404."""

        if not order_id or not ticker:
            raise ValueError("order identity is incomplete")
        try:
            return self._client.orders.get(order_id)
        except Exception as error:
            if getattr(error, "status_code", None) != 404:
                raise
        matches = [
            order
            for order in self.orders(ticker=ticker, exchange_index=exchange_index)
            if getattr(order, "order_id", None) == order_id
        ]
        if len(matches) != 1:
            raise KalshiPortfolioError("order list fallback did not resolve exactly one order")
        return matches[0]
