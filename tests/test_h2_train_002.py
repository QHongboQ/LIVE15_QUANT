from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from live15_quant.h2_train_002 import (
    DEPTHFEED_RATE_LIMIT_BLOCKED,
    H0OverlapTarget,
    RetryAttempt,
    run_bounded_depthfeed_request,
    select_recent_h0_overlap_target,
)
from live15_quant.historical_providers import DepthFeedHttpError

NOW = datetime(2026, 8, 28, 6, tzinfo=UTC)


def _target() -> H0OverlapTarget:
    return H0OverlapTarget(
        ticker="KXBTC15M-26AUG271945-45",
        event_id="KXBTC15M-26AUG271945",
        asset="BTC",
        series="KXBTC15M",
        window_start=NOW - timedelta(minutes=30),
        window_end=NOW - timedelta(minutes=15),
        h0_snapshot_count=8,
        h0_evidence_start=NOW - timedelta(minutes=29),
        h0_evidence_end=NOW - timedelta(minutes=16),
    )


def test_h0_target_is_an_explicit_identity_not_a_provider_discovery_result() -> None:
    target = _target()
    assert target.ticker == "KXBTC15M-26AUG271945-45"
    assert target.window_start < target.window_end
    assert target.h0_snapshot_count == 8


def test_rate_limit_retries_only_provider_advised_bounded_waits() -> None:
    calls = 0
    waits: list[float] = []

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise DepthFeedHttpError(status_code=429, endpoint_family="snapshots", retry_after="2")
        return "accepted"

    outcome = run_bounded_depthfeed_request(operation, sleep=waits.append)

    assert outcome.value == "accepted"
    assert outcome.classification == "ACCEPTED"
    assert outcome.attempts == (
        RetryAttempt(1, 429, "snapshots", "2", 2.0),
        RetryAttempt(2, 429, "snapshots", "2", 2.0),
        RetryAttempt(3, None, "snapshots", None, None),
    )
    assert waits == [2.0, 2.0]


def test_persistent_or_unbounded_rate_limit_stops_without_a_tight_loop() -> None:
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise DepthFeedHttpError(status_code=429, endpoint_family="snapshots", retry_after="999")

    outcome = run_bounded_depthfeed_request(
        operation, sleep=lambda _: pytest.fail("must not sleep")
    )

    assert outcome.value is None
    assert outcome.classification == DEPTHFEED_RATE_LIMIT_BLOCKED
    assert calls == 1
    assert outcome.attempts == (RetryAttempt(1, 429, "snapshots", "999", None),)


def _selection_database(
    path: Path,
    *,
    recovered_gap_end: datetime | None,
    recovery_detected_at: datetime | None = None,
) -> None:
    target = _target()
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE kalshi_market_lifecycle(
                id INTEGER PRIMARY KEY, ticker TEXT, event_ticker TEXT, asset TEXT, series TEXT,
                window_start TEXT, window_end TEXT, lifecycle TEXT, official_status TEXT,
                fetched_timestamp TEXT
            );
            CREATE TABLE kalshi_ws_book_checkpoints(
                id INTEGER PRIMARY KEY, ticker TEXT, received_timestamp TEXT
            );
            CREATE TABLE data_gaps(
                id INTEGER PRIMARY KEY, source TEXT, asset TEXT, instrument TEXT,
                gap_start TEXT, gap_end TEXT, detected_at TEXT, recovered INTEGER
            );
            """
        )
        connection.execute(
            """INSERT INTO kalshi_market_lifecycle VALUES(1,?,?,?,?,?,?,?,?,?)""",
            (
                target.ticker,
                target.event_id,
                target.asset,
                target.series,
                target.window_start.isoformat(),
                target.window_end.isoformat(),
                "settled_yes",
                "finalized",
                target.window_end.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO kalshi_ws_book_checkpoints VALUES(1,?,?)",
            (target.ticker, (target.window_start + timedelta(minutes=1)).isoformat()),
        )
        gap_start = target.window_start - timedelta(minutes=20)
        connection.execute(
            "INSERT INTO data_gaps VALUES(1,'kalshi_ws','BTC','KXBTC15M',?,?,?,0)",
            (gap_start.isoformat(), None, gap_start.isoformat()),
        )
        if recovered_gap_end is not None:
            connection.execute(
                "INSERT INTO data_gaps VALUES(2,'kalshi_ws','BTC','KXBTC15M',?,?,?,1)",
                (
                    gap_start.isoformat(),
                    recovered_gap_end.isoformat(),
                    (recovery_detected_at or recovered_gap_end).isoformat(),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def test_completed_effective_ws_gap_does_not_permanently_poison_later_h0_target(tmp_path) -> None:
    path = tmp_path / "selection.sqlite3"
    target = _target()
    _selection_database(path, recovered_gap_end=target.window_start - timedelta(minutes=1))

    selected = select_recent_h0_overlap_target(path, now=NOW)

    assert selected is not None
    assert selected.ticker == target.ticker


def test_effective_ws_gap_intersecting_target_remains_fail_closed(tmp_path) -> None:
    path = tmp_path / "selection.sqlite3"
    target = _target()
    _selection_database(path, recovered_gap_end=target.window_start + timedelta(minutes=1))

    assert select_recent_h0_overlap_target(path, now=NOW) is None


def test_h0_target_does_not_use_recovery_evidence_after_its_cutoff(tmp_path) -> None:
    path = tmp_path / "selection.sqlite3"
    target = _target()
    _selection_database(
        path,
        recovered_gap_end=target.window_start - timedelta(minutes=1),
        recovery_detected_at=NOW + timedelta(seconds=1),
    )

    assert select_recent_h0_overlap_target(path, now=NOW) is None
