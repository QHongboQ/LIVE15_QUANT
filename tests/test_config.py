from __future__ import annotations

import pytest

from live15_quant.config import DEFAULT_PRODUCTS, load_settings


def test_load_settings_uses_defaults() -> None:
    settings = load_settings({})

    assert settings.products == DEFAULT_PRODUCTS
    assert settings.request_timeout_seconds == 10.0
    assert settings.log_level == "INFO"


def test_load_settings_normalizes_environment_values() -> None:
    settings = load_settings(
        {
            "LIVE15_PRODUCTS": "btc-usd, eth-usd",
            "LIVE15_COINBASE_REST_URL": "https://example.test/",
            "LIVE15_LOG_LEVEL": "debug",
            "LIVE15_REQUEST_TIMEOUT_SECONDS": "2.5",
        }
    )

    assert settings.products == ("BTC-USD", "ETH-USD")
    assert settings.coinbase_rest_base_url == "https://example.test"
    assert settings.log_level == "DEBUG"
    assert settings.request_timeout_seconds == 2.5


def test_load_settings_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        load_settings({"LIVE15_REQUEST_TIMEOUT_SECONDS": "0"})


def test_load_settings_rejects_empty_products() -> None:
    with pytest.raises(ValueError, match="at least one product"):
        load_settings({"LIVE15_PRODUCTS": " , "})
