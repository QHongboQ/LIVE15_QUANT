"""Environment-backed application configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

DEFAULT_PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD")
DEFAULT_DATASET_DECISION_OFFSETS_SECONDS = (840, 720, 600, 480, 300, 180, 120, 60, 30)
ROBINHOOD_15MIN_PUBLIC_URL = "https://robinhood.com/us/en/prediction-markets/15-min/"
KALSHI_PUBLIC_API_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_DEMO_API_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
KALSHI_PRODUCTION_WEBSOCKET_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
KALSHI_DEMO_WEBSOCKET_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
PYTH_HERMES_BASE_URL = "https://hermes.pyth.network"


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings shared by collectors and command-line entry points."""

    coinbase_rest_base_url: str = "https://api.exchange.coinbase.com"
    coinbase_websocket_url: str = "wss://ws-feed.exchange.coinbase.com"
    robinhood_15min_url: str = ROBINHOOD_15MIN_PUBLIC_URL
    products: tuple[str, ...] = DEFAULT_PRODUCTS
    request_timeout_seconds: float = 10.0
    reconnect_delay_seconds: float = 3.0
    websocket_ping_interval_seconds: float = 20.0
    websocket_ping_timeout_seconds: float = 20.0
    rest_poll_interval_seconds: float = 5.0
    robinhood_max_source_age_seconds: float = 360.0
    robinhood_poll_interval_seconds: float = 15.0
    enable_robinhood_reference: bool = False
    kalshi_public_api_base_url: str = KALSHI_PUBLIC_API_BASE_URL
    kalshi_demo_api_key_id: str | None = field(default=None, repr=False)
    kalshi_demo_private_key_path: Path | None = field(default=None, repr=False)
    official_quote_poll_interval_seconds: float = 2.0
    official_quote_max_source_age_seconds: float = 15.0
    official_quote_orderbook_depth: int = 10
    recorder_data_path: Path = Path("data/live15.sqlite3")
    recorder_health_interval_seconds: float = 30.0
    recorder_coinbase_stale_seconds: float = 30.0
    enable_pyth_underlying: bool = False
    pyth_hermes_base_url: str = PYTH_HERMES_BASE_URL
    pyth_api_key_path: Path | None = field(default=None, repr=False)
    pyth_rest_fallback_interval_seconds: float = 2.0
    pyth_stream_read_timeout_seconds: float = 20.0
    pyth_request_budget_per_10_seconds: int = 8
    recorder_pyth_stale_seconds: float = 15.0
    enable_secondary_underlying: bool = False
    recorder_secondary_stale_seconds: float = 10.0
    native_discovery_poll_interval_seconds: float = 15.0
    settlement_followup_interval_seconds: float = 15.0
    settlement_followup_batch_size: int = 25
    recorder_checkpoint_interval_seconds: float = 300.0
    recorder_operation_timeout_seconds: float = 45.0
    recorder_max_backoff_seconds: float = 60.0
    recorder_health_path: Path = Path("data/health.json")
    recorder_control_path: Path = Path("data/recorder-control.json")
    recorder_pid_path: Path = Path("data/recorder.pid")
    ui_port: int = 8765
    ui_heartbeat_stale_seconds: float = 90.0
    dataset_build_interval_seconds: float | None = None
    feature_store_path: Path = Path("data/features.sqlite3")
    readiness_report_path: Path = Path("data/readiness.json")
    dataset_decision_offsets_seconds: tuple[int, ...] = DEFAULT_DATASET_DECISION_OFFSETS_SECONDS
    dataset_quote_max_age_seconds: float = 15.0
    dataset_underlying_max_age_seconds: float = 15.0
    paper_data_path: Path = Path("data/paper.sqlite3")
    paper_account_id: str = "local-paper"
    paper_starting_cash: Decimal = Decimal("1000")
    paper_signal_interval_seconds: float = 90.0
    paper_max_order_notional: Decimal = Decimal("10")
    paper_max_event_exposure: Decimal = Decimal("25")
    paper_max_daily_loss: Decimal = Decimal("20")
    paper_max_total_exposure: Decimal = Decimal("100")
    paper_max_consecutive_losses: int = 3
    paper_kill_switch: bool = False
    log_level: str = "INFO"


