from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from live15_quant.adaptive_retention import (
    AdaptiveRetentionController,
    AdaptiveRetentionMode,
    AdaptiveRetentionObservation,
    AdaptiveRetentionPolicy,
    AdaptiveRetentionStateError,
    EstimateConfidence,
    RetentionReasonCode,
    RetentionSimulationResult,
    write_adaptive_retention_status,
)

START = datetime(2026, 8, 20, tzinfo=UTC)


def policy(**changes: object) -> AdaptiveRetentionPolicy:
    values: dict[str, object] = {
        "evidence_window": timedelta(hours=12),
        "minimum_evidence_duration": timedelta(hours=2),
        "minimum_verified_chunks": 2,
        "minimum_evidence_samples": 2,
        "minimum_recovery_sessions": 1,
        "minimum_simulation_passes": 2,
        "safety_margin": timedelta(minutes=30),
        "cooldown": timedelta(0),
        "reevaluation_interval": timedelta(hours=1),
        "serious_incident_quiet_period": timedelta(hours=1),
        "minimum_projection_window": timedelta(hours=1),
    }
    values.update(changes)
    return AdaptiveRetentionPolicy(**values)


def observation(
    at: datetime, *, verified: int = 0, **changes: object
) -> AdaptiveRetentionObservation:
    values: dict[str, object] = {
        "observed_at": at,
        "verified_chunks": verified,
        "failed_chunks": 0,
        "physical_database_bytes": 12_000,
        "hot_used_bytes": 6_000,
        "freelist_reusable_bytes": 1_000,
        "cold_archive_bytes": 500,
        "cold_growth_bytes_per_day": 200.0,
        "raw_ws_growth_bytes_per_day": 24_000.0,
        "raw_ws_observation_window_seconds": 3_600.0,
        "disk_free_bytes": 80_000,
        "disk_total_bytes": 100_000,
        "event_loop_lag_seconds": 0.01,
        "ws_queue_depth": 10,
        "ws_queue_capacity": 100,
        "ws_sequence_gaps": 0,
        "ws_resyncs": 0,
        "ws_reconnects": 0,
        "data_gap_incidents": 0,
        "unresolved_data_gaps": 0,
        "archive_or_replay_failure": False,
        "serious_runtime_incident": False,
        "disk_threshold_state": "normal",
        "recovery_lookback_seconds": 60.0,
        "hot_access_age_seconds": 120.0,
        "recovery_session_id": "session-1",
        "hot_access_evidence_complete": True,
    }
    values.update(changes)
    return AdaptiveRetentionObservation(**values)


def test_insufficient_evidence_holds_six_hours_and_is_resumable(tmp_path) -> None:
    path = tmp_path / "controller.sqlite3"
    controller = AdaptiveRetentionController(path, policy(), initial_retention_seconds=21_600)

    first = controller.evaluate_once(observation(START))
    resumed = AdaptiveRetentionController(path, policy(), initial_retention_seconds=21_600)
    cached = resumed.evaluate_once(observation(START + timedelta(minutes=30), verified=1))

    assert first.controller_mode is AdaptiveRetentionMode.INSUFFICIENT_EVIDENCE
    assert first.current_retention_seconds == 21_600
    assert first.recommended_retention_seconds == 21_600
    assert cached.current_retention_seconds == first.current_retention_seconds
    assert cached.next_reevaluation_at == first.next_reevaluation_at
    assert cached.observed_at == START + timedelta(minutes=30)
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM adaptive_retention_samples").fetchone()[0] == 1
        )


def test_repeated_independent_passes_adjust_only_one_ladder_step(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3", policy(), initial_retention_seconds=21_600
    )
    controller.evaluate_once(observation(START))
    recommendation = controller.evaluate_once(observation(START + timedelta(hours=2), verified=2))
    adjusted = controller.evaluate_once(
        observation(START + timedelta(hours=3), verified=3),
        allow_adjustment=True,
    )

    assert recommendation.controller_mode is AdaptiveRetentionMode.RECOMMEND
    assert recommendation.recommended_retention_seconds == 14_400
    assert recommendation.current_retention_seconds == 21_600
    assert adjusted.controller_mode is AdaptiveRetentionMode.ADJUSTED
    assert adjusted.current_retention_seconds == 14_400
    assert adjusted.recommended_retention_seconds == 14_400


