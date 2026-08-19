"""Environment-backed application configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD")


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings shared by collectors and command-line entry points."""

    coinbase_rest_base_url: str = "https://api.exchange.coinbase.com"
    coinbase_websocket_url: str = "wss://ws-feed.exchange.coinbase.com"
    products: tuple[str, ...] = DEFAULT_PRODUCTS
    request_timeout_seconds: float = 10.0
    reconnect_delay_seconds: float = 3.0
    websocket_ping_interval_seconds: float = 20.0
    websocket_ping_timeout_seconds: float = 20.0
    rest_poll_interval_seconds: float = 5.0
    log_level: str = "INFO"


def _positive_float(source: Mapping[str, str], name: str, default: float) -> float:
    value = float(source.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Load settings from LIVE15_* environment variables."""

    source = os.environ if environ is None else environ
    defaults = Settings()
    raw_products = source.get("LIVE15_PRODUCTS", ",".join(DEFAULT_PRODUCTS))
    products = tuple(
        product.strip().upper() for product in raw_products.split(",") if product.strip()
    )
    if not products:
        raise ValueError("LIVE15_PRODUCTS must contain at least one product")

    return Settings(
        coinbase_rest_base_url=source.get(
            "LIVE15_COINBASE_REST_URL", defaults.coinbase_rest_base_url
        ).rstrip("/"),
        coinbase_websocket_url=source.get(
            "LIVE15_COINBASE_WS_URL", defaults.coinbase_websocket_url
        ),
        products=products,
        request_timeout_seconds=_positive_float(
            source, "LIVE15_REQUEST_TIMEOUT_SECONDS", defaults.request_timeout_seconds
        ),
        reconnect_delay_seconds=_positive_float(
            source, "LIVE15_RECONNECT_DELAY_SECONDS", defaults.reconnect_delay_seconds
        ),
        websocket_ping_interval_seconds=_positive_float(
            source,
            "LIVE15_WS_PING_INTERVAL_SECONDS",
            defaults.websocket_ping_interval_seconds,
        ),
        websocket_ping_timeout_seconds=_positive_float(
            source, "LIVE15_WS_PING_TIMEOUT_SECONDS", defaults.websocket_ping_timeout_seconds
        ),
        rest_poll_interval_seconds=_positive_float(
            source, "LIVE15_REST_POLL_INTERVAL_SECONDS", defaults.rest_poll_interval_seconds
        ),
        log_level=source.get("LIVE15_LOG_LEVEL", defaults.log_level).upper(),
    )
