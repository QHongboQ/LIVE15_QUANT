"""Command-line entry points for public market-data collectors."""

from __future__ import annotations

import asyncio
import logging
import time

import requests

from live15_quant.config import Settings, load_settings
from live15_quant.logging_config import configure_logging
from live15_quant.models import MarketTick
from live15_quant.providers.coinbase import (
    CoinbasePayloadError,
    CoinbaseRestClient,
    CoinbaseWebSocketClient,
)
from live15_quant.providers.robinhood_15min import Robinhood15MinuteProvider

logger = logging.getLogger(__name__)


def _tick_fields(tick: MarketTick) -> dict[str, object]:
    return {
        "event": "market_tick",
        "source": "coinbase",
        "symbol": tick.symbol,
        "price": tick.price,
        "bid": tick.bid,
        "ask": tick.ask,
        "spread": tick.spread,
        "exchange_time": tick.exchange_time,
        "received_at": tick.received_at,
    }


def rest_main() -> None:
    """Poll the public BTC-USD REST ticker until interrupted."""

    settings = load_settings()
    configure_logging(settings.log_level)
    client = CoinbaseRestClient(settings)
    logger.info("REST collector started", extra={"event": "collector_started", "symbol": "BTC-USD"})
    try:
        while True:
            try:
                tick = client.get_ticker("BTC-USD")
                logger.info("Coinbase market tick", extra=_tick_fields(tick))
            except (requests.RequestException, CoinbasePayloadError):
                logger.exception(
                    "Coinbase REST poll failed", extra={"event": "coinbase_rest_error"}
                )
            time.sleep(settings.rest_poll_interval_seconds)
    except KeyboardInterrupt:
        logger.info("REST collector stopped", extra={"event": "collector_stopped"})


async def _stream(settings: Settings, products: tuple[str, ...]) -> None:
    client = CoinbaseWebSocketClient(settings, products=products)
    async for tick in client.ticks():
        logger.info("Coinbase market tick", extra=_tick_fields(tick))


def _run_stream(settings: Settings, products: tuple[str, ...]) -> None:
    configure_logging(settings.log_level)
    logger.info(
        "WebSocket collector started",
        extra={"event": "collector_started", "products": products},
    )
    try:
        asyncio.run(_stream(settings, products))
    except KeyboardInterrupt:
        logger.info("WebSocket collector stopped", extra={"event": "collector_stopped"})


def stream_main() -> None:
    """Stream all configured Coinbase products."""

    settings = load_settings()
    _run_stream(settings, settings.products)


def btc_stream_main() -> None:
    """Stream only BTC-USD for backward compatibility."""

    settings = load_settings()
    _run_stream(settings, ("BTC-USD",))


def discover_main() -> None:
    """Discover one public snapshot of Robinhood Live 15-minute events."""

    settings = load_settings()
    configure_logging(settings.log_level)
    contracts = Robinhood15MinuteProvider(settings).discover()
    for contract in contracts:
        logger.info(
            "Robinhood 15-minute contract",
            extra={
                "event": "robinhood_15min_contract",
                "asset": contract.asset,
                "event_id": contract.event_id,
                "contract_id": contract.contract_id,
                "start_time": contract.start_time,
                "end_time": contract.end_time,
                "target_price": contract.target_price,
                "displayed_yes_probability": contract.quote.yes_probability,
                "displayed_no_probability": contract.quote.no_probability,
                "displayed_quote_availability": contract.quote.availability,
                "quote_is_executable": contract.quote.is_executable,
                "quote_data_role": contract.quote.role,
                "venue": contract.venue,
                "venue_candidates": contract.venue_candidates,
                "settlement_benchmark": contract.settlement.benchmark,
                "settlement_method": contract.settlement.method,
                "settlement_decimal_places": contract.settlement.decimal_places,
                "settlement_data_access": contract.settlement.data_access,
                "settlement_data_role": contract.settlement.role,
                "lifecycle_state": contract.lifecycle_state,
                "source_url": contract.source_url,
                "fetched_at": contract.fetched_at,
                "freshness_state": contract.freshness_state,
                "source_age_seconds": contract.source_age_seconds,
            },
        )
