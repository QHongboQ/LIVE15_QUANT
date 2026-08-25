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
    kalshi_demo_api_key_id_file: Path | None = field(default=None, repr=False)
    kalshi_demo_private_key_path: Path | None = field(default=None, repr=False)
    enable_kalshi_production_websocket: bool = False
    # The transport selection is explicit so two authoritative writers can
    # never be started through an implicit fallback.
    kalshi_recorder_provider: str = "legacy"
    kalshi_production_api_key_id_path: Path | None = field(default=None, repr=False)
    kalshi_production_private_key_path: Path | None = field(default=None, repr=False)
    kalshi_websocket_read_timeout_seconds: float = 45.0
    kalshi_websocket_stale_seconds: float = 10.0
    kalshi_websocket_queue_capacity: int = 8192
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
    enable_ws_archive: bool = True
    ws_archive_root: Path | None = None
    ws_archive_manifest_path: Path | None = None
    ws_archive_hot_retention_seconds: float = 21_600.0
    # Keep live archive I/O bursts below the synchronized WS persistence queue budget.
    ws_archive_chunk_records: int = 10_000
    ws_archive_poll_interval_seconds: float = 2.0
    ws_archive_shadow_chunks: int = 3
    ws_archive_purge_batch_rows: int = 20_000
    ws_compaction_min_reclaim_bytes: int = 8 * 1024**3
    ws_compaction_min_reclaim_percent: Decimal = Decimal("25")
    enable_adaptive_ws_retention: bool = True
    adaptive_retention_state_path: Path | None = None
    adaptive_retention_status_path: Path | None = None
    adaptive_retention_min_seconds: int = 3_600
    adaptive_retention_max_seconds: int = 21_600
    adaptive_retention_evidence_window_seconds: int = 7 * 86_400
    adaptive_retention_min_evidence_seconds: int = 3 * 86_400
    adaptive_retention_min_verified_chunks: int = 100
    adaptive_retention_min_evidence_samples: int = 24
    adaptive_retention_min_recovery_sessions: int = 3
    adaptive_retention_simulation_passes: int = 3
    adaptive_retention_safety_margin_seconds: int = 1_800
    adaptive_retention_cooldown_seconds: int = 86_400
    adaptive_retention_reevaluation_seconds: int = 3_600
    adaptive_retention_incident_quiet_seconds: int = 86_400
    adaptive_retention_min_projection_window_seconds: int = 86_400
    adaptive_retention_disk_deescalation_samples: int = 3
    adaptive_retention_auto_adjust: bool = True
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
    readiness_snapshot_max_seconds: float = 300.0
    dataset_decision_offsets_seconds: tuple[int, ...] = DEFAULT_DATASET_DECISION_OFFSETS_SECONDS
    dataset_quote_max_age_seconds: float = 15.0
    dataset_underlying_max_age_seconds: float = 15.0
    # Mutable, non-artifact trainability projection.  This never triggers training.
    current_trainable_path: Path = Path("data/current_trainable.sqlite3")
    current_trainable_poll_interval_seconds: float = 300.0
    current_trainable_active_poll_interval_seconds: float = 5.0
    current_trainable_batch_events: int = 25
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
    # Forward shadow validation is local-only.  It is intentionally separate from
    # both the raw recorder and the pre-existing exploratory paper ledger.
    forward_shadow_data_path: Path = Path("data/forward-shadow.sqlite3")
    forward_shadow_paper_root: Path = Path("data/forward-paper")
    forward_shadow_model_zoo_v2_path: Path = Path(
        "data/model_zoo_v2/model_zoo_v2/live15-model-zoo-v2-11f5eb6ff68f3da1391c"
    )
    forward_shadow_dataset_path: Path = Path("data/datasets/live15-dataset-v1-f81d7d1feebcbbaecff9")
    forward_shadow_model_root: Path = Path("data/forward-models")
    forward_shadow_starting_cash: Decimal = Decimal("1000")
    forward_shadow_order_quantity: Decimal = Decimal("1")
    forward_shadow_poll_interval_seconds: float = 1.0
    forward_shadow_decision_grace_seconds: float = 10.0
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.kalshi_recorder_provider not in {"legacy", "sdk"}:
            raise ValueError("kalshi recorder provider must be legacy or sdk")
        if (
            self.forward_shadow_starting_cash <= 0
            or self.forward_shadow_order_quantity <= 0
            or self.forward_shadow_poll_interval_seconds <= 0
            or self.forward_shadow_decision_grace_seconds <= 0
        ):
            raise ValueError("forward shadow configuration must be positive")
        if (
            self.current_trainable_poll_interval_seconds <= 0
            or self.current_trainable_active_poll_interval_seconds <= 0
            or self.current_trainable_batch_events <= 0
        ):
            raise ValueError("current trainable materializer configuration must be positive")
        ladder = (21_600, 14_400, 10_800, 7_200, 3_600)
        if self.enable_adaptive_ws_retention and (
            self.adaptive_retention_min_seconds not in ladder
            or self.adaptive_retention_max_seconds not in ladder
            or self.adaptive_retention_min_seconds > self.adaptive_retention_max_seconds
            or self.ws_archive_hot_retention_seconds not in ladder
            or not (
                self.adaptive_retention_min_seconds
                <= self.ws_archive_hot_retention_seconds
                <= self.adaptive_retention_max_seconds
            )
            or not (
                0
                < self.adaptive_retention_min_evidence_seconds
                <= self.adaptive_retention_evidence_window_seconds
            )
            or self.adaptive_retention_min_verified_chunks < 1
            or self.adaptive_retention_min_evidence_samples < 2
            or self.adaptive_retention_min_recovery_sessions < 1
            or self.adaptive_retention_simulation_passes < 1
            or self.adaptive_retention_safety_margin_seconds < 0
            or self.adaptive_retention_cooldown_seconds < 0
            or self.adaptive_retention_reevaluation_seconds < 1
            or self.adaptive_retention_incident_quiet_seconds < 0
            or self.adaptive_retention_min_projection_window_seconds < 1
            or self.adaptive_retention_disk_deescalation_samples < 1
        ):
            raise ValueError("adaptive WS retention configuration is outside the safety ladder")


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


