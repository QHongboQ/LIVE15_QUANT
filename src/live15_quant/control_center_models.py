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


class RecorderControlOutcome(StrEnum):
    APPLIED = "applied"
    ALREADY_IN_STATE = "already_in_state"


class RecorderControlAction(StrEnum):
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"


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


class TrainingProjectionState(StrEnum):
    AVAILABLE = "available"
    UNKNOWN = "unknown"
    STALE = "stale"
    NOT_MATERIALIZED = "not_materialized"
    INSUFFICIENT_DATA = "insufficient_data"


class StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountProfileResponse(StrictResponse):
    key: str
    label: str
    environment: str
    is_primary: bool = False


class AccountSummaryResponse(StrictResponse):
    profile: str
    status: str
    observed_at: datetime
    balance_cents: int | None = None
    portfolio_value_cents: int | None = None
    today_pnl_cents: int | None = None
    realized_pnl_cents: int | None = None
    fees_cents: int | None = None
    message: str | None = None


class AccountPositionResponse(StrictResponse):
    ticker: str
    position: int | None = None
    market_exposure_cents: int | None = None
    realized_pnl_cents: int | None = None
    fees_cents: int | None = None
    mark_cents: int | None = None
    mark_status: str = "unavailable"


class AccountOrderResponse(StrictResponse):
    order_id: str
    ticker: str | None = None
    status: str | None = None
    side: str | None = None
    action: str | None = None
    count: int | None = None
    remaining_count: int | None = None
    yes_price_cents: int | None = None
    no_price_cents: int | None = None
    created_at: datetime | None = None


class AccountFillResponse(StrictResponse):
    trade_id: str
    order_id: str | None = None
    ticker: str | None = None
    side: str | None = None
    action: str | None = None
    count: int | None = None
    yes_price_cents: int | None = None
    no_price_cents: int | None = None
    created_at: datetime | None = None


class AccountLedgerEntryResponse(StrictResponse):
    entry_type: str
    amount_cents: int | None = None
    ticker: str | None = None
    reference: str | None = None
    observed_at: datetime | None = None
    description: str | None = None


class AccountReadResponse(StrictResponse):
    profile: str
    status: str
    observed_at: datetime
    summary: AccountSummaryResponse
    positions: list[AccountPositionResponse] = Field(default_factory=list)
    orders: list[AccountOrderResponse] = Field(default_factory=list)
    fills: list[AccountFillResponse] = Field(default_factory=list)
    ledger: list[AccountLedgerEntryResponse] = Field(default_factory=list)
    message: str | None = None


class WsArchiveHealth(StrictResponse):
    enabled: bool = False
    chunks: int = 0
    verified: int = 0
    failed: int = 0
    eligible: int | None = None
    purged: int | None = None
    compressed: int | None = None
    uncompressed: int | None = None
    last_archive: datetime | None = None
    last_replay: datetime | None = None
    hot_events_estimate: int = 0
    hot_oldest_timestamp: datetime | None = None
    hot_newest_timestamp: datetime | None = None
    hot_oldest_age_seconds: float | None = None
    hot_retention_seconds: float | None = None
    archive_backlog_events: int = 0
    archive_backlog_capped: bool = False
    input_ws_events_per_second: float | None = None
    archive_throughput_events_per_second: float = 0.0
    archive_catch_up_ratio: float | None = None
    archive_backlog_slope_events_per_second: float | None = None
    archive_catch_up_eta_seconds: float | None = None
    archive_catch_up_status: str = "UNKNOWN"
    archive_elapsed_seconds: float = 0.0
    archive_lag_seconds: float | None = None
    compression_ratio: float | None = None
    last_purge_deleted_events: int | None = None
    last_purge_transaction_seconds: float | None = None
    last_purge_reusable_bytes: int | None = None
    hot_sqlite_used_bytes: int | None = None
    freelist_reusable_bytes: int | None = None
    physical_database_bytes: int | None = None
    wal_bytes: int | None = None
    cold_archive_bytes: int | None = None
    raw_ws_growth_bytes_per_hour: float | None = None
    raw_ws_growth_bytes_per_day: float | None = None
    cold_archive_growth_bytes_per_hour: float | None = None
    cold_archive_growth_bytes_per_day: float | None = None
    net_disk_growth_sample_seconds: float | None = None
    net_disk_growth_bytes_per_hour: float | None = None
    net_disk_growth_bytes_per_day: float | None = None
    disk_total_bytes: int | None = None
    disk_free_bytes: int | None = None
    disk_threshold_state: str = "unknown"
    shadow_acceptance_passed: bool = False
    quarantined: int | None = None
    waiting_for_replay_baseline: int | None = None
    archive_poll_mode: str | None = None
    archive_next_poll_seconds: float | None = None
    deferred_for_ws_backpressure: bool = False


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
    current_health_issues: list[str] = Field(default_factory=list)
    source_failures: dict[str, str] = Field(default_factory=dict)
    stale_sources: list[str] = Field(default_factory=list)
    market_closed_sources: list[str] = Field(default_factory=list)
    underlying_market_states: dict[str, str] = Field(default_factory=dict)
    worker_progress: dict[str, datetime] = Field(default_factory=dict)
    worker_progress_age_seconds: dict[str, float] = Field(default_factory=dict)
    worker_health: dict[str, WorkerHealthResponse] = Field(default_factory=dict)
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


