from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest

from live15_quant.config import Settings
from live15_quant.providers import coinbase


class FakeWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.sent: list[dict[str, Any]] = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def __aiter__(self) -> AsyncIterator[str]:
        async def iterate() -> AsyncIterator[str]:
            for message in self.messages:
                yield message

        return iterate()


class FakeConnection:
    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> FakeWebSocket:
        return self.websocket

    async def __aexit__(self, *_args: object) -> None:
        return None


async def test_websocket_client_subscribes_and_normalizes_ticker(monkeypatch: Any) -> None:
    websocket = FakeWebSocket(
        [
            json.dumps({"type": "subscriptions"}),
            json.dumps(
                {
                    "type": "ticker",
                    "product_id": "BTC-USD",
                    "price": "68000.00",
                    "best_bid": "67999.99",
                    "best_ask": "68000.01",
                    "time": "2026-08-20T01:00:00Z",
                    "best_bid_size": "1.000000000001",
                    "best_ask_size": "2.000000000002",
                    "last_size": "0.0000000100",
                    "volume_24h": "123.456789012345",
                }
            ),
        ]
    )

    def fake_connect(*_args: object, **_kwargs: object) -> FakeConnection:
        return FakeConnection(websocket)

    monkeypatch.setattr(coinbase, "connect", fake_connect)
    client = coinbase.CoinbaseWebSocketClient(Settings(), products=("BTC-USD",))
    stream = client.ticks()

    tick = await anext(stream)
    await stream.aclose()

    assert tick.symbol == "BTC-USD"
    assert tick.spread == Decimal("0.02")
    assert tick.last_size == Decimal("0.0000000100")
    assert tick.bid_size == Decimal("1.000000000001")
    assert tick.ask_size == Decimal("2.000000000002")
    assert tick.volume_24h == Decimal("123.456789012345")
    assert websocket.sent == [
        {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}
    ]


async def test_websocket_client_discards_invalid_messages(monkeypatch: Any) -> None:
    websocket = FakeWebSocket(
        [
            "not-json",
            "[]",
            json.dumps(
                {
                    "type": "ticker",
                    "product_id": "ETH-USD",
                    "price": "2000",
                    "best_bid": "1999.99",
                    "best_ask": "2000.01",
                }
            ),
        ]
    )
    monkeypatch.setattr(coinbase, "connect", lambda *_args, **_kwargs: FakeConnection(websocket))
    stream = coinbase.CoinbaseWebSocketClient(Settings(), products=("ETH-USD",)).ticks()

    tick = await anext(stream)
    await stream.aclose()

    assert tick.symbol == "ETH-USD"


def test_websocket_client_rejects_empty_products() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        coinbase.CoinbaseWebSocketClient(Settings(), products=())


def test_ticker_parser_rejects_float_price_to_prevent_precision_pollution() -> None:
    with pytest.raises(coinbase.CoinbasePayloadError, match="invalid Coinbase ticker"):
        coinbase.parse_ticker_payload(
            {
                "type": "ticker",
                "product_id": "BTC-USD",
                "price": 68159.123456789,
                "best_bid": "68159.12",
                "best_ask": "68159.13",
            }
        )


async def test_websocket_client_reconnects_after_connection_error(monkeypatch: Any) -> None:
    websocket = FakeWebSocket(
        [
            json.dumps(
                {
                    "type": "ticker",
                    "product_id": "BTC-USD",
                    "price": "68000",
                    "best_bid": "67999",
                    "best_ask": "68001",
                }
            )
        ]
    )
    attempts = 0

    def flaky_connect(*_args: object, **_kwargs: object) -> FakeConnection:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary connection failure")
        return FakeConnection(websocket)

    monkeypatch.setattr(coinbase, "connect", flaky_connect)
    settings = Settings(reconnect_delay_seconds=0)
    stream = coinbase.CoinbaseWebSocketClient(settings, products=("BTC-USD",)).ticks()

    tick = await anext(stream)
    await stream.aclose()

    assert tick.symbol == "BTC-USD"
    assert attempts == 2
