from __future__ import annotations

from pathlib import Path

import pytest

from live15_quant.config import (
    DEFAULT_PRODUCTS,
    KALSHI_DEMO_API_BASE_URL,
    ROBINHOOD_15MIN_PUBLIC_URL,
    load_settings,
)


def test_load_settings_uses_defaults() -> None:
    settings = load_settings({})

    assert settings.products == DEFAULT_PRODUCTS
    assert settings.request_timeout_seconds == 10.0
    assert settings.robinhood_max_source_age_seconds == 360.0
    assert settings.recorder_data_path == Path("data/live15.sqlite3")
    assert settings.log_level == "INFO"
    assert settings.kalshi_demo_api_key_id is None
    assert settings.kalshi_demo_private_key_path is None


def test_load_settings_normalizes_environment_values() -> None:
    settings = load_settings(
        {
            "LIVE15_PRODUCTS": "btc-usd, eth-usd",
            "LIVE15_COINBASE_REST_URL": "https://example.test/",
            "LIVE15_LOG_LEVEL": "debug",
            "LIVE15_REQUEST_TIMEOUT_SECONDS": "2.5",
            "LIVE15_RECORDER_DATA_PATH": "scratch/test.sqlite3",
            "LIVE15_ROBINHOOD_POLL_INTERVAL_SECONDS": "7.5",
            "LIVE15_OFFICIAL_QUOTE_POLL_INTERVAL_SECONDS": "1.5",
            "LIVE15_OFFICIAL_QUOTE_MAX_SOURCE_AGE_SECONDS": "12",
            "LIVE15_OFFICIAL_QUOTE_ORDERBOOK_DEPTH": "20",
            "LIVE15_PAPER_DATA_PATH": "scratch/paper.sqlite3",
            "LIVE15_PAPER_MAX_ORDER_NOTIONAL": "1.25",
            "LIVE15_PAPER_KILL_SWITCH": "true",
            "LIVE15_ROBINHOOD_15MIN_URL": "https://private.example.test/hidden",
            "LIVE15_KALSHI_DEMO_API_KEY_ID": "demo-key-id",
            "LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH": "C:/safe/kalshi-demo.key",
        }
    )

    assert settings.products == ("BTC-USD", "ETH-USD")
    assert settings.coinbase_rest_base_url == "https://example.test"
    assert settings.log_level == "DEBUG"
    assert settings.request_timeout_seconds == 2.5
    assert settings.recorder_data_path == Path("scratch/test.sqlite3")
    assert settings.robinhood_poll_interval_seconds == 7.5
    assert settings.official_quote_poll_interval_seconds == 1.5
    assert settings.official_quote_max_source_age_seconds == 12
    assert settings.official_quote_orderbook_depth == 20
    assert settings.paper_data_path == Path("scratch/paper.sqlite3")
    assert str(settings.paper_max_order_notional) == "1.25"
    assert settings.paper_kill_switch is True
    assert settings.robinhood_15min_url == ROBINHOOD_15MIN_PUBLIC_URL
    assert settings.kalshi_demo_api_key_id == "demo-key-id"
    assert settings.kalshi_demo_private_key_path == Path("C:/safe/kalshi-demo.key")
    assert settings.kalshi_public_api_base_url != KALSHI_DEMO_API_BASE_URL
    assert "demo-key-id" not in repr(settings)
    assert "kalshi-demo.key" not in repr(settings)


def test_load_settings_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        load_settings({"LIVE15_REQUEST_TIMEOUT_SECONDS": "0"})


def test_load_settings_rejects_non_positive_orderbook_depth() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        load_settings({"LIVE15_OFFICIAL_QUOTE_ORDERBOOK_DEPTH": "0"})


def test_load_settings_rejects_empty_products() -> None:
    with pytest.raises(ValueError, match="at least one product"):
        load_settings({"LIVE15_PRODUCTS": " , "})


def test_load_settings_rejects_ambiguous_kill_switch() -> None:
    with pytest.raises(ValueError, match="true/false"):
        load_settings({"LIVE15_PAPER_KILL_SWITCH": "yes"})


def test_load_settings_rejects_shared_raw_and_paper_database() -> None:
    with pytest.raises(ValueError, match="must be different"):
        load_settings(
            {
                "LIVE15_RECORDER_DATA_PATH": "data/shared.sqlite3",
                "LIVE15_PAPER_DATA_PATH": "data/shared.sqlite3",
            }
        )
