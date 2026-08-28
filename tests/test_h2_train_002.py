from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from live15_quant.h2_train_002 import (
    DEPTHFEED_RATE_LIMIT_BLOCKED,
    H0OverlapTarget,
    RetryAttempt,
    run_bounded_depthfeed_request,
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
