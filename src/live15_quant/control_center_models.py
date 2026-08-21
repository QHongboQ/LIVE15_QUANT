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
    worker_progress: dict[str, datetime] = Field(default_factory=dict)
    worker_progress_age_seconds: dict[str, float] = Field(default_factory=dict)
    stale_workers: list[str] = Field(default_factory=list)
    event_loop_lag_seconds: float | None = None
    fatal_task: str | None = None
    fatal_error_type: str | None = None


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
