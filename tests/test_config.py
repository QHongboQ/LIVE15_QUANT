from __future__ import annotations

from pathlib import Path

import pytest

from live15_quant.config import (
    DEFAULT_DATASET_DECISION_OFFSETS_SECONDS,
    DEFAULT_PRODUCTS,
    KALSHI_DEMO_API_BASE_URL,
    PYTH_HERMES_BASE_URL,
    ROBINHOOD_15MIN_PUBLIC_URL,
    load_settings,
)


def test_load_settings_uses_defaults() -> None:
    settings = load_settings({})

    assert settings.products == DEFAULT_PRODUCTS
    assert settings.request_timeout_seconds == 10.0
    assert settings.robinhood_max_source_age_seconds == 360.0
    assert settings.recorder_data_path == Path("data/live15.sqlite3")
    assert settings.recorder_control_path == Path("data/recorder-control.json")
    assert settings.recorder_pid_path == Path("data/recorder.pid")
    assert settings.log_level == "INFO"
    assert settings.kalshi_demo_api_key_id is None
    assert settings.kalshi_demo_private_key_path is None
    assert settings.enable_robinhood_reference is False
    assert settings.feature_store_path == Path("data/features.sqlite3")
    assert settings.ui_port == 8765
    assert settings.ui_heartbeat_stale_seconds == 90.0
    assert settings.dataset_decision_offsets_seconds == DEFAULT_DATASET_DECISION_OFFSETS_SECONDS
    assert settings.native_discovery_poll_interval_seconds == 15
    assert settings.settlement_followup_batch_size == 25
    assert settings.dataset_build_interval_seconds is None
    assert settings.recorder_max_backoff_seconds == 60
    assert settings.enable_pyth_underlying is False
    assert settings.enable_secondary_underlying is False
    assert settings.pyth_api_key_path is None
    assert settings.pyth_hermes_base_url == PYTH_HERMES_BASE_URL


