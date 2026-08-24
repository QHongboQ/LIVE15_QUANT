from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import live15_quant.demo_first_fill as demo_first_fill_module
from live15_quant.demo_execution import (
    DemoIntent,
    DemoIntentPurpose,
    DemoLifecycleState,
    DemoReconciliationResult,
    DemoRiskDecision,
    DemoRiskReason,
    stable_client_order_id,
)
from live15_quant.demo_first_fill import (
    DemoFirstFillAlreadyRunning,
    DemoFirstFillError,
    DemoFirstFillLease,
    DemoFirstFillStatus,
    DemoFirstFillStatusStore,
    DemoFirstFillWorker,
    _configure_worker_log,
    _initial_status,
    _latency_diagnostics,
)
from live15_quant.providers.kalshi_demo_execution import DemoBookSide


def _intent() -> DemoIntent:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    return DemoIntent(
        model_id="logistic_l2_identity",
        model_artifact_hash="a" * 64,
        decision_id="decision",
        event_id="ticker",
        opportunity_id="opportunity",
        ticker="ticker",
        side=DemoBookSide.BID,
        count=Decimal("1"),
        price=Decimal("0.50"),
        probability=Decimal("0.70"),
        edge=Decimal("0.20"),
        decision_timestamp=now,
        purpose=DemoIntentPurpose.EXECUTION_SMOKE,
    )


def _worker(tmp_path: Path, coordinator: object, reader) -> DemoFirstFillWorker:
    started = datetime(2026, 8, 24, tzinfo=UTC)
    return DemoFirstFillWorker(
        settings=SimpleNamespace(),
        coordinator=coordinator,
        candidate_reader=reader,
        status_store=DemoFirstFillStatusStore(tmp_path / "status.json"),
        status=_initial_status(started),
        logger=logging.getLogger("test.demo_first_fill"),
        utc_now=lambda: started,
        sleep=lambda _seconds: None,
    )


def test_no_signal_keeps_worker_waiting_and_status_is_atomic(tmp_path) -> None:
    class Coordinator:
        def submit(self, *_args):
            pytest.fail("submit must not run without a signal")

        def reconcile_positions(self) -> int:
            return 0

    worker = _worker(tmp_path, Coordinator(), lambda _settings: ())
    assert worker.run_once() is None
    status = worker.status_store.read()
    assert status is not None
    assert status["status"] == DemoFirstFillStatus.WAITING_SIGNAL.value
    assert not list(tmp_path.glob(".*.tmp"))


def test_no_signal_scans_at_quarter_second_cadence_but_logs_heartbeat_every_five_seconds(
    tmp_path,
) -> None:
    class Coordinator:
        def reconcile_positions(self) -> int:
            return 0

    current = datetime(2026, 8, 24, tzinfo=UTC)
    worker = _worker(tmp_path, Coordinator(), lambda _settings: ())
    worker.utc_now = lambda: current
    worker.signal_scan_seconds = 0.25
    worker.heartbeat_log_seconds = 5.0
    waiting_messages: list[str] = []

    class Logger:
        def info(self, message: str, **_kwargs) -> None:
            waiting_messages.append(message)

    worker.logger = Logger()
    assert worker.run_once() is None
    current = current.replace(microsecond=250000)
    assert worker.run_once() is None
    current = current.replace(second=4, microsecond=999999)
    assert worker.run_once() is None
    current = current.replace(second=5, microsecond=0)
    assert worker.run_once() is None
    assert waiting_messages == [
        "Demo first-fill waiting for fresh actionable signal",
        "Demo first-fill waiting for fresh actionable signal",
    ]
    assert worker.status["last_heartbeat"] == "2026-08-24T00:00:05.000000+00:00"


