"""Command-line entry points for public market-data collectors."""

from __future__ import annotations

import asyncio
import logging
import time

import requests

from live15_quant.config import Settings, load_settings
from live15_quant.logging_config import configure_logging
from live15_quant.models import MarketTick
from live15_quant.paper_runtime import PaperRuntime
from live15_quant.paper_storage import PaperStore
from live15_quant.providers.coinbase import (
    CoinbasePayloadError,
    CoinbaseRestClient,
    CoinbaseWebSocketClient,
)
from live15_quant.providers.kalshi_demo import (
    KalshiDemoCredentials,
    KalshiDemoReadOnlyClient,
)
from live15_quant.providers.robinhood_15min import Robinhood15MinuteProvider
from live15_quant.recorder import HistoricalRecorder
from live15_quant.storage import RecorderStore

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
        "bid_size": tick.bid_size,
        "ask_size": tick.ask_size,
        "last_size": tick.last_size,
        "volume_24h": tick.volume_24h,
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
    finally:
        client.close()


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
    provider = Robinhood15MinuteProvider(settings)
    try:
        contracts = provider.discover()
    finally:
        provider.close()
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


async def _run_recorder(settings: Settings) -> None:
    with RecorderStore(settings.recorder_data_path) as store:
        await HistoricalRecorder(settings, store).run()


def recorder_main() -> None:
    """Continuously persist public event snapshots and predictive ticks."""

    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        asyncio.run(_run_recorder(settings))
    except KeyboardInterrupt:
        logger.info("Recorder interrupted safely", extra={"event": "recorder_interrupted"})


def paper_main() -> None:
    """Run local-only paper execution; this entry point cannot place real orders."""

    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        with PaperStore(
            settings.paper_data_path,
            account_id=settings.paper_account_id,
            starting_cash=settings.paper_starting_cash,
        ) as store:
            PaperRuntime(settings, store).run()
    except KeyboardInterrupt:
        logger.info("Paper runtime interrupted safely", extra={"event": "paper_interrupted"})


def kalshi_demo_audit_main() -> None:
    """Run the credentialed, GET-only Kalshi Demo connectivity audit."""

    settings = load_settings()
    configure_logging(settings.log_level)
    if settings.kalshi_demo_api_key_id is None or settings.kalshi_demo_private_key_path is None:
        raise SystemExit(
            "Kalshi Demo credentials are not configured. Create a Demo API key, keep its "
            "private key outside the repository, then set LIVE15_KALSHI_DEMO_API_KEY_ID and "
            "LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH."
        )
    credentials = KalshiDemoCredentials(
        api_key_id=settings.kalshi_demo_api_key_id,
        private_key_path=settings.kalshi_demo_private_key_path,
    )
    with KalshiDemoReadOnlyClient(settings, credentials) as client:
        result = client.audit()
    logger.info(
        "Kalshi Demo read-only connectivity audit completed",
        extra={
            "event": "kalshi_demo_audit_complete",
            "environment": result.environment,
            "authenticated": result.authenticated,
            "balance_read": result.balance_dollars is not None,
            "market_count": result.market_count,
            "positions_readable": result.positions_readable,
            "orders_readable": result.orders_readable,
            "fills_readable": result.fills_readable,
            "write_operations_available_in_client": False,
        },
    )