def test_load_settings_normalizes_environment_values() -> None:
    settings = load_settings(
        {
            "LIVE15_PRODUCTS": "btc-usd, eth-usd",
            "LIVE15_COINBASE_REST_URL": "https://example.test/",
            "LIVE15_LOG_LEVEL": "debug",
            "LIVE15_REQUEST_TIMEOUT_SECONDS": "2.5",
            "LIVE15_RECORDER_DATA_PATH": "scratch/test.sqlite3",
            "LIVE15_ROBINHOOD_POLL_INTERVAL_SECONDS": "7.5",
            "LIVE15_ENABLE_ROBINHOOD_REFERENCE": "true",
            "LIVE15_OFFICIAL_QUOTE_POLL_INTERVAL_SECONDS": "1.5",
            "LIVE15_OFFICIAL_QUOTE_MAX_SOURCE_AGE_SECONDS": "12",
            "LIVE15_OFFICIAL_QUOTE_ORDERBOOK_DEPTH": "20",
            "LIVE15_PAPER_DATA_PATH": "scratch/paper.sqlite3",
            "LIVE15_PAPER_MAX_ORDER_NOTIONAL": "1.25",
            "LIVE15_PAPER_KILL_SWITCH": "true",
            "LIVE15_FEATURE_STORE_PATH": "scratch/features.sqlite3",
            "LIVE15_UI_PORT": "9123",
            "LIVE15_UI_HEARTBEAT_STALE_SECONDS": "45",
            "LIVE15_DATASET_DECISION_OFFSETS_SECONDS": "600,60,30",
            "LIVE15_DATASET_QUOTE_MAX_AGE_SECONDS": "9",
            "LIVE15_DATASET_UNDERLYING_MAX_AGE_SECONDS": "8",
            "LIVE15_NATIVE_DISCOVERY_POLL_INTERVAL_SECONDS": "4",
            "LIVE15_SETTLEMENT_FOLLOWUP_INTERVAL_SECONDS": "6",
            "LIVE15_SETTLEMENT_FOLLOWUP_BATCH_SIZE": "12",
            "LIVE15_RECORDER_OPERATION_TIMEOUT_SECONDS": "14",
            "LIVE15_RECORDER_MAX_BACKOFF_SECONDS": "55",
            "LIVE15_RECORDER_CHECKPOINT_INTERVAL_SECONDS": "20",
            "LIVE15_RECORDER_HEALTH_PATH": "scratch/health.json",
            "LIVE15_RECORDER_CONTROL_PATH": "scratch/control.json",
            "LIVE15_RECORDER_PID_PATH": "scratch/recorder.pid",
            "LIVE15_DATASET_BUILD_INTERVAL_SECONDS": "3600",
            "LIVE15_ROBINHOOD_15MIN_URL": "https://private.example.test/hidden",
            "LIVE15_KALSHI_DEMO_API_KEY_ID": "demo-key-id",
            "LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH": "C:/safe/kalshi-demo.key",
            "LIVE15_ENABLE_PYTH_UNDERLYING": "true",
            "LIVE15_PYTH_API_KEY_PATH": "C:/safe/pyth.key",
            "LIVE15_PYTH_HERMES_URL": "https://untrusted.example",
            "LIVE15_PYTH_REST_FALLBACK_INTERVAL_SECONDS": "2.5",
            "LIVE15_PYTH_STREAM_READ_TIMEOUT_SECONDS": "22",
            "LIVE15_PYTH_REQUEST_BUDGET_PER_10_SECONDS": "7",
            "LIVE15_ENABLE_SECONDARY_UNDERLYING": "true",
            "LIVE15_RECORDER_SECONDARY_STALE_SECONDS": "6",
        }
    )

    assert settings.products == ("BTC-USD", "ETH-USD")
    assert settings.coinbase_rest_base_url == "https://example.test"
    assert settings.log_level == "DEBUG"
    assert settings.request_timeout_seconds == 2.5
    assert settings.recorder_data_path == Path("scratch/test.sqlite3")
    assert settings.robinhood_poll_interval_seconds == 7.5
    assert settings.enable_robinhood_reference is True
    assert settings.official_quote_poll_interval_seconds == 1.5
    assert settings.official_quote_max_source_age_seconds == 12
    assert settings.official_quote_orderbook_depth == 20
    assert settings.paper_data_path == Path("scratch/paper.sqlite3")
    assert str(settings.paper_max_order_notional) == "1.25"
    assert settings.paper_kill_switch is True
    assert settings.feature_store_path == Path("scratch/features.sqlite3")
    assert settings.ui_port == 9123
    assert settings.ui_heartbeat_stale_seconds == 45.0
    assert settings.dataset_decision_offsets_seconds == (600, 60, 30)
    assert settings.dataset_quote_max_age_seconds == 9
    assert settings.dataset_underlying_max_age_seconds == 8
    assert settings.native_discovery_poll_interval_seconds == 4
    assert settings.settlement_followup_interval_seconds == 6
    assert settings.settlement_followup_batch_size == 12
    assert settings.recorder_operation_timeout_seconds == 14
    assert settings.recorder_max_backoff_seconds == 55
    assert settings.recorder_checkpoint_interval_seconds == 20
    assert settings.recorder_health_path == Path("scratch/health.json")
    assert settings.recorder_control_path == Path("scratch/control.json")
    assert settings.recorder_pid_path == Path("scratch/recorder.pid")
    assert settings.dataset_build_interval_seconds == 3600
    assert settings.robinhood_15min_url == ROBINHOOD_15MIN_PUBLIC_URL
    assert settings.kalshi_demo_api_key_id == "demo-key-id"
    assert settings.kalshi_demo_private_key_path == Path("C:/safe/kalshi-demo.key")
    assert settings.kalshi_public_api_base_url != KALSHI_DEMO_API_BASE_URL
    assert "demo-key-id" not in repr(settings)
    assert "kalshi-demo.key" not in repr(settings)
    assert settings.enable_pyth_underlying is True
    assert settings.pyth_api_key_path == Path("C:/safe/pyth.key")
    assert settings.pyth_hermes_base_url == PYTH_HERMES_BASE_URL
    assert settings.enable_secondary_underlying is True
    assert settings.recorder_secondary_stale_seconds == 6
    assert settings.pyth_rest_fallback_interval_seconds == 2.5
    assert settings.pyth_stream_read_timeout_seconds == 22
    assert settings.pyth_request_budget_per_10_seconds == 7
    assert "pyth.key" not in repr(settings)


def test_load_settings_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        load_settings({"LIVE15_REQUEST_TIMEOUT_SECONDS": "0"})


def test_load_settings_rejects_pyth_budget_above_official_limit() -> None:
    with pytest.raises(ValueError, match="at most 10"):
        load_settings({"LIVE15_PYTH_REQUEST_BUDGET_PER_10_SECONDS": "11"})


def test_load_settings_rejects_non_positive_orderbook_depth() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        load_settings({"LIVE15_OFFICIAL_QUOTE_ORDERBOOK_DEPTH": "0"})


def test_load_settings_rejects_non_positive_optional_dataset_interval() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        load_settings({"LIVE15_DATASET_BUILD_INTERVAL_SECONDS": "0"})


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


@pytest.mark.parametrize("value", ("", "0", "901", "60,60", "sixty"))
def test_load_settings_rejects_invalid_dataset_offsets(value: str) -> None:
    with pytest.raises(ValueError, match="DECISION_OFFSETS"):
        load_settings({"LIVE15_DATASET_DECISION_OFFSETS_SECONDS": value})


def test_load_settings_rejects_feature_store_shared_with_raw_data() -> None:
    with pytest.raises(ValueError, match="must be different"):
        load_settings(
            {
                "LIVE15_RECORDER_DATA_PATH": "data/shared.sqlite3",
                "LIVE15_FEATURE_STORE_PATH": "data/shared.sqlite3",
            }
        )


def test_load_settings_rejects_health_file_shared_with_database() -> None:
    with pytest.raises(ValueError, match="must be different"):
        load_settings(
            {
                "LIVE15_RECORDER_DATA_PATH": "data/shared.sqlite3",
                "LIVE15_RECORDER_HEALTH_PATH": "data/shared.sqlite3",
            }
        )
