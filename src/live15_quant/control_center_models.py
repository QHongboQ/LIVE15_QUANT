"""Public, secret-free response models for the localhost Control Center API."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RecorderState(StrEnum):
    RUNNING = "running"
    STARTING = "starting"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    STALE = "stale"
    ERROR = "error"


class Availability(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    STALE = "stale"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class DatasetSnapshotStatus(StrEnum):
    NOT_BUILT = "not_built"
    CURRENT = "current"
    OUTDATED = "outdated"
    UNKNOWN = "unknown"


class StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WsArchiveHealth(StrictResponse):
    enabled: bool = False
    chunks: int = 0
    verified: int = 0
    failed: int = 0
    eligible: int = 0
    purged: int = 0
    compressed: int = 0
    uncompressed: int = 0
    last_archive: datetime | None = None
    last_replay: datetime | None = None
    hot_events_estimate: int = 0
    hot_oldest_timestamp: datetime | None = None
    hot_newest_timestamp: datetime | None = None
    hot_oldest_age_seconds: float | None = None
    hot_retention_seconds: float | None = None
    archive_backlog_events: int = 0
    archive_backlog_capped: bool = False
    archive_throughput_events_per_second: float = 0.0
    archive_elapsed_seconds: float = 0.0
    archive_lag_seconds: float | None = None
    compression_ratio: float | None = None
    last_purge_deleted_events: int = 0
    last_purge_transaction_seconds: float = 0.0
    last_purge_reusable_bytes: int = 0
    hot_sqlite_used_bytes: int | None = None
    freelist_reusable_bytes: int | None = None
    physical_database_bytes: int | None = None
    wal_bytes: int | None = None
    cold_archive_bytes: int | None = None
    cold_archive_growth_bytes_per_hour: float | None = None
    cold_archive_growth_bytes_per_day: float | None = None
    net_disk_growth_sample_seconds: float | None = None
    net_disk_growth_bytes_per_hour: float | None = None
    net_disk_growth_bytes_per_day: float | None = None
    disk_total_bytes: int | None = None
    disk_free_bytes: int | None = None
    disk_threshold_state: str = "unknown"
    shadow_acceptance_passed: bool = False


class HealthResponse(StrictResponse):
    status: str
    recorder_state: RecorderState
    heartbeat_status: Availability
    heartbeat_age_seconds: float | None = None
    observed_at: datetime | None = None
    uptime_seconds: float | None = None
    database_bytes: int | None = None
    wal_bytes: int | None = None
    written_records: int | None = None
    current_markets: dict[str, str | None] = Field(default_factory=dict)
    active_settlement_followups: int | None = None
    last_finalized_settlement: dict[str, str] = Field(default_factory=dict)
    retry_counts: dict[str, int] = Field(default_factory=dict)
    source_failures: dict[str, str] = Field(default_factory=dict)
    stale_sources: list[str] = Field(default_factory=list)
    market_closed_sources: list[str] = Field(default_factory=list)
    underlying_market_states: dict[str, str] = Field(default_factory=dict)
    worker_progress: dict[str, datetime] = Field(default_factory=dict)
    worker_progress_age_seconds: dict[str, float] = Field(default_factory=dict)
    stale_workers: list[str] = Field(default_factory=list)
    event_loop_lag_seconds: float | None = None
    fatal_task: str | None = None
    fatal_error_type: str | None = None
    kalshi_ws_connection_state: str = "disabled"
    kalshi_ws_synchronized_markets: dict[str, str] = Field(default_factory=dict)
    kalshi_ws_synchronized_count: int = 0
    kalshi_ws_book_age_seconds: dict[str, float] = Field(default_factory=dict)
    kalshi_ws_seq_gaps: int = 0
    kalshi_ws_resync_count: int = 0
    kalshi_ws_reconnect_count: int = 0
    kalshi_ws_queue_high_watermark: int = 0
    kalshi_ws_queue_capacity: int = 0
    kalshi_ws_queue_depth: int = 0
    kalshi_ws_queue_enqueued: int = 0
    kalshi_ws_queue_dequeued: int = 0
    kalshi_ws_queue_full_waits: int = 0
    kalshi_ws_queue_dropped: int = 0
    kalshi_ws_queue_max_backlog_seconds: float = 0.0
    kalshi_ws_queue_above_50_seconds: float = 0.0
    kalshi_ws_queue_above_75_seconds: float = 0.0
    kalshi_ws_queue_above_90_seconds: float = 0.0
    kalshi_ws_receive_persist_latency_ms: str | None = None
    kalshi_rest_fallback_status: str = "unavailable"
    ws_archive: WsArchiveHealth = Field(default_factory=WsArchiveHealth)


class MarketResponse(StrictResponse):
    asset: str
    availability: str
    ticker: str | None = None
    series: str | None = None
    target: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    seconds_remaining: float | None = None
    lifecycle: str
    official_status: str | None = None
    yes_bid: str | None = None
    yes_ask: str | None = None
    no_bid: str | None = None
    no_ask: str | None = None
    last_trade: str | None = None
    spread: str | None = None
    quote_age_seconds: float | None = None
    quote_status: str
    quote_source_timestamp: str | None = None
    quote_received_timestamp: datetime | None = None
    orderbook_status: str
    yes_bid_depth: list[list[str]] = Field(default_factory=list)
    no_bid_depth: list[list[str]] = Field(default_factory=list)
    underlying_provider: str | None = None
    underlying_product: str | None = None
    underlying_price: str | None = None
    underlying_age_seconds: float | None = None
    underlying_status: str
    primary_provider: str | None = None
    primary_age_seconds: float | None = None
    secondary_provider: str | None = None
    secondary_instrument: str | None = None
    secondary_price: str | None = None
    secondary_bid: str | None = None
    secondary_ask: str | None = None
    secondary_price_semantics: str | None = None
    secondary_age_seconds: float | None = None
    secondary_status: str = "not_applicable"
    secondary_clock_skew: bool = False
    secondary_source_timestamp: datetime | None = None
    secondary_received_timestamp: datetime | None = None
    secondary_persisted_timestamp: datetime | None = None
    secondary_source_receive_latency_ms: str | None = None
    secondary_receive_persist_latency_ms: str | None = None
    primary_secondary_price_diff: str | None = None
    primary_secondary_age_diff: float | None = None
    settlement_followup: str
    features: dict[str, dict[str, str | None]] = Field(default_factory=dict)
    previous_events: list[dict[str, str | None]] = Field(default_factory=list)


class AssetCoverage(StrictResponse):
    finalized_events: int = 0
    evaluated_finalized_events: int = 0
    unevaluated_finalized_events: int = 0
    trainable_events: int = 0
    training_rows: int = 0


class CoverageResponse(StrictResponse):
    status: str
    finalized_events: int
    trainable_events: int
    training_rows: int
    dataset_version: str
    feature_schema_version: str
    build_id: str | None = None
    completed_timestamp: datetime | None = None
    snapshot_status: DatasetSnapshotStatus
    snapshot_finalized_events: int | None = None
    unevaluated_finalized_events: int | None = None
    skipped_decisions: int | None = None
    events_without_training_rows: int | None = None
    trainability_rejections: dict[str, int] | None = None
    label_balance: dict[str, int] | None = None
    decision_time_bucket_coverage: dict[str, int] | None = None
    missing_feature_rates: dict[str, float | None] | None = None
    stale_feature_rates: dict[str, float | None] | None = None
    per_asset: dict[str, AssetCoverage]


class SystemResponse(StrictResponse):
    service: str = "LIVE15 Control Center"
    api_mode: str = "read_only_data_with_bounded_recorder_control"
    bind_host: str = "127.0.0.1"
    generated_at: datetime
    recorder_state: RecorderState
    raw_store: Availability
    feature_store: Availability
    trading_endpoints: bool = False
    credential_endpoints: bool = False
    recorder_control_actions: tuple[str, ...] = ("start", "pause", "resume")


class RecorderControlResponse(StrictResponse):
    state: RecorderState
    message: str


class RecorderEventResponse(StrictResponse):
    timestamp: datetime
    severity: str
    event_type: str
    asset: str | None = None
    source: str | None = None
    error_type: str | None = None
    message: str
