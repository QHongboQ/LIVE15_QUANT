"""SDK-backed generic Kalshi REST reads; LIVE15 domain validation stays upstream."""

from __future__ import annotations

from typing import Any


class KalshiMarketDataGateway:
    def __init__(self, sdk_client: Any) -> None:
        self._client = sdk_client

    def exchange_status(self) -> Any:
        return self._client.exchange.status()

    def list_markets(
        self,
        *,
        status: str | None = None,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        max_pages: int | None = None,
    ) -> tuple[Any, ...]:
        return tuple(
            self._client.markets.list_all(
                status=status,
                series_ticker=series_ticker,
                event_ticker=event_ticker,
                max_pages=max_pages,
            )
        )

    def market(self, ticker: str) -> Any:
        if not ticker:
            raise ValueError("ticker must not be empty")
        return self._client.markets.get(ticker)

    def orderbook(self, ticker: str, *, depth: int = 10) -> Any:
        if not ticker or depth < 1:
            raise ValueError("orderbook request is invalid")
        return self._client.markets.orderbook(ticker, depth=depth)

    def trades(self, *, ticker: str, max_pages: int | None = None) -> tuple[Any, ...]:
        if not ticker:
            raise ValueError("ticker must not be empty")
        return tuple(self._client.markets.list_all_trades(ticker=ticker, max_pages=max_pages))
