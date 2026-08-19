from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from live15_quant.config import Settings
from live15_quant.models import DataRole
from live15_quant.providers.coinbase import CoinbasePayloadError, CoinbaseRestClient


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str]:
        return {
            "price": "68123.45",
            "bid": "68123.44",
            "ask": "68123.46",
            "time": "2026-08-20T01:00:00Z",
        }


class FakeSession:
    def __init__(self) -> None:
        self.request: tuple[str, dict[str, Any]] | None = None

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.request = (url, kwargs)
        return FakeResponse()


class NonObjectResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return []


class NonObjectSession:
    def get(self, _url: str, *, timeout: float) -> NonObjectResponse:
        assert timeout == 10.0
        return NonObjectResponse()


def test_rest_client_normalizes_ticker() -> None:
    session = FakeSession()
    client = CoinbaseRestClient(Settings(), session=session)

    tick = client.get_ticker("BTC-USD")

    assert tick.symbol == "BTC-USD"
    assert tick.price == Decimal("68123.45")
    assert tick.spread == Decimal("0.02")
    assert tick.exchange_time is not None
    assert tick.role is DataRole.PREDICTIVE_MARKET_DATA
    assert session.request == (
        "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
        {"timeout": 10.0},
    )


def test_rest_client_rejects_non_object_payload() -> None:
    with pytest.raises(CoinbasePayloadError, match="must be an object"):
        CoinbaseRestClient(Settings(), session=NonObjectSession()).get_ticker("BTC-USD")