def test_required_recovery_plus_margin_blocks_unsafe_candidate(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3", policy(), initial_retention_seconds=21_600
    )
    controller.evaluate_once(
        observation(START, recovery_lookback_seconds=14_000.0, hot_access_age_seconds=10.0)
    )
    status = controller.evaluate_once(
        observation(
            START + timedelta(hours=2),
            verified=2,
            recovery_lookback_seconds=14_000.0,
            hot_access_age_seconds=10.0,
        )
    )

    candidate = next(item for item in status.simulations if item.retention_seconds == 14_400)
    assert candidate.result is RetentionSimulationResult.FAIL
    assert status.current_retention_seconds == 21_600


def test_incident_safety_increases_one_step_and_never_jumps(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3", policy(), initial_retention_seconds=10_800
    )
    status = controller.evaluate_once(
        observation(START, serious_runtime_incident=True, ws_sequence_gaps=1),
        allow_adjustment=True,
    )

    assert status.controller_mode is AdaptiveRetentionMode.SAFETY_INCREASE
    assert status.current_retention_seconds == 14_400


def test_counter_regression_fails_loudly(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3", policy(), initial_retention_seconds=21_600
    )
    controller.evaluate_once(observation(START, verified=5))

    with pytest.raises(RuntimeError, match="counter regressed"):
        controller.evaluate_once(observation(START + timedelta(hours=2), verified=4))


def test_fail_safe_disk_pressure_never_bypasses_archive_verification(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3", policy(), initial_retention_seconds=21_600
    )
    status = controller.evaluate_once(
        observation(
            START,
            failed_chunks=1,
            archive_or_replay_failure=True,
            disk_threshold_state="fail_safe",
        )
    )

    assert status.controller_mode is AdaptiveRetentionMode.FAIL_SAFE
    assert status.current_retention_seconds == 21_600
    assert "controlled pause" in status.reason


def test_disk_fail_safe_pauses_even_when_archive_evidence_is_reliable(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3", policy(), initial_retention_seconds=21_600
    )
    controller.evaluate_once(observation(START, verified=1))
    status = controller.evaluate_once(
        observation(START + timedelta(hours=2), verified=3, disk_threshold_state="fail_safe")
    )

    assert status.archive_reliability == 1.0
    assert status.controller_mode is AdaptiveRetentionMode.FAIL_SAFE
    assert status.current_retention_seconds == 21_600
    assert status.reason_code is RetentionReasonCode.DISK_FAIL_SAFE


def test_archive_failure_blocks_normal_pressure_retention_decrease(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3", policy(), initial_retention_seconds=21_600
    )
    controller.evaluate_once(observation(START, verified=1))
    status = controller.evaluate_once(
        observation(
            START + timedelta(hours=2),
            verified=3,
            failed_chunks=1,
            archive_or_replay_failure=True,
        ),
        allow_adjustment=True,
    )
    candidate = next(item for item in status.simulations if item.retention_seconds == 14_400)

    assert candidate.result is RetentionSimulationResult.FAIL
    assert status.current_retention_seconds == 21_600


def test_status_is_machine_readable_and_atomic(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3", policy(), initial_retention_seconds=21_600
    )
    status = controller.evaluate_once(observation(START))
    output = tmp_path / "status.json"

    write_adaptive_retention_status(output, status)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["controller_mode"] == "INSUFFICIENT_EVIDENCE"
    assert set(payload["simulation_results"]) == {"21600", "14400", "10800", "7200", "3600"}
    assert not output.with_name(f".{output.name}.tmp").exists()


def test_policy_rejects_floor_outside_hard_ladder() -> None:
    with pytest.raises(ValueError, match="policy is invalid"):
        replace(policy(), minimum_seconds=900)