def test_actionable_signal_is_evaluated_on_next_scan_without_waiting_for_heartbeat(
    tmp_path,
) -> None:
    class Coordinator:
        calls = 0

        def submit(self, *_args):
            self.calls += 1
            return DemoRiskDecision(False, (DemoRiskReason.PRE_SUBMIT_DATA_UNAVAILABLE,))

        def reconcile_positions(self) -> int:
            return 0

    current = datetime(2026, 8, 24, tzinfo=UTC)
    coordinator = Coordinator()
    scan_count = 0

    def reader(_settings):
        nonlocal scan_count
        scan_count += 1
        return () if scan_count == 1 else (_intent(),)

    worker = _worker(tmp_path, coordinator, reader)
    worker.utc_now = lambda: current
    worker.signal_scan_seconds = 0.25
    worker.heartbeat_log_seconds = 5.0
    sleeps: list[float] = []

    def bounded_sleep(seconds: float) -> None:
        nonlocal current
        sleeps.append(seconds)
        current += timedelta(seconds=seconds)

    worker.sleep = bounded_sleep
    worker.stop_requested = lambda: coordinator.calls == 1
    assert worker.run_forever() == "USER_STOP_REQUESTED"
    assert coordinator.calls == 1
    assert sleeps == [0.25, 0.25]
    assert current == datetime(2026, 8, 24, 0, 0, 0, 500000, tzinfo=UTC)


def test_guard_blocked_candidate_keeps_worker_alive(tmp_path) -> None:
    class Coordinator:
        def submit(self, *_args):
            return DemoRiskDecision(False, (DemoRiskReason.PRE_SUBMIT_DATA_UNAVAILABLE,))

        def reconcile_positions(self) -> int:
            return 0

    worker = _worker(tmp_path, Coordinator(), lambda _settings: (_intent(),))
    assert worker.run_once() is None
    assert worker.status["status"] == DemoFirstFillStatus.GUARD_BLOCKED.value
    assert worker.status["last_skip_reason"] == "SNAPSHOT_NOT_READY"
    candidate = worker.status["last_candidate"]
    assert isinstance(candidate, dict)
    assert candidate["typed_skip_reason"] == "SNAPSHOT_NOT_READY"
    assert candidate["book_state"] == "SNAPSHOT_NOT_READY"
    assert "decision_age_seconds" in candidate
    assert worker.status["post_count"] == 0


def test_typed_remote_risk_failure_is_recorded_and_worker_continues(tmp_path) -> None:
    class Coordinator:
        calls = 0

        def submit(self, *_args):
            self.calls += 1
            return DemoRiskDecision(
                False,
                (DemoRiskReason.DEMO_BALANCE_UNAVAILABLE,),
                {
                    "remote_risk_start_at": "2026-08-24T00:00:00.000000+00:00",
                    "remote_risk_end_at": "2026-08-24T00:00:00.250000+00:00",
                    "remote_risk_latency_ms": "250.000",
                },
            )

        def reconcile_positions(self) -> int:
            return 0

    coordinator = Coordinator()
    reads = 0

    def reader(_settings):
        nonlocal reads
        reads += 1
        return (_intent(),) if reads == 1 else ()

    worker = _worker(tmp_path, coordinator, reader)
    assert worker.run_once() is None
    candidate = worker.status["last_candidate"]
    assert isinstance(candidate, dict)
    assert candidate["typed_skip_reason"] == "demo_balance_unavailable"
    assert candidate["remote_risk_latency_ms"] == "250.000"
    assert worker.run_once() is None
    assert coordinator.calls == 1


def test_stop_request_ends_worker_without_submitting(tmp_path) -> None:
    class Coordinator:
        def submit(self, *_args):
            pytest.fail("a stop request must prevent submit")

        def reconcile_positions(self) -> int:
            return 0

    worker = _worker(tmp_path, Coordinator(), lambda _settings: (_intent(),))
    worker.stop_requested = lambda: True
    assert worker.run_once() == "USER_STOP_REQUESTED"
    assert worker.status["status"] == DemoFirstFillStatus.STOPPED.value
    assert worker.status["post_count"] == 0


