"""Evidence-gated adaptive HOT retention without raw-database scans."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any

RETENTION_LADDER_SECONDS = (21_600, 14_400, 10_800, 7_200, 3_600)
ADAPTIVE_RETENTION_SCHEMA_VERSION = 3
_DISK_STATE_RANK = {
    "normal": 0,
    "warning": 1,
    "archive_urgent": 2,
    "critical": 3,
    "fail_safe": 4,
}


class AdaptiveRetentionError(RuntimeError):
    """Adaptive state/evidence cannot be trusted."""


class AdaptiveRetentionStateError(AdaptiveRetentionError):
    """Persisted controller state is corrupt or inconsistent with runtime."""


class AdaptiveRetentionMode(StrEnum):
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    HOLD = "HOLD"
    RECOMMEND = "RECOMMEND"
    ADJUSTED = "ADJUSTED"
    SAFETY_INCREASE = "SAFETY_INCREASE"
    FAIL_SAFE = "FAIL_SAFE"


class RetentionSimulationResult(StrEnum):
    PASS = "PASS"
    HOLD = "HOLD"
    FAIL = "FAIL"


class RetentionReasonCode(StrEnum):
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CURRENT_BASELINE = "current_baseline"
    MORE_PASSES_REQUIRED = "more_passes_required"
    COOLDOWN_ACTIVE = "cooldown_active"
    SAFETY_INCREASE = "safety_increase"
    GATES_PASSED = "gates_passed"
    AUTO_ADJUST_DISABLED = "auto_adjust_disabled"
    CLOCK_ROLLBACK = "clock_rollback"
    DISK_FAIL_SAFE = "disk_fail_safe"


class EstimateConfidence(StrEnum):
    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    SUFFICIENT = "SUFFICIENT"


@dataclass(frozen=True, slots=True)
class AdaptiveRetentionPolicy:
    minimum_seconds: int = 3_600
    maximum_seconds: int = 21_600
    evidence_window: timedelta = timedelta(days=7)
    minimum_evidence_duration: timedelta = timedelta(days=3)
    minimum_verified_chunks: int = 100
    minimum_evidence_samples: int = 24
    minimum_recovery_sessions: int = 3
    minimum_simulation_passes: int = 3
    safety_margin: timedelta = timedelta(minutes=30)
    cooldown: timedelta = timedelta(days=1)
    reevaluation_interval: timedelta = timedelta(hours=1)
    serious_incident_quiet_period: timedelta = timedelta(days=1)
    minimum_projection_window: timedelta = timedelta(days=1)
    disk_deescalation_samples: int = 3
    auto_adjust: bool = True

    def __post_init__(self) -> None:
        if (
            self.minimum_seconds not in RETENTION_LADDER_SECONDS
            or self.maximum_seconds not in RETENTION_LADDER_SECONDS
            or self.minimum_seconds > self.maximum_seconds
            or self.evidence_window <= timedelta(0)
            or not timedelta(0) < self.minimum_evidence_duration <= self.evidence_window
            or self.minimum_verified_chunks < 1
            or self.minimum_evidence_samples < 2
            or self.minimum_recovery_sessions < 1
            or self.minimum_simulation_passes < 1
            or self.safety_margin < timedelta(0)
            or self.cooldown < timedelta(0)
            or self.reevaluation_interval <= timedelta(0)
            or self.serious_incident_quiet_period < timedelta(0)
            or self.minimum_projection_window <= timedelta(0)
            or self.disk_deescalation_samples < 1
        ):
            raise ValueError("adaptive retention policy is invalid")

    @property
    def candidates(self) -> tuple[int, ...]:
        return tuple(
            value
            for value in RETENTION_LADDER_SECONDS
            if self.minimum_seconds <= value <= self.maximum_seconds
        )


@dataclass(frozen=True, slots=True)
class AdaptiveRetentionObservation:
    observed_at: datetime
    verified_chunks: int
    failed_chunks: int
    physical_database_bytes: int
    hot_used_bytes: int
    freelist_reusable_bytes: int
    cold_archive_bytes: int
    cold_growth_bytes_per_day: float | None
    raw_ws_growth_bytes_per_day: float | None
    raw_ws_observation_window_seconds: float | None
    disk_free_bytes: int
    disk_total_bytes: int
    event_loop_lag_seconds: float
    ws_queue_depth: int
    ws_queue_capacity: int
    ws_sequence_gaps: int
    ws_resyncs: int
    ws_reconnects: int
    data_gap_incidents: int
    unresolved_data_gaps: int | None
    archive_or_replay_failure: bool
    serious_runtime_incident: bool
    disk_threshold_state: str = "normal"
    recovery_lookback_seconds: float | None = None
    hot_access_age_seconds: float | None = None
    recovery_session_id: str | None = None
    hot_access_evidence_complete: bool = False

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("adaptive retention observation time must be timezone-aware")
        integers = (
            self.verified_chunks,
            self.failed_chunks,
            self.physical_database_bytes,
            self.hot_used_bytes,
            self.freelist_reusable_bytes,
            self.cold_archive_bytes,
            self.disk_free_bytes,
            self.disk_total_bytes,
            self.ws_queue_depth,
            self.ws_queue_capacity,
            self.ws_sequence_gaps,
            self.ws_resyncs,
            self.ws_reconnects,
            self.data_gap_incidents,
        )
        if any(value < 0 for value in integers) or self.disk_free_bytes > self.disk_total_bytes:
            raise ValueError("adaptive retention counters must be non-negative")
        if self.unresolved_data_gaps is not None and self.unresolved_data_gaps < 0:
            raise ValueError("adaptive retention unresolved gap count must be non-negative")
        if self.ws_queue_depth > self.ws_queue_capacity and self.ws_queue_capacity > 0:
            raise ValueError("adaptive retention queue depth exceeds capacity")
        if self.disk_threshold_state not in {
            "normal",
            "warning",
            "archive_urgent",
            "critical",
            "fail_safe",
        }:
            raise ValueError("adaptive retention disk threshold state is invalid")
        for value in (
            self.cold_growth_bytes_per_day,
            self.raw_ws_growth_bytes_per_day,
            self.raw_ws_observation_window_seconds,
            self.event_loop_lag_seconds,
            self.recovery_lookback_seconds,
            self.hot_access_age_seconds,
        ):
            if value is not None and value < 0:
                raise ValueError("adaptive retention duration/rate must be non-negative")


@dataclass(frozen=True, slots=True)
class CandidateSimulation:
    retention_seconds: int
    result: RetentionSimulationResult
    estimated_hot_bytes: int
    estimated_transition_savings_bytes: int
    estimated_disk_savings_first_day: int
    required_recovery_seconds: float | None
    estimate_confidence: EstimateConfidence
    estimate_observation_window_seconds: float | None
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "retention_seconds": self.retention_seconds,
            "result": self.result.value,
            "estimated_hot_bytes": self.estimated_hot_bytes,
            "estimated_transition_savings_bytes": self.estimated_transition_savings_bytes,
            "estimated_disk_savings_first_day": self.estimated_disk_savings_first_day,
            "required_recovery_seconds": self.required_recovery_seconds,
            "estimate_confidence": self.estimate_confidence.value,
            "estimate_observation_window_seconds": self.estimate_observation_window_seconds,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RetentionRuntimeMetrics:
    physical_database_bytes: int
    hot_used_bytes: int
    freelist_reusable_bytes: int
    cold_archive_bytes: int
    cold_growth_bytes_per_day: float | None
    raw_ws_growth_bytes_per_day: float | None
    disk_free_bytes: int
    disk_total_bytes: int
    estimated_days_to_full: float | None
    event_loop_lag_seconds: float
    ws_queue_depth: int
    ws_queue_capacity: int
    ws_queue_pressure_percent: float | None
    disk_pressure_state: str

    def as_dict(self) -> dict[str, int | float | str | None]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class AdaptiveRetentionStatus:
    observed_at: datetime
    current_retention_seconds: int
    recommended_retention_seconds: int
    actual_applied_retention_seconds: int
    controller_mode: AdaptiveRetentionMode
    evidence_window_start: datetime | None
    evidence_window_end: datetime | None
    evidence_duration_seconds: float
    evidence_samples: int
    recovery_lookback_p50_seconds: float | None
    recovery_lookback_p95_seconds: float | None
    recovery_lookback_max_seconds: float | None
    hot_access_age_p50_seconds: float | None
    hot_access_age_p95_seconds: float | None
    hot_access_age_max_seconds: float | None
    archive_reliability: float | None
    verified_chunks_in_window: int
    failed_chunks_in_window: int
    serious_incidents_in_window: int
    recovery_sessions_in_window: int
    runtime_metrics: RetentionRuntimeMetrics
    simulations: tuple[CandidateSimulation, ...]
    reason: str
    reason_code: RetentionReasonCode
    cooldown_until: datetime | None
    last_adjustment_at: datetime | None
    next_eligible_adjustment_at: datetime | None
    next_reevaluation_at: datetime
    disk_deescalation_streak: int

    def as_dict(self) -> dict[str, object]:
        return {
            "observed_at": self.observed_at.isoformat(),
            "current_retention_seconds": self.current_retention_seconds,
            "recommended_retention_seconds": self.recommended_retention_seconds,
            "actual_applied_retention_seconds": self.actual_applied_retention_seconds,
            "controller_mode": self.controller_mode.value,
            "evidence_window": {
                "start": (
                    None
                    if self.evidence_window_start is None
                    else self.evidence_window_start.isoformat()
                ),
                "end": (
                    None
                    if self.evidence_window_end is None
                    else self.evidence_window_end.isoformat()
                ),
                "duration_seconds": self.evidence_duration_seconds,
                "samples": self.evidence_samples,
            },
            "recovery_lookback_seconds": {
                "p50": self.recovery_lookback_p50_seconds,
                "p95": self.recovery_lookback_p95_seconds,
                "max": self.recovery_lookback_max_seconds,
            },
            "hot_access_age_seconds": {
                "p50": self.hot_access_age_p50_seconds,
                "p95": self.hot_access_age_p95_seconds,
                "max": self.hot_access_age_max_seconds,
            },
            "archive_reliability": self.archive_reliability,
            "verified_chunks_in_window": self.verified_chunks_in_window,
            "failed_chunks_in_window": self.failed_chunks_in_window,
            "serious_incidents_in_window": self.serious_incidents_in_window,
            "recovery_sessions_in_window": self.recovery_sessions_in_window,
            "runtime_metrics": self.runtime_metrics.as_dict(),
            "simulation_results": {
                str(item.retention_seconds): item.as_dict() for item in self.simulations
            },
            "reason": self.reason,
            "reason_code": self.reason_code.value,
            "cooldown_until": None
            if self.cooldown_until is None
            else self.cooldown_until.isoformat(),
            "last_adjustment_at": (
                None if self.last_adjustment_at is None else self.last_adjustment_at.isoformat()
            ),
            "next_eligible_adjustment_at": (
                None
                if self.next_eligible_adjustment_at is None
                else self.next_eligible_adjustment_at.isoformat()
            ),
            "next_reevaluation_at": self.next_reevaluation_at.isoformat(),
            "disk_pressure_state": self.runtime_metrics.disk_pressure_state,
            "disk_deescalation_streak": self.disk_deescalation_streak,
        }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _persisted_utc(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp is naive")
        return parsed.astimezone(UTC)
    except (TypeError, ValueError) as error:
        raise AdaptiveRetentionStateError(
            f"adaptive retention persisted {field} timestamp is corrupt"
        ) from error


class AdaptiveRetentionController:
    """Persist small evidence samples and make one bounded, resumable decision."""

    def __init__(
        self,
        state_path: Path,
        policy: AdaptiveRetentionPolicy,
        *,
        initial_retention_seconds: int,
    ) -> None:
        self.path = state_path.resolve()
        self.policy = policy
        if initial_retention_seconds not in policy.candidates:
            raise ValueError("initial adaptive retention is outside the configured ladder")
        self.initial_retention_seconds = initial_retention_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._initialize()
        except sqlite3.DatabaseError as error:
            raise AdaptiveRetentionStateError(
                "adaptive retention state database is unavailable or corrupt"
            ) from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=2000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS adaptive_retention_samples(
                    observed_at TEXT PRIMARY KEY,
                    verified_chunks INTEGER NOT NULL,
                    failed_chunks INTEGER NOT NULL,
                    physical_database_bytes INTEGER NOT NULL,
                    hot_used_bytes INTEGER NOT NULL,
                    freelist_reusable_bytes INTEGER NOT NULL,
                    cold_archive_bytes INTEGER NOT NULL,
                    cold_growth_bytes_per_day REAL,
                    raw_ws_growth_bytes_per_day REAL,
                    disk_free_bytes INTEGER NOT NULL,
                    disk_total_bytes INTEGER NOT NULL,
                    event_loop_lag_seconds REAL NOT NULL,
                    ws_queue_depth INTEGER NOT NULL,
                    ws_queue_capacity INTEGER NOT NULL,
                    ws_sequence_gaps INTEGER NOT NULL,
                    ws_resyncs INTEGER NOT NULL,
                    ws_reconnects INTEGER NOT NULL,
                    data_gap_incidents INTEGER NOT NULL,
                    archive_or_replay_failure INTEGER NOT NULL,
                    serious_runtime_incident INTEGER NOT NULL,
                    disk_threshold_state TEXT NOT NULL,
                    recovery_lookback_seconds REAL,
                    hot_access_age_seconds REAL
                    ,recovery_session_id TEXT
                    ,hot_access_evidence_complete INTEGER NOT NULL DEFAULT 0
                ) STRICT"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_adaptive_samples_time
                ON adaptive_retention_samples(observed_at)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS adaptive_retention_state(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    current_retention_seconds INTEGER NOT NULL,
                    recommended_retention_seconds INTEGER NOT NULL,
                    recommendation_streak INTEGER NOT NULL,
                    last_change_at TEXT,
                    next_reevaluation_at TEXT,
                    last_status_json TEXT,
                    last_observed_at TEXT,
                    disk_pressure_state TEXT NOT NULL DEFAULT 'normal',
                    disk_deescalation_streak INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                ) STRICT"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS adaptive_retention_meta(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    schema_version INTEGER NOT NULL
                ) STRICT"""
            )
            meta = connection.execute(
                "SELECT schema_version FROM adaptive_retention_meta WHERE singleton=1"
            ).fetchone()
            if meta is not None and int(meta[0]) > ADAPTIVE_RETENTION_SCHEMA_VERSION:
                raise AdaptiveRetentionStateError(
                    "adaptive retention state uses an unsupported future schema"
                )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(adaptive_retention_samples)")
            }
            if "raw_ws_growth_bytes_per_day" not in columns:
                connection.execute(
                    "ALTER TABLE adaptive_retention_samples "
                    "ADD COLUMN raw_ws_growth_bytes_per_day REAL"
                )
            if "data_gap_incidents" not in columns:
                connection.execute(
                    "ALTER TABLE adaptive_retention_samples "
                    "ADD COLUMN data_gap_incidents INTEGER NOT NULL DEFAULT 0"
                )
            if "recovery_session_id" not in columns:
                connection.execute(
                    "ALTER TABLE adaptive_retention_samples ADD COLUMN recovery_session_id TEXT"
                )
            if "hot_access_evidence_complete" not in columns:
                connection.execute(
                    "ALTER TABLE adaptive_retention_samples "
                    "ADD COLUMN hot_access_evidence_complete INTEGER NOT NULL DEFAULT 0"
                )
            state_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(adaptive_retention_state)")
            }
            if "last_observed_at" not in state_columns:
                connection.execute(
                    "ALTER TABLE adaptive_retention_state ADD COLUMN last_observed_at TEXT"
                )
            if "disk_pressure_state" not in state_columns:
                connection.execute(
                    "ALTER TABLE adaptive_retention_state "
                    "ADD COLUMN disk_pressure_state TEXT NOT NULL DEFAULT 'normal'"
                )
            if "disk_deescalation_streak" not in state_columns:
                connection.execute(
                    "ALTER TABLE adaptive_retention_state "
                    "ADD COLUMN disk_deescalation_streak INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """INSERT INTO adaptive_retention_meta(singleton,schema_version) VALUES(1,?)
                ON CONFLICT(singleton) DO UPDATE SET schema_version=excluded.schema_version""",
                (ADAPTIVE_RETENTION_SCHEMA_VERSION,),
            )
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """INSERT OR IGNORE INTO adaptive_retention_state(
                    singleton,current_retention_seconds,recommended_retention_seconds,
                    recommendation_streak,updated_at
                ) VALUES(1,?,?,0,?)""",
                (self.initial_retention_seconds, self.initial_retention_seconds, now),
            )
            connection.commit()

    def _state(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM adaptive_retention_state WHERE singleton=1"
        ).fetchone()
        if (
            row is None
            or int(row["current_retention_seconds"]) not in self.policy.candidates
            or str(row["disk_pressure_state"]) not in _DISK_STATE_RANK
            or int(row["disk_deescalation_streak"]) < 0
        ):
            raise AdaptiveRetentionStateError(
                "adaptive retention state is missing or outside the safety ladder"
            )
        return row

    def current_retention_seconds(self) -> int:
        with self._connect() as connection:
            return int(self._state(connection)["current_retention_seconds"])

    def evaluate_once(
        self,
        observation: AdaptiveRetentionObservation,
        *,
        actual_retention_seconds: int | None = None,
        allow_adjustment: bool = False,
        record_evidence: bool = True,
    ) -> AdaptiveRetentionStatus:
        observed = observation.observed_at.astimezone(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state(connection)
            current = int(state["current_retention_seconds"])
            actual = current if actual_retention_seconds is None else actual_retention_seconds
            if actual not in self.policy.candidates or actual != current:
                raise AdaptiveRetentionStateError(
                    "persisted and actually applied retention values do not reconcile"
                )
            last_observed = (
                _persisted_utc(state["last_observed_at"], "last_observed_at")
                if state["last_observed_at"]
                else None
            )
            if last_observed is not None and observed < last_observed:
                raw = state["last_status_json"]
                if not raw:
                    raise AdaptiveRetentionStateError(
                        "clock rollback detected without a trusted prior status"
                    )
                connection.rollback()
                trusted = self._trusted_status(str(raw))
                return replace(
                    trusted,
                    observed_at=observed,
                    actual_applied_retention_seconds=actual,
                    controller_mode=AdaptiveRetentionMode.HOLD,
                    recommended_retention_seconds=current,
                    reason="wall clock moved backwards; adjustment is blocked",
                    reason_code=RetentionReasonCode.CLOCK_ROLLBACK,
                    runtime_metrics=self._runtime_metrics(
                        observation, trusted.runtime_metrics.disk_pressure_state
                    ),
                )
            if record_evidence and last_observed == observed:
                if self._append_observation(connection, observation):
                    raise AdaptiveRetentionStateError(
                        "controller state references a missing last observation"
                    )
                raw = state["last_status_json"]
                if not raw:
                    raise AdaptiveRetentionStateError(
                        "duplicate adaptive observation has no committed prior decision"
                    )
                connection.rollback()
                trusted = self._trusted_status(str(raw))
                return replace(
                    trusted,
                    observed_at=observed,
                    actual_applied_retention_seconds=actual,
                    runtime_metrics=self._runtime_metrics(
                        observation, trusted.runtime_metrics.disk_pressure_state
                    ),
                )
            if not record_evidence:
                raw = state["last_status_json"]
                if raw:
                    connection.rollback()
                    trusted = self._trusted_status(str(raw))
                    return replace(
                        trusted,
                        observed_at=observed,
                        actual_applied_retention_seconds=actual,
                        runtime_metrics=self._runtime_metrics(
                            observation, trusted.runtime_metrics.disk_pressure_state
                        ),
                    )
                status = self._evaluate(
                    connection,
                    observation,
                    state,
                    actual,
                    allow_adjustment=False,
                )
                connection.rollback()
                return status
            next_raw = state["next_reevaluation_at"]
            next_at = _persisted_utc(next_raw, "next_reevaluation_at") if next_raw else None
            if next_at is not None and observed < next_at:
                raw = state["last_status_json"]
                if raw:
                    connection.rollback()
                    trusted = self._trusted_status(str(raw))
                    return replace(
                        trusted,
                        observed_at=observed,
                        actual_applied_retention_seconds=actual,
                        runtime_metrics=self._runtime_metrics(
                            observation, trusted.runtime_metrics.disk_pressure_state
                        ),
                    )
            inserted = self._append_observation(connection, observation)
            if not inserted:
                raw = state["last_status_json"]
                if not raw:
                    raise AdaptiveRetentionStateError(
                        "duplicate adaptive observation has no committed prior decision"
                    )
                connection.rollback()
                trusted = self._trusted_status(str(raw))
                return replace(
                    trusted,
                    observed_at=observed,
                    actual_applied_retention_seconds=actual,
                    runtime_metrics=self._runtime_metrics(
                        observation, trusted.runtime_metrics.disk_pressure_state
                    ),
                )
            status = self._evaluate(
                connection, observation, state, actual, allow_adjustment=allow_adjustment
            )
            self._persist_status(connection, status, state)
            self._prune_samples(connection, observed)
            connection.commit()
            return status

    @staticmethod
    def _trusted_status(raw: str) -> AdaptiveRetentionStatus:
        try:
            return AdaptiveRetentionController._status_from_json(raw)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise AdaptiveRetentionStateError(
                "adaptive retention cached status is corrupt"
            ) from error

    def _append_observation(
        self, connection: sqlite3.Connection, observation: AdaptiveRetentionObservation
    ) -> bool:
        values = (
            observation.observed_at.astimezone(UTC).isoformat(),
            observation.verified_chunks,
            observation.failed_chunks,
            observation.physical_database_bytes,
            observation.hot_used_bytes,
            observation.freelist_reusable_bytes,
            observation.cold_archive_bytes,
            observation.cold_growth_bytes_per_day,
            observation.raw_ws_growth_bytes_per_day,
            observation.disk_free_bytes,
            observation.disk_total_bytes,
            observation.event_loop_lag_seconds,
            observation.ws_queue_depth,
            observation.ws_queue_capacity,
            observation.ws_sequence_gaps,
            observation.ws_resyncs,
            observation.ws_reconnects,
            observation.data_gap_incidents,
            int(observation.archive_or_replay_failure),
            int(observation.serious_runtime_incident),
            observation.disk_threshold_state,
            observation.recovery_lookback_seconds,
            observation.hot_access_age_seconds,
            observation.recovery_session_id,
            int(observation.hot_access_evidence_complete),
        )
        cursor = connection.execute(
            """INSERT INTO adaptive_retention_samples(
                observed_at,verified_chunks,failed_chunks,physical_database_bytes,
                hot_used_bytes,freelist_reusable_bytes,cold_archive_bytes,
                cold_growth_bytes_per_day,raw_ws_growth_bytes_per_day,disk_free_bytes,
                disk_total_bytes,event_loop_lag_seconds,ws_queue_depth,ws_queue_capacity,
                ws_sequence_gaps,ws_resyncs,ws_reconnects,data_gap_incidents,
                archive_or_replay_failure,serious_runtime_incident,disk_threshold_state,
                recovery_lookback_seconds,hot_access_age_seconds,recovery_session_id,
                hot_access_evidence_complete
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(observed_at) DO NOTHING""",
            values,
        )
        if cursor.rowcount == 1:
            return True
        existing = connection.execute(
            "SELECT * FROM adaptive_retention_samples WHERE observed_at=?", (values[0],)
        ).fetchone()
        if existing is None:
            raise AdaptiveRetentionStateError("duplicate adaptive observation disappeared")
        columns = (
            "observed_at",
            "verified_chunks",
            "failed_chunks",
            "physical_database_bytes",
            "hot_used_bytes",
            "freelist_reusable_bytes",
            "cold_archive_bytes",
            "cold_growth_bytes_per_day",
            "raw_ws_growth_bytes_per_day",
            "disk_free_bytes",
            "disk_total_bytes",
            "event_loop_lag_seconds",
            "ws_queue_depth",
            "ws_queue_capacity",
            "ws_sequence_gaps",
            "ws_resyncs",
            "ws_reconnects",
            "data_gap_incidents",
            "archive_or_replay_failure",
            "serious_runtime_incident",
            "disk_threshold_state",
            "recovery_lookback_seconds",
            "hot_access_age_seconds",
            "recovery_session_id",
            "hot_access_evidence_complete",
        )
        if tuple(existing[column] for column in columns) != values:
            raise AdaptiveRetentionStateError(
                "conflicting adaptive observation fact shares an existing timestamp"
            )
        return False

    def _window_rows(
        self, connection: sqlite3.Connection, observed: datetime
    ) -> tuple[sqlite3.Row, ...]:
        cutoff = observed - self.policy.evidence_window
        return tuple(
            connection.execute(
                """SELECT * FROM adaptive_retention_samples
                WHERE observed_at>=? AND observed_at<=? ORDER BY observed_at""",
                (cutoff.isoformat(), observed.isoformat()),
            )
        )

    def _evaluate(
        self,
        connection: sqlite3.Connection,
        observation: AdaptiveRetentionObservation,
        state: sqlite3.Row,
        actual_retention_seconds: int,
        *,
        allow_adjustment: bool,
    ) -> AdaptiveRetentionStatus:
        observed = observation.observed_at.astimezone(UTC)
        rows = self._window_rows(connection, observed)
        first_at = _persisted_utc(rows[0]["observed_at"], "sample") if rows else None
        last_at = _persisted_utc(rows[-1]["observed_at"], "sample") if rows else None
        row_times = [_persisted_utc(row["observed_at"], "sample") for row in rows]
        maximum_continuous_interval = self.policy.reevaluation_interval * 2
        duration = sum(
            (right - left).total_seconds()
            for left, right in pairwise(row_times)
            if timedelta(0) <= right - left <= maximum_continuous_interval
        )
        recovery = [
            float(row["recovery_lookback_seconds"])
            for row in rows
            if row["recovery_lookback_seconds"] is not None
        ]
        accesses = [
            float(row["hot_access_age_seconds"])
            for row in rows
            if row["hot_access_age_seconds"] is not None
        ]
        verified_delta, failed_delta = self._counter_deltas(
            rows, "verified_chunks", "failed_chunks"
        )
        denominator = verified_delta + failed_delta
        reliability = None if denominator == 0 else verified_delta / denominator
        recent_cutoff = observed - self.policy.serious_incident_quiet_period
        incidents = self._incident_count(
            tuple(
                row for row in rows if _persisted_utc(row["observed_at"], "sample") >= recent_cutoff
            )
        )
        current = int(state["current_retention_seconds"])
        prior_disk_state = str(state["disk_pressure_state"])
        raw_disk_state = observation.disk_threshold_state
        disk_deescalation_streak = 0
        if _DISK_STATE_RANK[raw_disk_state] >= _DISK_STATE_RANK[prior_disk_state]:
            stable_disk_state = raw_disk_state
        else:
            disk_deescalation_streak = int(state["disk_deescalation_streak"]) + 1
            if disk_deescalation_streak >= self.policy.disk_deescalation_samples:
                stable_disk_state = raw_disk_state
                disk_deescalation_streak = 0
            else:
                stable_disk_state = prior_disk_state
        required_max = max((*recovery, *accesses), default=None)
        recovery_sessions = {
            str(row["recovery_session_id"])
            for row in rows
            if row["recovery_session_id"] is not None
            and row["recovery_lookback_seconds"] is not None
        }
        access_evidence_complete = bool(rows) and all(
            bool(row["hot_access_evidence_complete"]) for row in rows
        )
        evidence_sufficient = (
            duration >= self.policy.minimum_evidence_duration.total_seconds()
            and len(rows) >= self.policy.minimum_evidence_samples
            and verified_delta >= self.policy.minimum_verified_chunks
            and reliability == 1.0
            and bool(recovery)
            and bool(accesses)
            and len(recovery_sessions) >= self.policy.minimum_recovery_sessions
            and access_evidence_complete
            and observation.unresolved_data_gaps == 0
        )
        simulations = tuple(
            self._simulate(
                candidate,
                current=current,
                observation=observation,
                evidence_sufficient=evidence_sufficient,
                reliability=reliability,
                required_max=required_max,
                incidents=incidents,
            )
            for candidate in self.policy.candidates
        )
        recommended = self._recommend(current, simulations, observation, incidents)
        last_change = (
            _persisted_utc(state["last_change_at"], "last_change_at")
            if state["last_change_at"]
            else None
        )
        cooldown_until = None if last_change is None else last_change + self.policy.cooldown
        in_cooldown = cooldown_until is not None and observed < cooldown_until
        mode = AdaptiveRetentionMode.HOLD
        reason = "current retention remains the safest supported candidate"
        reason_code = RetentionReasonCode.CURRENT_BASELINE

        safety_increase = recommended > current
        if not evidence_sufficient and not safety_increase:
            mode = AdaptiveRetentionMode.INSUFFICIENT_EVIDENCE
            recommended = current
            reason = (
                "insufficient duration/samples, recovery/access evidence, verified archive "
                "volume, or resolved-gap evidence"
            )
            reason_code = RetentionReasonCode.INSUFFICIENT_EVIDENCE
        elif safety_increase:
            if allow_adjustment:
                mode = AdaptiveRetentionMode.SAFETY_INCREASE
                reason = "safety evidence requires more HOT history"
                reason_code = RetentionReasonCode.SAFETY_INCREASE
            else:
                mode = AdaptiveRetentionMode.RECOMMEND
                reason = "safety increase requires the recorder-owned apply path"
                reason_code = RetentionReasonCode.SAFETY_INCREASE
        elif recommended < current and in_cooldown:
            recommended = current
            reason = "retention change cooldown is active"
            reason_code = RetentionReasonCode.COOLDOWN_ACTIVE
        elif recommended < current:
            streak = (
                int(state["recommendation_streak"]) + 1
                if int(state["recommended_retention_seconds"]) == recommended
                else 1
            )
            if streak < self.policy.minimum_simulation_passes:
                mode = AdaptiveRetentionMode.RECOMMEND
                remaining = self.policy.minimum_simulation_passes - streak
                reason = f"candidate requires {remaining} more independent PASS simulations"
                reason_code = RetentionReasonCode.MORE_PASSES_REQUIRED
            elif self.policy.auto_adjust and allow_adjustment:
                mode = AdaptiveRetentionMode.ADJUSTED
                reason = "all verification, evidence, hysteresis, and cooldown gates passed"
                reason_code = RetentionReasonCode.GATES_PASSED
            else:
                mode = AdaptiveRetentionMode.RECOMMEND
                reason = "candidate passed; adjustment requires the recorder-owned apply path"
                reason_code = RetentionReasonCode.AUTO_ADJUST_DISABLED

        applied = (
            recommended
            if mode in {AdaptiveRetentionMode.ADJUSTED, AdaptiveRetentionMode.SAFETY_INCREASE}
            else current
        )
        if stable_disk_state == "fail_safe" or (
            stable_disk_state == "critical"
            and (observation.archive_or_replay_failure or reliability != 1.0)
        ):
            mode = AdaptiveRetentionMode.FAIL_SAFE
            applied = current
            recommended = current
            reason = "disk fail-safe requires controlled pause with raw truth preserved"
            reason_code = RetentionReasonCode.DISK_FAIL_SAFE
        last_adjustment = (
            _persisted_utc(state["last_change_at"], "last_change_at")
            if state["last_change_at"]
            else None
        )
        if applied != current:
            last_adjustment = observed
        next_eligible = max(
            observed + self.policy.reevaluation_interval,
            cooldown_until or observed,
        )
        return AdaptiveRetentionStatus(
            observed_at=observed,
            current_retention_seconds=applied,
            recommended_retention_seconds=recommended,
            actual_applied_retention_seconds=actual_retention_seconds,
            controller_mode=mode,
            evidence_window_start=first_at,
            evidence_window_end=last_at,
            evidence_duration_seconds=duration,
            evidence_samples=len(rows),
            recovery_lookback_p50_seconds=(None if not recovery else median(recovery)),
            recovery_lookback_p95_seconds=_percentile(recovery, 0.95),
            recovery_lookback_max_seconds=max(recovery, default=None),
            hot_access_age_p50_seconds=(None if not accesses else median(accesses)),
            hot_access_age_p95_seconds=_percentile(accesses, 0.95),
            hot_access_age_max_seconds=max(accesses, default=None),
            archive_reliability=reliability,
            verified_chunks_in_window=verified_delta,
            failed_chunks_in_window=failed_delta,
            serious_incidents_in_window=incidents,
            recovery_sessions_in_window=len(recovery_sessions),
            runtime_metrics=self._runtime_metrics(observation, stable_disk_state),
            simulations=simulations,
            reason=reason,
            reason_code=reason_code,
            cooldown_until=cooldown_until,
            last_adjustment_at=last_adjustment,
            next_eligible_adjustment_at=next_eligible,
            next_reevaluation_at=observed + self.policy.reevaluation_interval,
            disk_deescalation_streak=disk_deescalation_streak,
        )

    @staticmethod
    def _runtime_metrics(
        observation: AdaptiveRetentionObservation, disk_pressure_state: str | None = None
    ) -> RetentionRuntimeMetrics:
        daily_growth = observation.cold_growth_bytes_per_day
        days_to_full = (
            None
            if daily_growth is None or daily_growth <= 0
            else observation.disk_free_bytes / daily_growth
        )
        queue_pressure = (
            None
            if observation.ws_queue_capacity <= 0
            else observation.ws_queue_depth * 100 / observation.ws_queue_capacity
        )
        return RetentionRuntimeMetrics(
            physical_database_bytes=observation.physical_database_bytes,
            hot_used_bytes=observation.hot_used_bytes,
            freelist_reusable_bytes=observation.freelist_reusable_bytes,
            cold_archive_bytes=observation.cold_archive_bytes,
            cold_growth_bytes_per_day=observation.cold_growth_bytes_per_day,
            raw_ws_growth_bytes_per_day=observation.raw_ws_growth_bytes_per_day,
            disk_free_bytes=observation.disk_free_bytes,
            disk_total_bytes=observation.disk_total_bytes,
            estimated_days_to_full=days_to_full,
            event_loop_lag_seconds=observation.event_loop_lag_seconds,
            ws_queue_depth=observation.ws_queue_depth,
            ws_queue_capacity=observation.ws_queue_capacity,
            ws_queue_pressure_percent=queue_pressure,
            disk_pressure_state=disk_pressure_state or observation.disk_threshold_state,
        )

    @staticmethod
    def _counter_deltas(rows: tuple[sqlite3.Row, ...], *names: str) -> tuple[int, ...]:
        if len(rows) < 2:
            return tuple(0 for _ in names)
        for name in names:
            values = [int(row[name]) for row in rows]
            if any(right < left for left, right in pairwise(values)):
                raise AdaptiveRetentionStateError(
                    f"adaptive retention cumulative counter regressed: {name}"
                )
        deltas = tuple(int(rows[-1][name]) - int(rows[0][name]) for name in names)
        return deltas

    @staticmethod
    def _incident_count(rows: tuple[sqlite3.Row, ...]) -> int:
        if not rows:
            return 0
        count = sum(int(row["serious_runtime_incident"]) for row in rows)
        if len(rows) >= 2:
            data_gap_values = [int(row["data_gap_incidents"]) for row in rows]
            if any(right < left for left, right in pairwise(data_gap_values)):
                raise AdaptiveRetentionStateError(
                    "adaptive retention cumulative counter regressed: data_gap_incidents"
                )
            count += data_gap_values[-1] - data_gap_values[0]
        for name in ("ws_sequence_gaps", "ws_resyncs", "ws_reconnects"):
            prior: sqlite3.Row | None = None
            for row in rows:
                if prior is not None and row["recovery_session_id"] == prior["recovery_session_id"]:
                    delta = int(row[name]) - int(prior[name])
                    if delta < 0:
                        raise AdaptiveRetentionStateError(
                            f"adaptive retention cumulative counter regressed: {name}"
                        )
                    count += delta
                prior = row
        return count

    def _simulate(
        self,
        candidate: int,
        *,
        current: int,
        observation: AdaptiveRetentionObservation,
        evidence_sufficient: bool,
        reliability: float | None,
        required_max: float | None,
        incidents: int,
    ) -> CandidateSimulation:
        raw_daily = observation.raw_ws_growth_bytes_per_day
        projection_window = observation.raw_ws_observation_window_seconds
        if raw_daily is None or projection_window is None:
            estimate_confidence = EstimateConfidence.UNKNOWN
        elif (
            projection_window < self.policy.minimum_projection_window.total_seconds()
            or incidents
            or observation.unresolved_data_gaps != 0
            or observation.archive_or_replay_failure
        ):
            estimate_confidence = EstimateConfidence.LOW
        else:
            estimate_confidence = EstimateConfidence.SUFFICIENT
        if current <= 0 or raw_daily is None:
            estimated_hot = observation.hot_used_bytes
        else:
            current_ws = int(raw_daily * current / 86_400)
            fixed_hot = max(0, observation.hot_used_bytes - current_ws)
            estimated_hot = fixed_hot + int(raw_daily * candidate / 86_400)
        transition_savings = max(0, observation.hot_used_bytes - estimated_hot)
        required_with_margin = (
            None
            if required_max is None
            else required_max + self.policy.safety_margin.total_seconds()
        )
        if candidate == current:
            result, reason = RetentionSimulationResult.PASS, "current verified safety baseline"
        elif candidate > current:
            result, reason = RetentionSimulationResult.PASS, "longer retention increases safety"
        elif observation.archive_or_replay_failure or reliability not in {None, 1.0}:
            result, reason = (
                RetentionSimulationResult.FAIL,
                "archive/replay reliability gate failed",
            )
        elif incidents:
            result, reason = RetentionSimulationResult.HOLD, "recent serious runtime incident"
        elif raw_daily is None:
            result, reason = RetentionSimulationResult.HOLD, "WS raw growth projection unavailable"
        elif estimate_confidence is not EstimateConfidence.SUFFICIENT:
            result, reason = (
                RetentionSimulationResult.HOLD,
                "WS growth projection window is too short",
            )
        elif not evidence_sufficient or required_with_margin is None:
            result, reason = RetentionSimulationResult.HOLD, "insufficient evidence"
        elif candidate <= required_with_margin:
            result, reason = (
                RetentionSimulationResult.FAIL,
                "candidate does not exceed required lookback plus margin",
            )
        else:
            result, reason = (
                RetentionSimulationResult.PASS,
                "all non-destructive safety gates passed",
            )
        return CandidateSimulation(
            retention_seconds=candidate,
            result=result,
            estimated_hot_bytes=estimated_hot,
            estimated_transition_savings_bytes=transition_savings,
            estimated_disk_savings_first_day=transition_savings,
            required_recovery_seconds=required_with_margin,
            estimate_confidence=estimate_confidence,
            estimate_observation_window_seconds=projection_window,
            reason=reason,
        )

    def _recommend(
        self,
        current: int,
        simulations: tuple[CandidateSimulation, ...],
        observation: AdaptiveRetentionObservation,
        incidents: int,
    ) -> int:
        ladder = self.policy.candidates
        index = ladder.index(current)
        if (
            observation.archive_or_replay_failure
            or incidents
            or (observation.unresolved_data_gaps or 0) > 0
        ):
            return ladder[max(0, index - 1)]
        if (
            index + 1 < len(ladder)
            and simulations[index + 1].result is RetentionSimulationResult.PASS
        ):
            return ladder[index + 1]
        return current

    def _persist_status(
        self,
        connection: sqlite3.Connection,
        status: AdaptiveRetentionStatus,
        prior: sqlite3.Row,
    ) -> None:
        changed = status.current_retention_seconds != int(prior["current_retention_seconds"])
        recommendation_streak = (
            int(prior["recommendation_streak"]) + 1
            if status.recommended_retention_seconds == int(prior["recommended_retention_seconds"])
            else 1
        )
        connection.execute(
            """UPDATE adaptive_retention_state SET
            current_retention_seconds=?,recommended_retention_seconds=?,
            recommendation_streak=?,last_change_at=?,next_reevaluation_at=?,
            last_status_json=?,last_observed_at=?,disk_pressure_state=?,
            disk_deescalation_streak=?,updated_at=? WHERE singleton=1""",
            (
                status.current_retention_seconds,
                status.recommended_retention_seconds,
                recommendation_streak,
                status.observed_at.isoformat() if changed else prior["last_change_at"],
                status.next_reevaluation_at.isoformat(),
                json.dumps(status.as_dict(), sort_keys=True, separators=(",", ":")),
                status.observed_at.isoformat(),
                status.runtime_metrics.disk_pressure_state,
                status.disk_deescalation_streak,
                status.observed_at.isoformat(),
            ),
        )

    def _prune_samples(self, connection: sqlite3.Connection, observed: datetime) -> None:
        cutoff = observed - self.policy.evidence_window - self.policy.reevaluation_interval
        connection.execute(
            "DELETE FROM adaptive_retention_samples WHERE observed_at<?", (cutoff.isoformat(),)
        )

    @staticmethod
    def _status_from_json(raw: str) -> AdaptiveRetentionStatus:
        payload: dict[str, Any] = json.loads(raw)
        evidence = payload["evidence_window"]
        recovery = payload["recovery_lookback_seconds"]
        access = payload["hot_access_age_seconds"]
        runtime = payload.get("runtime_metrics") or {
            "physical_database_bytes": 0,
            "hot_used_bytes": 0,
            "freelist_reusable_bytes": 0,
            "cold_archive_bytes": 0,
            "cold_growth_bytes_per_day": None,
            "raw_ws_growth_bytes_per_day": None,
            "disk_free_bytes": 0,
            "disk_total_bytes": 0,
            "estimated_days_to_full": None,
            "event_loop_lag_seconds": 0.0,
            "ws_queue_depth": 0,
            "ws_queue_capacity": 0,
            "ws_queue_pressure_percent": None,
        }
        simulations = tuple(
            CandidateSimulation(
                retention_seconds=int(item["retention_seconds"]),
                result=RetentionSimulationResult(item["result"]),
                estimated_hot_bytes=int(item["estimated_hot_bytes"]),
                estimated_transition_savings_bytes=int(item["estimated_transition_savings_bytes"]),
                estimated_disk_savings_first_day=int(item["estimated_disk_savings_first_day"]),
                required_recovery_seconds=item["required_recovery_seconds"],
                estimate_confidence=EstimateConfidence(item.get("estimate_confidence", "UNKNOWN")),
                estimate_observation_window_seconds=item.get("estimate_observation_window_seconds"),
                reason=str(item["reason"]),
            )
            for _, item in sorted(
                payload["simulation_results"].items(), key=lambda pair: int(pair[0]), reverse=True
            )
        )
        return AdaptiveRetentionStatus(
            observed_at=_persisted_utc(payload["observed_at"], "cached observed_at"),
            current_retention_seconds=int(payload["current_retention_seconds"]),
            recommended_retention_seconds=int(payload["recommended_retention_seconds"]),
            actual_applied_retention_seconds=int(
                payload.get(
                    "actual_applied_retention_seconds",
                    payload["current_retention_seconds"],
                )
            ),
            controller_mode=AdaptiveRetentionMode(payload["controller_mode"]),
            evidence_window_start=(
                None
                if evidence["start"] is None
                else _persisted_utc(evidence["start"], "cached evidence start")
            ),
            evidence_window_end=(
                None
                if evidence["end"] is None
                else _persisted_utc(evidence["end"], "cached evidence end")
            ),
            evidence_duration_seconds=float(evidence["duration_seconds"]),
            evidence_samples=int(evidence["samples"]),
            recovery_lookback_p50_seconds=recovery["p50"],
            recovery_lookback_p95_seconds=recovery["p95"],
            recovery_lookback_max_seconds=recovery["max"],
            hot_access_age_p50_seconds=access["p50"],
            hot_access_age_p95_seconds=access["p95"],
            hot_access_age_max_seconds=access["max"],
            archive_reliability=payload["archive_reliability"],
            verified_chunks_in_window=int(payload["verified_chunks_in_window"]),
            failed_chunks_in_window=int(payload["failed_chunks_in_window"]),
            serious_incidents_in_window=int(payload["serious_incidents_in_window"]),
            recovery_sessions_in_window=int(payload.get("recovery_sessions_in_window", 0)),
            runtime_metrics=RetentionRuntimeMetrics(
                physical_database_bytes=int(runtime["physical_database_bytes"]),
                hot_used_bytes=int(runtime["hot_used_bytes"]),
                freelist_reusable_bytes=int(runtime["freelist_reusable_bytes"]),
                cold_archive_bytes=int(runtime["cold_archive_bytes"]),
                cold_growth_bytes_per_day=runtime["cold_growth_bytes_per_day"],
                raw_ws_growth_bytes_per_day=runtime["raw_ws_growth_bytes_per_day"],
                disk_free_bytes=int(runtime["disk_free_bytes"]),
                disk_total_bytes=int(runtime["disk_total_bytes"]),
                estimated_days_to_full=runtime["estimated_days_to_full"],
                event_loop_lag_seconds=float(runtime["event_loop_lag_seconds"]),
                ws_queue_depth=int(runtime["ws_queue_depth"]),
                ws_queue_capacity=int(runtime["ws_queue_capacity"]),
                ws_queue_pressure_percent=runtime["ws_queue_pressure_percent"],
                disk_pressure_state=str(runtime.get("disk_pressure_state", "normal")),
            ),
            simulations=simulations,
            reason=str(payload["reason"]),
            reason_code=RetentionReasonCode(
                payload.get("reason_code", RetentionReasonCode.CURRENT_BASELINE.value)
            ),
            cooldown_until=(
                None
                if payload["cooldown_until"] is None
                else _persisted_utc(payload["cooldown_until"], "cached cooldown")
            ),
            last_adjustment_at=(
                None
                if payload.get("last_adjustment_at") is None
                else _persisted_utc(payload["last_adjustment_at"], "cached last adjustment")
            ),
            next_eligible_adjustment_at=(
                None
                if payload.get("next_eligible_adjustment_at") is None
                else _persisted_utc(
                    payload["next_eligible_adjustment_at"], "cached next eligible adjustment"
                )
            ),
            next_reevaluation_at=_persisted_utc(
                payload["next_reevaluation_at"], "cached next reevaluation"
            ),
            disk_deescalation_streak=int(payload.get("disk_deescalation_streak", 0)),
        )


def write_adaptive_retention_status(path: Path, status: AdaptiveRetentionStatus) -> None:
    """Atomically publish one machine-readable runtime status outside source control."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(status.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
