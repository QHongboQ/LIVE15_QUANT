"""Bounded H2-TRAIN-002 selection and request controls.

This module deliberately owns no Recorder writes, model training, or provider credentials.
It provides a small read-only seam used by the one-event acquisition command.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from live15_quant.h2_l2_materialization import H0SnapshotReference
from live15_quant.historical_providers import DepthFeedHttpError, SnapshotLevel

DEPTHFEED_RATE_LIMIT_BLOCKED = "DEPTHFEED_RATE_LIMIT_BLOCKED"
_MAX_RETRY_AFTER_SECONDS = 60.0
_MAX_RATE_LIMIT_RETRIES = 2


def _utc(value: str | datetime) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class H0OverlapTarget:
    ticker: str
    event_id: str
    asset: str
    series: str
    window_start: datetime
    window_end: datetime
    h0_snapshot_count: int
    h0_evidence_start: datetime
    h0_evidence_end: datetime


@dataclass(frozen=True, slots=True)
class RetryAttempt:
    attempt: int
    status_code: int | None
    endpoint_family: str
    retry_after: str | None
    waited_seconds: float | None
    requested_interval_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class BoundedRequestResult[T]:
    value: T | None
    classification: str
    attempts: tuple[RetryAttempt, ...]
    error: DepthFeedHttpError | None = None


def _bounded_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if 0 < seconds <= _MAX_RETRY_AFTER_SECONDS else None


def run_bounded_depthfeed_request[T](
    operation: Callable[[], T],
    *,
    sleep: Callable[[float], None],
    endpoint_family: str = "snapshots",
    requested_interval_seconds: float | None = None,
    max_retries: int = _MAX_RATE_LIMIT_RETRIES,
) -> BoundedRequestResult[T]:
    """Run one request with at most two provider-advised 429 retries, never a tight loop."""

    if not 0 <= max_retries <= _MAX_RATE_LIMIT_RETRIES:
        raise ValueError("max_retries exceeds H2-TRAIN-002 policy")
    attempts: list[RetryAttempt] = []
    for number in range(1, max_retries + 2):
        try:
            value = operation()
        except DepthFeedHttpError as error:
            if error.status_code != 429:
                attempts.append(
                    RetryAttempt(
                        number,
                        error.status_code,
                        error.endpoint_family,
                        error.retry_after,
                        None,
                        requested_interval_seconds,
                    )
                )
                return BoundedRequestResult(
                    None, f"DEPTHFEED_HTTP_{error.status_code}", tuple(attempts), error
                )
            wait_seconds = _bounded_retry_after(error.retry_after)
            attempts.append(
                RetryAttempt(
                    number,
                    429,
                    error.endpoint_family,
                    error.retry_after,
                    wait_seconds,
                    requested_interval_seconds,
                )
            )
            if wait_seconds is None or number > max_retries:
                return BoundedRequestResult(
                    None, DEPTHFEED_RATE_LIMIT_BLOCKED, tuple(attempts), error
                )
            sleep(wait_seconds)
        else:
            attempts.append(
                RetryAttempt(number, None, endpoint_family, None, None, requested_interval_seconds)
            )
            return BoundedRequestResult(value, "ACCEPTED", tuple(attempts))
    raise AssertionError("bounded retry loop must return")


def select_recent_h0_overlap_target(
    recorder_db: Path,
    *,
    now: datetime | None = None,
    lookback_hours: int = 24,
) -> H0OverlapTarget | None:
    """Select the newest completed, ungapped BTC-first LIVE15 window with native H0 evidence."""

    if not 1 <= lookback_hours <= 48:
        raise ValueError("lookback_hours must be between 1 and 48")
    observed_now = _utc(now or datetime.now(UTC))
    cutoff = observed_now - timedelta(hours=lookback_hours)
    db_uri = f"file:{recorder_db.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as connection:
        row = connection.execute(
            """
            WITH latest_lifecycle AS (
                SELECT l.*, ROW_NUMBER() OVER (
                    PARTITION BY l.ticker ORDER BY l.fetched_timestamp DESC, l.id DESC
                ) AS lifecycle_rank
                FROM kalshi_market_lifecycle AS l
            )
            SELECT l.ticker, l.event_ticker, l.asset, l.series, l.window_start, l.window_end,
                   COUNT(c.id) AS snapshot_count,
                   MIN(c.received_timestamp) AS evidence_start,
                   MAX(c.received_timestamp) AS evidence_end
            FROM latest_lifecycle AS l
            JOIN kalshi_ws_book_checkpoints AS c ON c.ticker = l.ticker
            WHERE l.lifecycle_rank = 1
              AND l.lifecycle IN ('settled_yes', 'settled_no')
              AND l.official_status = 'finalized'
              AND l.window_end >= ? AND l.window_end <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM data_gaps AS g
                  WHERE g.instrument IN (l.ticker, l.series)
                    AND g.gap_start < l.window_end
                    AND COALESCE(g.gap_end, ?) > l.window_start
              )
            GROUP BY l.ticker, l.event_ticker, l.asset, l.series, l.window_start, l.window_end
            ORDER BY CASE WHEN l.asset = 'BTC' AND l.series = 'KXBTC15M' THEN 0 ELSE 1 END,
                     l.window_end DESC, l.ticker ASC
            LIMIT 1
            """,
            (cutoff.isoformat(), observed_now.isoformat(), observed_now.isoformat()),
        ).fetchone()
    if row is None:
        return None
    return H0OverlapTarget(
        ticker=str(row[0]),
        event_id=str(row[1]),
        asset=str(row[2]),
        series=str(row[3]),
        window_start=_utc(str(row[4])),
        window_end=_utc(str(row[5])),
        h0_snapshot_count=int(row[6]),
        h0_evidence_start=_utc(str(row[7])),
        h0_evidence_end=_utc(str(row[8])),
    )


def _h0_levels(serialized: str) -> tuple[SnapshotLevel, ...]:
    raw = json.loads(serialized)
    if not isinstance(raw, list):
        raise ValueError("H0 checkpoint ladder must be a list")
    levels: list[SnapshotLevel] = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("H0 checkpoint level must contain price and size")
        levels.append(SnapshotLevel(Decimal(str(item[0])), Decimal(str(item[1]))))
    return tuple(levels)


def load_h0_overlap_references(
    recorder_db: Path, target: H0OverlapTarget
) -> tuple[H0SnapshotReference, ...]:
    """Load only native H0 checkpoints for the selected completed event, read-only."""

    db_uri = f"file:{recorder_db.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as connection:
        rows = connection.execute(
            """
            SELECT received_timestamp, yes_bids, no_bids, content_hash
            FROM kalshi_ws_book_checkpoints
            WHERE ticker = ? AND received_timestamp >= ? AND received_timestamp <= ?
            ORDER BY received_timestamp ASC, id ASC
            """,
            (target.ticker, target.window_start.isoformat(), target.window_end.isoformat()),
        ).fetchall()
    return tuple(
        H0SnapshotReference(
            "live15_recorder_h0",
            "H0_LIVE_NATIVE",
            target.ticker,
            target.event_id,
            _utc(str(row[0])),
            _h0_levels(str(row[1])),
            _h0_levels(str(row[2])),
            str(row[3]),
        )
        for row in rows
    )