def test_single_post_reservation_stops_worker_and_prevents_second_attempt(tmp_path) -> None:
    worker: DemoFirstFillWorker

    class Coordinator:
        calls = 0

        def submit(self, intent: DemoIntent, _context):
            self.calls += 1
            worker.reserve_post(stable_client_order_id(intent))
            return DemoReconciliationResult(
                stable_client_order_id(intent), DemoLifecycleState.FILLED, "remote", 1
            )

        def reconcile_positions(self) -> int:
            return 0

    coordinator = Coordinator()
    worker = _worker(tmp_path, coordinator, lambda _settings: (_intent(),))
    assert worker.run_once() == "DEMO_ENTRY_EXECUTION_PATH_CERTIFIED"
    assert worker.status["post_count"] == 1
    assert worker.status["fill_count"] == 1
    assert worker.status["status"] == DemoFirstFillStatus.FILLED.value
    with pytest.raises(DemoFirstFillError, match="already reserved"):
        worker.reserve_post("second")
    assert coordinator.calls == 1


def test_second_instance_is_blocked_and_stale_lease_is_recoverable(tmp_path) -> None:
    path = tmp_path / "first-fill.lock"
    first = DemoFirstFillLease(path)
    first.acquire(datetime.now(UTC))
    second = DemoFirstFillLease(path)
    with pytest.raises(DemoFirstFillAlreadyRunning):
        second.acquire(datetime.now(UTC))
    first.release()
    path.write_text(json.dumps({"pid": -1, "started_at": "2026-08-24T00:00:00+00:00"}))
    recovered = DemoFirstFillLease(path)
    recovered.acquire(datetime.now(UTC))
    recovered.release()


def test_worker_log_never_receives_secret_values(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LIVE15_KALSHI_DEMO_API_KEY_ID", "unit-secret-key-id")
    logger = logging.getLogger("live15_quant.demo_first_fill")
    prior_handlers = tuple(logger.handlers)
    path = tmp_path / "worker.log"
    handler = _configure_worker_log(path)
    try:
        logger.info("Demo first-fill candidate skipped", extra={"event": "safe"})
        handler.flush()
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert tuple(logger.handlers) == prior_handlers
    content = path.read_text(encoding="utf-8")
    assert "private-key" not in content
    assert "signature" not in content
    configured_key_id = os.environ.get("LIVE15_KALSHI_DEMO_API_KEY_ID")
    if configured_key_id:
        assert configured_key_id not in content


def test_status_store_retries_transient_replace_and_cleans_temp(tmp_path, monkeypatch) -> None:
    store = DemoFirstFillStatusStore(tmp_path / "status.json", retry_sleep=lambda _delay: None)
    value = _initial_status(datetime(2026, 8, 24, tzinfo=UTC))
    original = demo_first_fill_module.os.replace
    attempts = 0

    def flaky(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("sharing violation")
        return original(source, destination)

    monkeypatch.setattr(demo_first_fill_module.os, "replace", flaky)
    store.write(value)
    assert attempts == 3
    assert not list(tmp_path.glob(".*.tmp"))


def test_status_write_exhaustion_is_logged_and_worker_continues(
    tmp_path, monkeypatch, caplog
) -> None:
    store = DemoFirstFillStatusStore(
        tmp_path / "status.json", replace_retries=2, retry_sleep=lambda _delay: None
    )

    def always_locked(*_args):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(demo_first_fill_module.os, "replace", always_locked)
    worker = _worker(tmp_path, object(), lambda _settings: ())
    worker.status_store = store
    with caplog.at_level("ERROR"):
        worker._write_status()
    assert "STATUS_WRITE_FAILED" in caplog.text
    assert not list(tmp_path.glob(".*.tmp"))


def test_pre_submit_latency_diagnostics_are_monotonic() -> None:
    decision = datetime(2026, 8, 24, 0, 0, 0, tzinfo=UTC)
    candidate = datetime(2026, 8, 24, 0, 0, 0, 100000, tzinfo=UTC)
    diagnostics = _latency_diagnostics(
        decision,
        candidate,
        {
            "live_book_read_at": "2026-08-24T00:00:00.125000+00:00",
            "pre_submit_ready_at": "2026-08-24T00:00:00.150000+00:00",
        },
    )
    assert diagnostics == {
        "decision_to_candidate_ms": "100.0",
        "candidate_to_book_ms": "25.000",
        "total_pre_submit_latency_ms": "150.00",
    }