class WorkerHealthResponse(StrictResponse):
    """Current worker state; lifetime retry counters deliberately do not appear here."""

    current_state: str = "unknown"
    consecutive_failures: int = 0
    last_error_type: str | None = None
    next_retry_at: datetime | None = None
    last_progress_timestamp: datetime | None = None
    last_successful_observation_timestamp: datetime | None = None
    age_seconds: float | None = None


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


class EventSummaryResponse(StrictResponse):
    """Exact aggregate over an explicitly bounded recorder-event time window."""

    window_start: datetime
    window_end: datetime
    availability: Availability
    warnings: int | None = None
    errors: int | None = None
    fatals: int | None = None
    sample_truncated: bool = False


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


class TrainingAssetCoverage(StrictResponse):
    events: int | None = None
    rows: int | None = None
    eligible_events: int | None = None
    ineligible_events: int | None = None


class TrainingProjection(StrictResponse):
    state: TrainingProjectionState
    status: str
    reason_code: str
    events: int | None = None
    eligible_events: int | None = None
    ineligible_events: int | None = None
    rows: int | None = None
    assets: int | None = None
    observed_at: datetime | None = None
    source_path: str | None = None
    per_asset: dict[str, TrainingAssetCoverage] = Field(default_factory=dict)


class CompletedDatasetProjection(StrictResponse):
    state: TrainingProjectionState
    status: str
    reason_code: str
    build_id: str | None = None
    dataset_version: str | None = None
    feature_schema_version: str | None = None
    completed_timestamp: datetime | None = None
    events: int | None = None
    rows: int | None = None
    snapshot_status: DatasetSnapshotStatus = DatasetSnapshotStatus.NOT_BUILT
    diagnostics: dict[str, object] | None = None
    per_asset: dict[str, TrainingAssetCoverage] = Field(default_factory=dict)


class FrozenExperimentFact(StrictResponse):
    experiment_id: str
    status: str
    dataset_id: str | None = None
    created_timestamp: datetime | None = None
    source: str | None = None
    details: dict[str, object] = Field(default_factory=dict)


class TrainingResponse(StrictResponse):
    generated_at: datetime
    raw_finalized_pool: TrainingProjection
    current_trainable: TrainingProjection
    latest_completed_dataset: CompletedDatasetProjection
    frozen_experiment_facts: list[FrozenExperimentFact] = Field(default_factory=list)
    sequence_readiness: str = "UNKNOWN"


class DataResponse(StrictResponse):
    generated_at: datetime
    recorder_state: RecorderState
    raw_store: Availability
    finalized_events: int | None = None
    finalized_assets: int | None = None
    source_as_of: datetime | None = None
    freshness: str = "UNKNOWN"
    notes: list[str] = Field(default_factory=list)


class ResearchDataSourceResponse(StrictResponse):
    source_id: str
    source_type: str
    trust_tier: str
    provider_version: str | None = None
    schema_version: str
    earliest_timestamp: datetime | None = None
    latest_timestamp: datetime | None = None
    utc_calendar_days: list[str] = Field(default_factory=list)
    market_session_days: list[str] = Field(default_factory=list)
    assets: list[str] = Field(default_factory=list)
    eligible_events: int = 0
    eligible_observations: int = 0
    availability_semantics: str
    verification_state: str
    provenance: str
    content_identity: str
    limitations: list[str] = Field(default_factory=list)
    capability_days: dict[str, list[str]] = Field(default_factory=dict)
    coverage_status: dict[str, str] = Field(default_factory=dict)


class ResearchFreshnessPolicyResponse(StrictResponse):
    version: str
    feature_freshness: dict[str, object]
    training_recency: dict[str, object]
    forward_oos_freshness: dict[str, object]


class FrozenHoldoutResponse(StrictResponse):
    dataset_id: str
    status: str
    excluded_event_count: int = 0
    excluded_time_range_count: int = 0
    validation_days: list[str] = Field(default_factory=list)
    payload_accessed: bool = False


