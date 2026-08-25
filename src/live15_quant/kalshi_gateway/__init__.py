"""Thin, fail-closed boundary around the pinned Kalshi SDK."""

from live15_quant.kalshi_gateway.client import (
    GatewayCredentials,
    KalshiEnvironment,
    KalshiGatewayConfig,
    build_sdk_client,
    production_credentials,
    production_runtime_environment,
)
from live15_quant.kalshi_gateway.execution import (
    GatewayOrderIntent,
    KalshiExecutionGateway,
    KalshiWriteDisabledError,
)
from live15_quant.kalshi_gateway.market_data import KalshiMarketDataGateway
from live15_quant.kalshi_gateway.portfolio import KalshiPortfolioGateway
from live15_quant.kalshi_gateway.websocket import KalshiWebSocketGateway

__all__ = [
    "GatewayCredentials",
    "GatewayOrderIntent",
    "KalshiEnvironment",
    "KalshiExecutionGateway",
    "KalshiGatewayConfig",
    "KalshiMarketDataGateway",
    "KalshiPortfolioGateway",
    "KalshiWebSocketGateway",
    "KalshiWriteDisabledError",
    "build_sdk_client",
    "production_credentials",
    "production_runtime_environment",
]