def test_controller_state_schema_migration_is_additive_and_order_independent(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE adaptive_retention_samples(
            observed_at TEXT PRIMARY KEY, verified_chunks INTEGER NOT NULL,
            failed_chunks INTEGER NOT NULL, physical_database_bytes INTEGER NOT NULL,
            hot_used_bytes INTEGER NOT NULL, freelist_reusable_bytes INTEGER NOT NULL,
            cold_archive_bytes INTEGER NOT NULL, cold_growth_bytes_per_day REAL,
            disk_free_bytes INTEGER NOT NULL, disk_total_bytes INTEGER NOT NULL,
            event_loop_lag_seconds REAL NOT NULL, ws_queue_depth INTEGER NOT NULL,
            ws_queue_capacity INTEGER NOT NULL, ws_sequence_gaps INTEGER NOT NULL,
            ws_resyncs INTEGER NOT NULL, ws_reconnects INTEGER NOT NULL,
            archive_or_replay_failure INTEGER NOT NULL,
            serious_runtime_incident INTEGER NOT NULL, disk_threshold_state TEXT NOT NULL,
            recovery_lookback_seconds REAL, hot_access_age_seconds REAL
            ) STRICT"""
        )

    controller = AdaptiveRetentionController(path, policy(), initial_retention_seconds=21_600)
    status = controller.evaluate_once(observation(START))

    assert status.controller_mode is AdaptiveRetentionMode.INSUFFICIENT_EVIDENCE
    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(adaptive_retention_samples)")
        }
    assert {
        "raw_ws_growth_bytes_per_day",
        "data_gap_incidents",
        "recovery_session_id",
        "hot_access_evidence_complete",
    } <= columns
    with sqlite3.connect(path) as connection:
        state_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(adaptive_retention_state)")
        }
    assert {"disk_pressure_state", "disk_deescalation_streak"} <= state_columns


@pytest.mark.parametrize(
    ("changes", "policy_changes"),
    [
        ({"recovery_lookback_seconds": None}, {}),
        ({"hot_access_age_seconds": None}, {}),
        ({"hot_access_evidence_complete": False}, {}),
        ({"unresolved_data_gaps": None}, {}),
        ({}, {"minimum_evidence_samples": 3}),
        ({}, {"minimum_verified_chunks": 3}),
    ],
)
def test_missing_or_small_evidence_never_shortens_retention(
    tmp_path, changes: dict[str, object], policy_changes: dict[str, object]
) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3",
        policy(**policy_changes),
        initial_retention_seconds=21_600,
    )
    controller.evaluate_once(observation(START, **changes))
    status = controller.evaluate_once(
        observation(START + timedelta(hours=2), verified=2, **changes),
        allow_adjustment=True,
    )

    assert status.controller_mode is AdaptiveRetentionMode.INSUFFICIENT_EVIDENCE
    assert status.current_retention_seconds == 21_600


def test_short_projection_window_is_low_confidence_and_cannot_adjust(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3",
        policy(minimum_projection_window=timedelta(hours=4)),
        initial_retention_seconds=21_600,
    )
    controller.evaluate_once(observation(START))
    status = controller.evaluate_once(
        observation(START + timedelta(hours=2), verified=2), allow_adjustment=True
    )
    candidate = next(item for item in status.simulations if item.retention_seconds == 14_400)

    assert candidate.result is RetentionSimulationResult.HOLD
    assert candidate.estimate_confidence is EstimateConfidence.LOW
    assert candidate.estimate_observation_window_seconds == 3600
    assert status.current_retention_seconds == 21_600


def test_unresolved_gap_triggers_only_one_step_safety_increase(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3", policy(), initial_retention_seconds=10_800
    )
    status = controller.evaluate_once(
        observation(START, unresolved_data_gaps=1), allow_adjustment=True
    )

    assert status.current_retention_seconds == 14_400
    assert status.reason_code is RetentionReasonCode.SAFETY_INCREASE


def test_cooldown_blocks_immediate_second_decrease(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3",
        policy(cooldown=timedelta(hours=10)),
        initial_retention_seconds=21_600,
    )
    controller.evaluate_once(observation(START))
    controller.evaluate_once(observation(START + timedelta(hours=2), verified=2))
    adjusted = controller.evaluate_once(
        observation(START + timedelta(hours=3), verified=3), allow_adjustment=True
    )
    held = controller.evaluate_once(
        observation(START + timedelta(hours=4), verified=4),
        actual_retention_seconds=14_400,
        allow_adjustment=True,
    )

    assert adjusted.current_retention_seconds == 14_400
    assert adjusted.actual_applied_retention_seconds == 21_600
    assert held.current_retention_seconds == 14_400
    assert held.reason_code is RetentionReasonCode.COOLDOWN_ACTIVE


def test_inspect_only_cli_semantics_never_writes_or_advances_streak(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    controller = AdaptiveRetentionController(path, policy(), initial_retention_seconds=21_600)
    status = controller.evaluate_once(observation(START), record_evidence=False)

    assert status.controller_mode is AdaptiveRetentionMode.INSUFFICIENT_EVIDENCE
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM adaptive_retention_samples").fetchone()[0] == 0
        )
        streak = connection.execute(
            "SELECT recommendation_streak FROM adaptive_retention_state WHERE singleton=1"
        ).fetchone()[0]
    assert streak == 0


def test_actual_and_persisted_retention_must_reconcile(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3", policy(), initial_retention_seconds=21_600
    )
    with pytest.raises(AdaptiveRetentionStateError, match="do not reconcile"):
        controller.evaluate_once(observation(START), actual_retention_seconds=14_400)


def test_wall_clock_rollback_holds_without_adding_evidence(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    controller = AdaptiveRetentionController(path, policy(), initial_retention_seconds=21_600)
    controller.evaluate_once(observation(START))
    status = controller.evaluate_once(observation(START - timedelta(minutes=1)))

    assert status.controller_mode is AdaptiveRetentionMode.HOLD
    assert status.reason_code is RetentionReasonCode.CLOCK_ROLLBACK
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM adaptive_retention_samples").fetchone()[0] == 1
        )


def test_crash_during_recommendation_rolls_back_observation_and_state(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "state.sqlite3"
    controller = AdaptiveRetentionController(path, policy(), initial_retention_seconds=21_600)

    def crash(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(AdaptiveRetentionController, "_persist_status", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        controller.evaluate_once(observation(START))

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM adaptive_retention_samples").fetchone()[0] == 0
        )
        current = connection.execute(
            "SELECT current_retention_seconds FROM adaptive_retention_state WHERE singleton=1"
        ).fetchone()[0]
    assert current == 21_600


def test_corrupt_cached_status_and_controller_database_never_become_pass(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    controller = AdaptiveRetentionController(path, policy(), initial_retention_seconds=21_600)
    controller.evaluate_once(observation(START))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE adaptive_retention_state SET last_status_json='not-json' WHERE singleton=1"
        )
    with pytest.raises(AdaptiveRetentionStateError, match="cached status is corrupt"):
        controller.evaluate_once(observation(START + timedelta(minutes=1)))

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(AdaptiveRetentionStateError, match="unavailable or corrupt"):
        AdaptiveRetentionController(corrupt, policy(), initial_retention_seconds=21_600)


def test_future_schema_fails_loudly(tmp_path) -> None:
    path = tmp_path / "future.sqlite3"
    controller = AdaptiveRetentionController(path, policy(), initial_retention_seconds=21_600)
    del controller
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE adaptive_retention_meta SET schema_version=999 WHERE singleton=1"
        )
    with pytest.raises(AdaptiveRetentionStateError, match="future schema"):
        AdaptiveRetentionController(path, policy(), initial_retention_seconds=21_600)


def test_duplicate_process_evaluation_cannot_double_decrease(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    seed = AdaptiveRetentionController(path, policy(), initial_retention_seconds=21_600)
    seed.evaluate_once(observation(START))
    seed.evaluate_once(observation(START + timedelta(hours=2), verified=2))
    controllers = tuple(
        AdaptiveRetentionController(path, policy(), initial_retention_seconds=21_600)
        for _ in range(2)
    )

    def evaluate(controller: AdaptiveRetentionController) -> object:
        try:
            return controller.evaluate_once(
                observation(START + timedelta(hours=3), verified=3),
                actual_retention_seconds=21_600,
                allow_adjustment=True,
            )
        except AdaptiveRetentionStateError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(evaluate, controllers))

    resumed = AdaptiveRetentionController(path, policy(), initial_retention_seconds=21_600)
    assert resumed.current_retention_seconds() == 14_400
    assert sum(isinstance(item, AdaptiveRetentionStateError) for item in outcomes) <= 1


def test_sqlite_transaction_lock_is_released_after_owner_crash_or_close(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    controller = AdaptiveRetentionController(path, policy(), initial_retention_seconds=21_600)
    lock = sqlite3.connect(path)
    lock.execute("BEGIN IMMEDIATE")
    lock.close()

    status = controller.evaluate_once(observation(START))

    assert status.current_retention_seconds == 21_600


def test_identical_observation_is_idempotent_but_conflicting_fact_fails(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    controller = AdaptiveRetentionController(path, policy(), initial_retention_seconds=21_600)
    first = controller.evaluate_once(observation(START))
    duplicate = controller.evaluate_once(observation(START))

    assert duplicate.current_retention_seconds == first.current_retention_seconds
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM adaptive_retention_samples").fetchone()[0] == 1
        )
        assert (
            connection.execute(
                "SELECT recommendation_streak FROM adaptive_retention_state WHERE singleton=1"
            ).fetchone()[0]
            == 1
        )

    with pytest.raises(AdaptiveRetentionStateError, match="conflicting adaptive observation"):
        controller.evaluate_once(observation(START, physical_database_bytes=12_001))


def test_offline_wall_clock_gap_does_not_count_as_continuous_evidence(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3",
        policy(minimum_evidence_duration=timedelta(hours=4)),
        initial_retention_seconds=21_600,
    )
    controller.evaluate_once(observation(START))
    status = controller.evaluate_once(
        observation(START + timedelta(days=4), verified=200), allow_adjustment=True
    )

    assert status.evidence_duration_seconds == 0
    assert status.controller_mode is AdaptiveRetentionMode.INSUFFICIENT_EVIDENCE
    assert status.current_retention_seconds == 21_600


def test_counter_regression_between_window_endpoints_fails_loudly(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3",
        policy(minimum_evidence_samples=4),
        initial_retention_seconds=21_600,
    )
    controller.evaluate_once(observation(START, verified=5))
    controller.evaluate_once(observation(START + timedelta(hours=1), verified=7))
    controller.evaluate_once(observation(START + timedelta(hours=2), verified=8))
    with sqlite3.connect(controller.path) as connection:
        connection.execute(
            "UPDATE adaptive_retention_samples SET verified_chunks=4 WHERE observed_at=?",
            ((START + timedelta(hours=1)).isoformat(),),
        )

    with pytest.raises(AdaptiveRetentionStateError, match="counter regressed"):
        controller.evaluate_once(observation(START + timedelta(hours=3), verified=9))


def test_ws_counter_reset_requires_a_new_recovery_session(tmp_path) -> None:
    same_session = AdaptiveRetentionController(
        tmp_path / "same.sqlite3", policy(), initial_retention_seconds=21_600
    )
    same_session.evaluate_once(observation(START, ws_reconnects=2))
    with pytest.raises(AdaptiveRetentionStateError, match="counter regressed"):
        same_session.evaluate_once(observation(START + timedelta(hours=1), ws_reconnects=0))

    restarted = AdaptiveRetentionController(
        tmp_path / "restart.sqlite3", policy(), initial_retention_seconds=21_600
    )
    restarted.evaluate_once(observation(START, ws_reconnects=2))
    status = restarted.evaluate_once(
        observation(
            START + timedelta(hours=1),
            ws_reconnects=0,
            recovery_session_id="session-2",
        )
    )
    assert status.current_retention_seconds == 21_600


def test_restart_preserves_adjustment_and_cooldown(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    retention_policy = policy(cooldown=timedelta(hours=10))
    controller = AdaptiveRetentionController(
        path, retention_policy, initial_retention_seconds=21_600
    )
    controller.evaluate_once(observation(START))
    controller.evaluate_once(observation(START + timedelta(hours=2), verified=2))
    controller.evaluate_once(
        observation(START + timedelta(hours=3), verified=3), allow_adjustment=True
    )

    resumed = AdaptiveRetentionController(path, retention_policy, initial_retention_seconds=21_600)
    held = resumed.evaluate_once(
        observation(START + timedelta(hours=4), verified=4),
        actual_retention_seconds=14_400,
        allow_adjustment=True,
    )

    assert resumed.current_retention_seconds() == 14_400
    assert held.reason_code is RetentionReasonCode.COOLDOWN_ACTIVE
    assert held.current_retention_seconds == 14_400


def test_disk_pressure_escalates_immediately_and_recovers_with_hysteresis(tmp_path) -> None:
    controller = AdaptiveRetentionController(
        tmp_path / "state.sqlite3",
        policy(disk_deescalation_samples=3),
        initial_retention_seconds=21_600,
    )
    failed = {
        "failed_chunks": 1,
        "archive_or_replay_failure": True,
    }
    critical = controller.evaluate_once(
        observation(START, disk_threshold_state="critical", **failed)
    )
    first_safe = controller.evaluate_once(
        observation(START + timedelta(hours=1), disk_threshold_state="normal", **failed)
    )
    second_safe = controller.evaluate_once(
        observation(START + timedelta(hours=2), disk_threshold_state="normal", **failed)
    )
    recovered = controller.evaluate_once(
        observation(START + timedelta(hours=3), disk_threshold_state="normal", **failed)
    )

    assert critical.runtime_metrics.disk_pressure_state == "critical"
    assert first_safe.runtime_metrics.disk_pressure_state == "critical"
    assert first_safe.disk_deescalation_streak == 1
    assert second_safe.runtime_metrics.disk_pressure_state == "critical"
    assert second_safe.disk_deescalation_streak == 2
    assert recovered.runtime_metrics.disk_pressure_state == "normal"
    assert recovered.disk_deescalation_streak == 0