def _positive_float(source: Mapping[str, str], name: str, default: float) -> float:
    value = float(source.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_int(source: Mapping[str, str], name: str, default: int) -> int:
    value = int(source.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _bounded_positive_int(source: Mapping[str, str], name: str, default: int, maximum: int) -> int:
    value = _positive_int(source, name, default)
    if value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _positive_decimal(source: Mapping[str, str], name: str, default: Decimal) -> Decimal:
    try:
        value = Decimal(source.get(name, str(default)))
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a decimal") from error
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _decision_offsets(source: Mapping[str, str]) -> tuple[int, ...]:
    raw = source.get(
        "LIVE15_DATASET_DECISION_OFFSETS_SECONDS",
        ",".join(str(value) for value in DEFAULT_DATASET_DECISION_OFFSETS_SECONDS),
    )
    try:
        values = tuple(int(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as error:
        raise ValueError("LIVE15_DATASET_DECISION_OFFSETS_SECONDS must contain integers") from error
    if (
        not values
        or len(set(values)) != len(values)
        or any(value <= 0 or value > 900 for value in values)
    ):
        raise ValueError(
            "LIVE15_DATASET_DECISION_OFFSETS_SECONDS must contain unique values in 1..900"
        )
    return values


def _boolean(source: Mapping[str, str], name: str, default: bool) -> bool:
    raw = source.get(name, str(default)).strip().lower()
    if raw not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true/false or 1/0")
    return raw in {"true", "1"}


def _optional_positive_float(
    source: Mapping[str, str], name: str, default: float | None
) -> float | None:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive when configured")
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
    recorder_data_path = Path(
        source.get("LIVE15_RECORDER_DATA_PATH", str(defaults.recorder_data_path))
    )
    paper_data_path = Path(source.get("LIVE15_PAPER_DATA_PATH", str(defaults.paper_data_path)))
    feature_store_path = Path(
        source.get("LIVE15_FEATURE_STORE_PATH", str(defaults.feature_store_path))
    )
    readiness_report_path = Path(
        source.get("LIVE15_READINESS_REPORT_PATH", str(defaults.readiness_report_path))
    )
    recorder_health_path = Path(
        source.get("LIVE15_RECORDER_HEALTH_PATH", str(defaults.recorder_health_path))
    )
    recorder_control_path = Path(
        source.get("LIVE15_RECORDER_CONTROL_PATH", str(defaults.recorder_control_path))
    )
    recorder_pid_path = Path(
        source.get("LIVE15_RECORDER_PID_PATH", str(defaults.recorder_pid_path))
    )
    pyth_api_key_path = (
        Path(source["LIVE15_PYTH_API_KEY_PATH"]) if source.get("LIVE15_PYTH_API_KEY_PATH") else None
    )
    resolved_paths = {
        recorder_data_path.resolve(),
        paper_data_path.resolve(),
        feature_store_path.resolve(),
        recorder_health_path.resolve(),
        recorder_control_path.resolve(),
        recorder_pid_path.resolve(),
        readiness_report_path.resolve(),
    }
    if len(resolved_paths) != 7:
        raise ValueError("database and recorder runtime paths must be different from each other")
    paper_account_id = source.get("LIVE15_PAPER_ACCOUNT_ID", defaults.paper_account_id).strip()
    if not paper_account_id:
        raise ValueError("LIVE15_PAPER_ACCOUNT_ID must not be empty")

    return Settings(
        coinbase_rest_base_url=source.get(
            "LIVE15_COINBASE_REST_URL", defaults.coinbase_rest_base_url
        ).rstrip("/"),
        coinbase_websocket_url=source.get(
            "LIVE15_COINBASE_WS_URL", defaults.coinbase_websocket_url
        ),
        robinhood_15min_url=defaults.robinhood_15min_url,
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
        robinhood_max_source_age_seconds=_positive_float(
            source,
            "LIVE15_ROBINHOOD_MAX_SOURCE_AGE_SECONDS",
            defaults.robinhood_max_source_age_seconds,
        ),
        robinhood_poll_interval_seconds=_positive_float(
            source,
            "LIVE15_ROBINHOOD_POLL_INTERVAL_SECONDS",
            defaults.robinhood_poll_interval_seconds,
        ),
        enable_robinhood_reference=_boolean(
            source,
            "LIVE15_ENABLE_ROBINHOOD_REFERENCE",
            defaults.enable_robinhood_reference,
        ),
        kalshi_public_api_base_url=KALSHI_PUBLIC_API_BASE_URL,
        kalshi_demo_api_key_id=source.get("LIVE15_KALSHI_DEMO_API_KEY_ID") or None,
        kalshi_demo_private_key_path=(
            Path(source["LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH"])
            if source.get("LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH")
            else None
        ),
        official_quote_poll_interval_seconds=_positive_float(
            source,
            "LIVE15_OFFICIAL_QUOTE_POLL_INTERVAL_SECONDS",
            defaults.official_quote_poll_interval_seconds,
        ),
        official_quote_max_source_age_seconds=_positive_float(
            source,
            "LIVE15_OFFICIAL_QUOTE_MAX_SOURCE_AGE_SECONDS",
            defaults.official_quote_max_source_age_seconds,
        ),
        official_quote_orderbook_depth=_positive_int(
            source,
            "LIVE15_OFFICIAL_QUOTE_ORDERBOOK_DEPTH",
            defaults.official_quote_orderbook_depth,
        ),
        recorder_data_path=recorder_data_path,
        recorder_health_interval_seconds=_positive_float(
            source,
            "LIVE15_RECORDER_HEALTH_INTERVAL_SECONDS",
            defaults.recorder_health_interval_seconds,
        ),
        recorder_coinbase_stale_seconds=_positive_float(
            source,
            "LIVE15_RECORDER_COINBASE_STALE_SECONDS",
            defaults.recorder_coinbase_stale_seconds,
        ),
        enable_pyth_underlying=_boolean(
            source, "LIVE15_ENABLE_PYTH_UNDERLYING", defaults.enable_pyth_underlying
        ),
        pyth_hermes_base_url=PYTH_HERMES_BASE_URL,
        pyth_api_key_path=pyth_api_key_path,
        pyth_rest_fallback_interval_seconds=_positive_float(
            source,
            "LIVE15_PYTH_REST_FALLBACK_INTERVAL_SECONDS",
            defaults.pyth_rest_fallback_interval_seconds,
        ),
        pyth_stream_read_timeout_seconds=_positive_float(
            source,
            "LIVE15_PYTH_STREAM_READ_TIMEOUT_SECONDS",
            defaults.pyth_stream_read_timeout_seconds,
        ),
        pyth_request_budget_per_10_seconds=_bounded_positive_int(
            source,
            "LIVE15_PYTH_REQUEST_BUDGET_PER_10_SECONDS",
            defaults.pyth_request_budget_per_10_seconds,
            10,
        ),
        recorder_pyth_stale_seconds=_positive_float(
            source, "LIVE15_RECORDER_PYTH_STALE_SECONDS", defaults.recorder_pyth_stale_seconds
        ),
        enable_secondary_underlying=_boolean(
            source,
            "LIVE15_ENABLE_SECONDARY_UNDERLYING",
            defaults.enable_secondary_underlying,
        ),
        recorder_secondary_stale_seconds=_positive_float(
            source,
            "LIVE15_RECORDER_SECONDARY_STALE_SECONDS",
            defaults.recorder_secondary_stale_seconds,
        ),
        native_discovery_poll_interval_seconds=_positive_float(
            source,
            "LIVE15_NATIVE_DISCOVERY_POLL_INTERVAL_SECONDS",
            defaults.native_discovery_poll_interval_seconds,
        ),
        settlement_followup_interval_seconds=_positive_float(
            source,
            "LIVE15_SETTLEMENT_FOLLOWUP_INTERVAL_SECONDS",
            defaults.settlement_followup_interval_seconds,
        ),
        settlement_followup_batch_size=_positive_int(
            source,
            "LIVE15_SETTLEMENT_FOLLOWUP_BATCH_SIZE",
            defaults.settlement_followup_batch_size,
        ),
        recorder_checkpoint_interval_seconds=_positive_float(
            source,
            "LIVE15_RECORDER_CHECKPOINT_INTERVAL_SECONDS",
            defaults.recorder_checkpoint_interval_seconds,
        ),
        recorder_operation_timeout_seconds=_positive_float(
            source,
            "LIVE15_RECORDER_OPERATION_TIMEOUT_SECONDS",
            defaults.recorder_operation_timeout_seconds,
        ),
        recorder_max_backoff_seconds=_positive_float(
            source,
            "LIVE15_RECORDER_MAX_BACKOFF_SECONDS",
            defaults.recorder_max_backoff_seconds,
        ),
        recorder_health_path=recorder_health_path,
        recorder_control_path=recorder_control_path,
        recorder_pid_path=recorder_pid_path,
        ui_port=_positive_int(source, "LIVE15_UI_PORT", defaults.ui_port),
        ui_heartbeat_stale_seconds=_positive_float(
            source,
            "LIVE15_UI_HEARTBEAT_STALE_SECONDS",
            defaults.ui_heartbeat_stale_seconds,
        ),
        dataset_build_interval_seconds=_optional_positive_float(
            source,
            "LIVE15_DATASET_BUILD_INTERVAL_SECONDS",
            defaults.dataset_build_interval_seconds,
        ),
        feature_store_path=feature_store_path,
        readiness_report_path=readiness_report_path,
        dataset_decision_offsets_seconds=_decision_offsets(source),
        dataset_quote_max_age_seconds=_positive_float(
            source,
            "LIVE15_DATASET_QUOTE_MAX_AGE_SECONDS",
            defaults.dataset_quote_max_age_seconds,
        ),
        dataset_underlying_max_age_seconds=_positive_float(
            source,
            "LIVE15_DATASET_UNDERLYING_MAX_AGE_SECONDS",
            defaults.dataset_underlying_max_age_seconds,
        ),
        paper_data_path=paper_data_path,
        paper_account_id=paper_account_id,
        paper_starting_cash=_positive_decimal(
            source, "LIVE15_PAPER_STARTING_CASH", defaults.paper_starting_cash
        ),
        paper_signal_interval_seconds=_positive_float(
            source,
            "LIVE15_PAPER_SIGNAL_INTERVAL_SECONDS",
            defaults.paper_signal_interval_seconds,
        ),
        paper_max_order_notional=_positive_decimal(
            source, "LIVE15_PAPER_MAX_ORDER_NOTIONAL", defaults.paper_max_order_notional
        ),
        paper_max_event_exposure=_positive_decimal(
            source, "LIVE15_PAPER_MAX_EVENT_EXPOSURE", defaults.paper_max_event_exposure
        ),
        paper_max_daily_loss=_positive_decimal(
            source, "LIVE15_PAPER_MAX_DAILY_LOSS", defaults.paper_max_daily_loss
        ),
        paper_max_total_exposure=_positive_decimal(
            source, "LIVE15_PAPER_MAX_TOTAL_EXPOSURE", defaults.paper_max_total_exposure
        ),
        paper_max_consecutive_losses=_positive_int(
            source,
            "LIVE15_PAPER_MAX_CONSECUTIVE_LOSSES",
            defaults.paper_max_consecutive_losses,
        ),
        paper_kill_switch=_boolean(source, "LIVE15_PAPER_KILL_SWITCH", defaults.paper_kill_switch),
        log_level=source.get("LIVE15_LOG_LEVEL", defaults.log_level).upper(),
    )