class ResearchDataResponse(StrictResponse):
    schema_version: str
    universe_id: str
    content_hash: str
    cutoff_timestamp: datetime
    code_git_sha: str
    freshness_policy: ResearchFreshnessPolicyResponse
    session_semantics_version: str
    source_registry_version: str
    sources: list[ResearchDataSourceResponse] = Field(default_factory=list)
    earliest_timestamp: datetime | None = None
    latest_timestamp: datetime | None = None
    utc_calendar_days: list[str] = Field(default_factory=list)
    market_session_days: list[str] = Field(default_factory=list)
    eligible_development_days: list[str] = Field(default_factory=list)
    validation_days: list[str] = Field(default_factory=list)
    assets: list[str] = Field(default_factory=list)
    eligible_events: int = 0
    eligible_observations: int = 0
    deduplicated_observations: int = 0
    conflicting_observations: int = 0
    quarantined_observations: int = 0
    holdout_excluded_observations: int = 0
    selected_source_ids: list[str] = Field(default_factory=list)
    frozen_holdout: FrozenHoldoutResponse
    holdout_accessed: bool = False
    depthfeed_status: str
    capability_days: dict[str, list[str]] = Field(default_factory=dict)


class ArchiveResponse(StrictResponse):
    generated_at: datetime
    state: str
    enabled: bool
    poll_mode: str | None = None
    next_poll_seconds: float | None = None
    verified_chunks: int | None = None
    failed_chunks: int | None = None
    waiting_chunks: int | None = None
    quarantined_chunks: int | None = None
    backlog_events: int | None = None
    backlog_capped: bool = False
    deferred_for_ws_backpressure: bool = False
    throughput_events_per_second: float | None = None
    input_ws_events_per_second: float | None = None
    catch_up_ratio: float | None = None
    backlog_slope_events_per_second: float | None = None
    catch_up_eta_seconds: float | None = None
    catch_up_status: str = "UNKNOWN"
    lag_seconds: float | None = None
    uncompressed_archive_bytes: int | None = None
    compressed_archive_bytes: int | None = None
    compression_ratio: float | None = None
    compressed_bytes_saved: int | None = None
    compression_saving_percent: float | None = None
    cold_archive_bytes: int | None = None
    purge_eligible_chunks: int | None = None
    purged_chunks: int | None = None
    total_purged_events: int | None = None
    purge_eligible_events: int | None = None
    last_purge_deleted_events: int | None = None
    last_purge_duration_seconds: float | None = None
    last_purge_reusable_bytes: int | None = None
    purge_is_dry_run: bool = True
    notes: list[str] = Field(default_factory=list)


class StorageResponse(StrictResponse):
    generated_at: datetime
    state: str
    disk_total_bytes: int | None = None
    disk_free_bytes: int | None = None
    hot_sqlite_bytes: int | None = None
    sqlite_reusable_bytes: int | None = None
    physical_reclaimed_bytes: int | None = None
    cold_archive_bytes: int | None = None
    wal_bytes: int | None = None
    compression_saved_bytes: int | None = None
    compression_saving_percent: float | None = None
    growth_bytes_per_day: float | None = None
    raw_ws_growth_bytes_per_hour: float | None = None
    raw_ws_growth_bytes_per_day: float | None = None
    cold_archive_growth_bytes_per_hour: float | None = None
    cold_archive_growth_bytes_per_day: float | None = None
    net_disk_growth_bytes_per_hour: float | None = None
    net_disk_growth_bytes_per_day: float | None = None
    retention_seconds: float | None = None
    purge_eligible_chunks: int | None = None
    purged_chunks: int | None = None
    total_purged_events: int | None = None
    last_purge_deleted_events: int | None = None
    last_purge_duration_seconds: float | None = None
    last_purge_reusable_bytes: int | None = None
    compaction_reclaimable_bytes: int | None = None
    compaction_reclaimable_percent: float | None = None
    compaction_minimum_required_bytes: int | None = None
    compaction_minimum_required_percent: float | None = None
    compaction_status: str = "UNKNOWN"
    purge_is_dry_run: bool = True
    notes: list[str] = Field(default_factory=list)


class OperationsResponse(StrictResponse):
    generated_at: datetime
    recorder_state: RecorderState
    recorder_heartbeat: Availability
    fatal_task: str | None = None
    fatal_error_type: str | None = None
    active_markets: int | None = None
    pending_settlements: int | None = None
    retries: int | None = None
    runtime_components: dict[str, RuntimeComponentResponse] = Field(default_factory=dict)
    recent_events: list[RecorderEventResponse] = Field(default_factory=list)


class RuntimeComponentResponse(StrictResponse):
    status: str
    pid: int | None = None
    started_at: datetime | None = None
    last_heartbeat: datetime | None = None
    heartbeat_age_seconds: float | None = None
    last_error: str | None = None
    process_alive: bool = False
    expected_mode: str | None = None


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
    runtime_components: dict[str, RuntimeComponentResponse] = Field(default_factory=dict)


class RecorderControlResponse(StrictResponse):
    action: RecorderControlAction
    action_succeeded: bool = True
    outcome: RecorderControlOutcome
    state: RecorderState
    pid: int | None = None
    message: str


class RecorderEventResponse(StrictResponse):
    timestamp: datetime
    severity: str
    event_type: str
    asset: str | None = None
    source: str | None = None
    error_type: str | None = None
    message: str
