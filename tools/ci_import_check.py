"""Import the production module surface exercised by CI code validation."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

MODULES = (
    "live15_quant",
    "live15_quant.backfill",
    "live15_quant.control_center",
    "live15_quant.control_center_launcher",
    "live15_quant.control_center_models",
    "live15_quant.control_center_service",
    "live15_quant.control_center_store",
    "live15_quant.dataset",
    "live15_quant.execution",
    "live15_quant.feature_registry",
    "live15_quant.features",
    "live15_quant.fees",
    "live15_quant.kalshi_lifecycle",
    "live15_quant.kalshi_ws",
    "live15_quant.managed_recorder",
    "live15_quant.native_acceptance",
    "live15_quant.native_recorder",
    "live15_quant.normalization",
    "live15_quant.paper",
    "live15_quant.paper_acceptance",
    "live15_quant.paper_execution",
    "live15_quant.paper_runtime",
    "live15_quant.paper_storage",
    "live15_quant.providers.coinbase",
    "live15_quant.providers.kalshi",
    "live15_quant.providers.kalshi_demo",
    "live15_quant.providers.kalshi_ws",
    "live15_quant.providers.robinhood_15min",
    "live15_quant.recorder",
    "live15_quant.recorder_control",
    "live15_quant.records",
    "live15_quant.replay",
    "live15_quant.risk",
    "live15_quant.settlement",
    "live15_quant.splits",
    "live15_quant.storage",
    "btc_price_test",
    "btc_stream",
    "market_stream",
)


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    for module in MODULES:
        importlib.import_module(module)


if __name__ == "__main__":
    main()
