"""Public Coinbase Exchange REST and WebSocket market-data clients."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import requests
from websockets.asyncio.client import connect

from live15_quant.config import Settings
from live15_quant.models import MarketTick

logger = logging.getLogger(__name__)


class CoinbasePayloadError(ValueError):
    """Raised when a Coinbase payload cannot be normalized."""


class HttpResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


class HttpSession(Protocol):
    def get(self, url: str, *, timeout: float) -> HttpResponse: ...


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CoinbasePayloadError("invalid Coinbase timestamp") from error


def _source_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, (float, bool)):
        raise CoinbasePayloadError(f"{name} must be a source precision string")
    return Decimal(str(value))


def _optional_decimal(payload: Mapping[str, Any], name: str) -> Decimal | None:
    value: object = payload.get(name)
    if value is None or value == "":
        return None
    return _source_decimal(value, name)


def parse_ticker_payload(
    payload: Mapping[str, Any], *, received_at: datetime | None = None
) -> MarketTick | None:
    """Normalize a Coinbase ticker message, ignoring non-ticker messages."""

    if payload.get("type") != "ticker":
        return None
    try:
        return MarketTick(
            symbol=str(payload["product_id"]),
            price=_source_decimal(payload["price"], "price"),
            bid=_source_decimal(payload["best_bid"], "best_bid"),
            ask=_source_decimal(payload["best_ask"], "best_ask"),
            exchange_time=_parse_time(payload.get("time")),
            received_at=received_at or datetime.now(UTC),
            bid_size=_optional_decimal(payload, "best_bid_size"),
            ask_size=_optional_decimal(payload, "best_ask_size"),
            last_size=_optional_decimal(payload, "last_size"),
            volume_24h=_optional_decimal(payload, "volume_24h"),
        )
    except (KeyError, InvalidOperation, TypeError, ValueError) as error:
        raise CoinbasePayloadError("invalid Coinbase ticker payload") from error


class CoinbaseRestClient:
    """Synchronous client for Coinbase's public product ticker endpoint."""

    def __init__(self, settings: Settings, session: HttpSession | None = None) -> None:
        self._settings = settings
        self._owned_session = requests.Session() if session is None else None
        self._session = self._owned_session or session

    def close(self) -> None:
        if self._owned_session is not None:
            self._owned_session.close()

    def get_ticker(self, product_id: str) -> MarketTick:
        url = f"{self._settings.coinbase_rest_base_url}/products/{product_id}/ticker"
        response = self._session.get(url, timeout=self._settings.request_timeout_seconds)
        response.raise_for_status()
        received_at = datetime.now(UTC)
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise CoinbasePayloadError("Coinbase REST ticker payload must be an object")
        try:
            return MarketTick(
                symbol=product_id,
                price=_source_decimal(payload["price"], "price"),
                bid=_source_decimal(payload["bid"], "bid"),
                ask=_source_decimal(payload["ask"], "ask"),
                exchange_time=_parse_time(payload.get("time")),
                received_at=received_at,
                bid_size=_optional_decimal(payload, "bid_size"),
                ask_size=_optional_decimal(payload, "ask_size"),
                last_size=_optional_decimal(payload, "size"),
                volume_24h=_optional_decimal(payload, "volume"),
            )
        except (KeyError, InvalidOperation, TypeError, ValueError) as error:
            raise CoinbasePayloadError("invalid Coinbase REST ticker payload") from error


class CoinbaseWebSocketClient:
    """Resilient async stream for Coinbase public ticker messages."""

    def __init__(self, settings: Settings, products: tuple[str, ...] | None = None) -> None:
        self._settings = settings
        self._products = settings.products if products is None else products
        if not self._products:
            raise ValueError("Coinbase WebSocket products must not be empty")

    async def ticks(self) -> AsyncIterator[MarketTick]:
        subscription = {
            "type": "subscribe",
            "product_ids": list(self._products),
            "channels": ["ticker"],
        }
        while True:
            try:
                async with connect(
                    self._settings.coinbase_websocket_url,
                    ping_interval=self._settings.websocket_ping_interval_seconds,
                    ping_timeout=self._settings.websocket_ping_timeout_seconds,
                ) as websocket:
                    await websocket.send(json.dumps(subscription))
                    logger.info(
                        "Coinbase WebSocket connected",
                        extra={"event": "coinbase_ws_connected", "products": self._products},
                    )
                    async for message in websocket:
                        received_at = datetime.now(UTC)
                        try:
                            payload = json.loads(message)
                            if not isinstance(payload, Mapping):
                                raise CoinbasePayloadError(
                                    "Coinbase WebSocket payload must be an object"
                                )
                            tick = parse_ticker_payload(payload, received_at=received_at)
                        except (json.JSONDecodeError, CoinbasePayloadError) as error:
                            logger.warning(
                                "Discarding invalid Coinbase message",
                                extra={"event": "coinbase_invalid_message", "error": str(error)},
                            )
                            continue
                        if tick is not None:
                            yield tick
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Coinbase WebSocket disconnected",
                    extra={
                        "event": "coinbase_ws_disconnected",
                        "retry_seconds": self._settings.reconnect_delay_seconds,
                    },
                )
                await asyncio.sleep(self._settings.reconnect_delay_seconds)