def _percentage_decimal(source: Mapping[str, str], name: str, default: Decimal) -> Decimal:
    value = _positive_decimal(source, name, default)
    if value > 100:
        raise ValueError(f"{name} must be at most 100")
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


def _recorder_provider(source: Mapping[str, str], default: str) -> str:
    value = source.get("LIVE15_KALSHI_RECORDER_PROVIDER", default).strip().lower()
    if value not in {"legacy", "sdk"}:
        raise ValueError("LIVE15_KALSHI_RECORDER_PROVIDER must be legacy or sdk")
    return value


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
    forward_shadow_data_path = Path(
        source.get("LIVE15_FORWARD_SHADOW_DATA_PATH", str(defaults.forward_shadow_data_path))
    )
    forward_shadow_paper_root = Path(
        source.get("LIVE15_FORWARD_SHADOW_PAPER_ROOT", str(defaults.forward_shadow_paper_root))
    )
    forward_shadow_model_zoo_v2_path = Path(
        source.get(
            "LIVE15_FORWARD_SHADOW_MODEL_ZOO_V2_PATH",
            str(defaults.forward_shadow_model_zoo_v2_path),
        )
    )
    forward_shadow_dataset_path = Path(
        source.get("LIVE15_FORWARD_SHADOW_DATASET_PATH", str(defaults.forward_shadow_dataset_path))
    )
    forward_shadow_model_root = Path(
        source.get("LIVE15_FORWARD_SHADOW_MODEL_ROOT", str(defaults.forward_shadow_model_root))
    )
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
    ws_archive_root = (
        Path(source["LIVE15_WS_ARCHIVE_ROOT"]) if source.get("LIVE15_WS_ARCHIVE_ROOT") else None
    )
    ws_archive_manifest_path = (
        Path(source["LIVE15_WS_ARCHIVE_MANIFEST_PATH"])
        if source.get("LIVE15_WS_ARCHIVE_MANIFEST_PATH")
        else None
    )
    adaptive_retention_state_path = (
        Path(source["LIVE15_ADAPTIVE_RETENTION_STATE_PATH"])
        if source.get("LIVE15_ADAPTIVE_RETENTION_STATE_PATH")
        else None
    )
    adaptive_retention_status_path = (
        Path(source["LIVE15_ADAPTIVE_RETENTION_STATUS_PATH"])
        if source.get("LIVE15_ADAPTIVE_RETENTION_STATUS_PATH")
        else None
    )
    pyth_api_key_path = (
        Path(source["LIVE15_PYTH_API_KEY_PATH"]) if source.get("LIVE15_PYTH_API_KEY_PATH") else None
    )
    effective_adaptive_state_path = adaptive_retention_state_path or (
        recorder_data_path.parent / "adaptive-retention.sqlite3"
    )
    effective_adaptive_status_path = adaptive_retention_status_path or (
        recorder_data_path.parent / "adaptive-retention.json"
    )
    resolved_paths = {
        recorder_data_path.resolve(),
        paper_data_path.resolve(),
        forward_shadow_data_path.resolve(),
        feature_store_path.resolve(),
        recorder_health_path.resolve(),
        recorder_control_path.resolve(),
        recorder_pid_path.resolve(),
        readiness_report_path.resolve(),
        effective_adaptive_state_path.resolve(),
        effective_adaptive_status_path.resolve(),
    }
    if ws_archive_manifest_path is not None:
        resolved_paths.add(ws_archive_manifest_path.resolve())
    expected_paths = 10 + (1 if ws_archive_manifest_path is not None else 0)
    if len(resolved_paths) != expected_paths:
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
        kalshi_demo_api_key_id_file=(
            Path(source["LIVE15_KALSHI_DEMO_API_KEY_ID_FILE"])
            if source.get("LIVE15_KALSHI_DEMO_API_KEY_ID_FILE")
            else None
        ),
        kalshi_demo_private_key_path=(
            Path(source["LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH"])
            if source.get("LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH")
            else None
        ),
        enable_kalshi_production_websocket=_boolean(
            source,
            "LIVE15_ENABLE_KALSHI_PRODUCTION_WEBSOCKET",
            defaults.enable_kalshi_production_websocket,
        ),
        kalshi_recorder_provider=_recorder_provider(source, defaults.kalshi_recorder_provider),
        kalshi_production_api_key_id_path=(
            Path(source["LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH"])
            if source.get("LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH")
            else None
        ),
        kalshi_production_private_key_path=(
            Path(source["LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH"])
            if source.get("LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH")
            else None
        ),
        kalshi_websocket_read_timeout_seconds=_positive_float(
            source,
            "LIVE15_KALSHI_WEBSOCKET_READ_TIMEOUT_SECONDS",
            defaults.kalshi_websocket_read_timeout_seconds,
        ),
        kalshi_websocket_stale_seconds=_positive_float(
            source,
            "LIVE15_KALSHI_WEBSOCKET_STALE_SECONDS",
            defaults.kalshi_websocket_stale_seconds,
        ),
        kalshi_websocket_queue_capacity=_bounded_positive_int(
            source,
            "LIVE15_KALSHI_WEBSOCKET_QUEUE_CAPACITY",
            defaults.kalshi_websocket_queue_capacity,
            65536,
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
        enable_ws_archive=_boolean(source, "LIVE15_ENABLE_WS_ARCHIVE", defaults.enable_ws_archive),
        ws_archive_root=ws_archive_root,
        ws_archive_manifest_path=ws_archive_manifest_path,
        ws_archive_hot_retention_seconds=_positive_float(
            source,
            "LIVE15_WS_ARCHIVE_HOT_RETENTION_SECONDS",
            defaults.ws_archive_hot_retention_seconds,
        ),
        ws_archive_chunk_records=_bounded_positive_int(
            source,
            "LIVE15_WS_ARCHIVE_CHUNK_RECORDS",
            defaults.ws_archive_chunk_records,
            250_000,
        ),
        ws_archive_poll_interval_seconds=_positive_float(
            source,
            "LIVE15_WS_ARCHIVE_POLL_INTERVAL_SECONDS",
            defaults.ws_archive_poll_interval_seconds,
        ),
        ws_archive_shadow_chunks=_bounded_positive_int(
            source,
            "LIVE15_WS_ARCHIVE_SHADOW_CHUNKS",
            defaults.ws_archive_shadow_chunks,
            100,
        ),
        ws_archive_purge_batch_rows=_bounded_positive_int(
            source,
            "LIVE15_WS_ARCHIVE_PURGE_BATCH_ROWS",
            defaults.ws_archive_purge_batch_rows,
            100_000,
        ),
        ws_compaction_min_reclaim_bytes=_positive_int(
            source,
            "LIVE15_WS_COMPACTION_MIN_RECLAIM_BYTES",
            defaults.ws_compaction_min_reclaim_bytes,
        ),
        ws_compaction_min_reclaim_percent=_percentage_decimal(
            source,
            "LIVE15_WS_COMPACTION_MIN_RECLAIM_PERCENT",
            defaults.ws_compaction_min_reclaim_percent,
        ),
        enable_adaptive_ws_retention=_boolean(
            source,
            "LIVE15_ENABLE_ADAPTIVE_WS_RETENTION",
            defaults.enable_adaptive_ws_retention,
        ),
        adaptive_retention_state_path=adaptive_retention_state_path,
        adaptive_retention_status_path=adaptive_retention_status_path,
        adaptive_retention_min_seconds=_positive_int(
            source,
            "LIVE15_ADAPTIVE_RETENTION_MIN_SECONDS",
            defaults.adaptive_retention_min_seconds,
        ),
        adaptive_retention_max_seconds=_positive_int(
            source,
            "LIVE15_ADAPTIVE_RETENTION_MAX_SECONDS",
            defaults.adaptive_retention_max_seconds,
        ),
        adaptive_retention_evidence_window_seconds=_positive_int(
            source,
            "LIVE15_ADAPTIVE_RETENTION_EVIDENCE_WINDOW_SECONDS",
            defaults.adaptive_retention_evidence_window_seconds,
        ),
        adaptive_retention_min_evidence_seconds=_positive_int(
            source,
            "LIVE15_ADAPTIVE_RETENTION_MIN_EVIDENCE_SECONDS",
            defaults.adaptive_retention_min_evidence_seconds,
        ),
        adaptive_retention_min_verified_chunks=_positive_int(
            source,
            "LIVE15_ADAPTIVE_RETENTION_MIN_VERIFIED_CHUNKS",
            defaults.adaptive_retention_min_verified_chunks,
        ),
        adaptive_retention_min_evidence_samples=_positive_int(
            source,
            "LIVE15_ADAPTIVE_RETENTION_MIN_EVIDENCE_SAMPLES",
            defaults.adaptive_retention_min_evidence_samples,
        ),
        adaptive_retention_min_recovery_sessions=_positive_int(
            source,
            "LIVE15_ADAPTIVE_RETENTION_MIN_RECOVERY_SESSIONS",
            defaults.adaptive_retention_min_recovery_sessions,
        ),
        adaptive_retention_simulation_passes=_positive_int(
            source,
            "LIVE15_ADAPTIVE_RETENTION_SIMULATION_PASSES",
            defaults.adaptive_retention_simulation_passes,
        ),
        adaptive_retention_safety_margin_seconds=_positive_int(
            source,
            "LIVE15_ADAPTIVE_RETENTION_SAFETY_MARGIN_SECONDS",
            defaults.adaptive_retention_safety_margin_seconds,
        ),
        adaptive_retention_cooldown_seconds=_positive_int(
            source,
            "LIVE15_ADAPTIVE_RETENTION_COOLDOWN_SECONDS",
            defaults.adaptive_retention_cooldown_seconds,
        ),
        adaptive_retention_reevaluation_seconds=_positive_int(
            source,
            "LIVE15_ADAPTIVE_RETENTION_REEVALUATION_SECONDS",
            defaults.adaptive_retention_reevaluation_seconds,
        ),
        adaptive_retention_incident_quiet_seconds=_positive_int(
            source,
            "LIVE15_ADAPTIVE_RETENTION_INCIDENT_QUIET_SECONDS",
            defaults.adaptive_retention_incident_quiet_seconds,
        ),
        adaptive_retention_min_projection_window_seconds=_positive_int(
            source,
            "LIVE15_ADAPTIVE_RETENTION_MIN_PROJECTION_WINDOW_SECONDS",
            defaults.adaptive_retention_min_projection_window_seconds,
        ),
        adaptive_retention_disk_deescalation_samples=_positive_int(
            source,
            "LIVE15_ADAPTIVE_RETENTION_DISK_DEESCALATION_SAMPLES",
            defaults.adaptive_retention_disk_deescalation_samples,
        ),
        adaptive_retention_auto_adjust=_boolean(
            source,
            "LIVE15_ADAPTIVE_RETENTION_AUTO_ADJUST",
            defaults.adaptive_retention_auto_adjust,
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
        readiness_snapshot_max_seconds=_positive_float(
            source,
            "LIVE15_READINESS_SNAPSHOT_MAX_SECONDS",
            defaults.readiness_snapshot_max_seconds,
        ),
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
        forward_shadow_data_path=forward_shadow_data_path,
        forward_shadow_paper_root=forward_shadow_paper_root,
        forward_shadow_model_zoo_v2_path=forward_shadow_model_zoo_v2_path,
        forward_shadow_dataset_path=forward_shadow_dataset_path,
        forward_shadow_model_root=forward_shadow_model_root,
        forward_shadow_starting_cash=_positive_decimal(
            source,
            "LIVE15_FORWARD_SHADOW_STARTING_CASH",
            defaults.forward_shadow_starting_cash,
        ),
        forward_shadow_order_quantity=_positive_decimal(
            source,
            "LIVE15_FORWARD_SHADOW_ORDER_QUANTITY",
            defaults.forward_shadow_order_quantity,
        ),
        forward_shadow_poll_interval_seconds=_positive_float(
            source,
            "LIVE15_FORWARD_SHADOW_POLL_INTERVAL_SECONDS",
            defaults.forward_shadow_poll_interval_seconds,
        ),
        forward_shadow_decision_grace_seconds=_positive_float(
            source,
            "LIVE15_FORWARD_SHADOW_DECISION_GRACE_SECONDS",
            defaults.forward_shadow_decision_grace_seconds,
        ),
        log_level=source.get("LIVE15_LOG_LEVEL", defaults.log_level).upper(),
    )
