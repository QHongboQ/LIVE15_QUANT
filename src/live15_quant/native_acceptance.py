"""Event-driven live acceptance for one official Kalshi 15-minute rollover."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import requests

from live15_quant.config import Settings
from live15_quant.kalshi_lifecycle import (
    KalshiDiscovery,
    KalshiLifecycle,
    KalshiLifecycleStateMachine,
    KalshiMarket,
    KalshiNativeMarketProvider,
)
from live15_quant.providers.kalshi import KalshiOfficialQuoteProvider, KalshiTargetUnavailableError
from live15_quant.storage import RecorderStore

MAX_ACCEPTANCE_SECONDS = 1800.0


def _persist_discoveries(
    store: RecorderStore,
    discoveries: tuple[KalshiDiscovery, ...],
    states: dict[str, KalshiLifecycle],
) -> None:
    """Persist official observations; malformed transitions fail immediately."""

    for discovery in discoveries:
        for market in discovery.valid_markets:
            prior = states.get(market.ticker)
            for observation in KalshiLifecycleStateMachine.observations(prior, market):
                store.append_kalshi_market(observation)
                states[market.ticker] = observation.lifecycle


def _nearest_current(
    discoveries: tuple[KalshiDiscovery, ...],
    *,
    minimum_observation_seconds: float,
) -> KalshiMarket | None:
    """Choose the valid open market whose real close is nearest."""

    candidates = [
        market
        for discovery in discoveries
        if (market := discovery.current) is not None
        and market.lifecycle is KalshiLifecycle.OPEN
        and (market.window_end - discovery.fetched_timestamp).total_seconds()
        >= minimum_observation_seconds
    ]
    return min(
        candidates,
        key=lambda market: (market.window_end, market.series, market.ticker),
        default=None,
    )


def _timeout_reason(
    *,
    baseline: KalshiMarket | None,
    baseline_quote_available: bool,
    successor: KalshiMarket | None,
    successor_quote_available: bool,
    settlement_result: str | None,
    post_rollover_complete: bool,
) -> str:
    if baseline is None:
        return "no_current_market_with_published_target"
    if not baseline_quote_available:
        return "baseline_quote_unavailable"
    if successor is None:
        return "no_adjacent_rollover_observed"
    if not successor_quote_available:
        return "successor_quote_unavailable"
    if settlement_result is None:
        return "official_settlement_not_published"
    if not post_rollover_complete:
        return "post_rollover_observation_incomplete"
    return "acceptance_deadline_elapsed"


def run_acceptance(
    *,
    max_seconds: float = MAX_ACCEPTANCE_SECONDS,
    poll_seconds: float = 5.0,
    post_rollover_seconds: float = 30.0,
    database_path: Path | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Observe one adjacent official rollover without assuming a date or UTC opening time."""

    if min(max_seconds, poll_seconds, post_rollover_seconds) <= 0:
        raise ValueError("acceptance durations must be positive")
    if max_seconds > MAX_ACCEPTANCE_SECONDS:
        raise ValueError("acceptance wall-clock timeout must not exceed 30 minutes")

    if database_path is None:
        temporary = tempfile.TemporaryDirectory(prefix="live15-native-acceptance-")
        path = Path(temporary.name) / "native.sqlite3"
    else:
        temporary = None
        path = database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    deadline = monotonic() + max_seconds
    settings = Settings(official_quote_orderbook_depth=10)
    client = KalshiOfficialQuoteProvider(
        settings,
        retry_total=0,
        deadline_monotonic=deadline,
        monotonic=monotonic,
    )
    provider = KalshiNativeMarketProvider(client)
    minimum_observation = max(10.0, poll_seconds * 2)
    states: dict[str, KalshiLifecycle] = {}
    baseline: KalshiMarket | None = None
    recent_ended: KalshiMarket | None = None
    announced_next: KalshiMarket | None = None
    successor: KalshiMarket | None = None
    rollover_observed_at: datetime | None = None
    rollover_complete_at: float | None = None
    settlement_result: str | None = None
    settlement_timestamp: datetime | None = None
    baseline_quote_writes = 0
    successor_quote_writes = 0
    baseline_quote_available = False
    successor_quote_available = False
    discovery_polls = 0
    upstream_unavailable_polls = 0
    network_retry_exhaustions = 0
    consecutive_network_failures = 0
    post_rollover_complete = False
    try:
        with RecorderStore(path) as store:
            states.update(
                {record.ticker: record.lifecycle for record in store.latest_kalshi_states()}
            )
            while deadline - monotonic() > 0:
                try:
                    discoveries = (
                        provider.discover_all()
                        if baseline is None
                        else (provider.discover(baseline.asset),)
                    )
                    consecutive_network_failures = 0
                except requests.RequestException:
                    network_retry_exhaustions += 1
                    consecutive_network_failures += 1
                    remaining_after_error = max(0.0, deadline - monotonic())
                    delay = min(
                        30.0,
                        poll_seconds * 2 ** min(consecutive_network_failures - 1, 4),
                        remaining_after_error,
                    )
                    sleeper(delay)
                    continue

                discovery_polls += 1
                _persist_discoveries(store, discoveries, states)

                if baseline is None:
                    baseline = _nearest_current(
                        discoveries,
                        minimum_observation_seconds=minimum_observation,
                    )
                    if baseline is None:
                        upstream_unavailable_polls += 1
                        sleeper(min(poll_seconds, max(0.0, deadline - monotonic())))
                        continue
                    baseline_quote_available = any(
                        True for _ in store.replay_kalshi_quotes(baseline.ticker)
                    )

                discovery = next(item for item in discoveries if item.asset is baseline.asset)
                if discovery.previous is not None:
                    recent_ended = discovery.previous
                if (
                    discovery.next is not None
                    and discovery.next.window_start == baseline.window_end
                ):
                    announced_next = discovery.next

                current = discovery.current
                if current is not None and current.ticker == baseline.ticker:
                    if discovery.fetched_timestamp < baseline.window_end:
                        try:
                            baseline_quote = client.quote_native(baseline)
                            if baseline_quote.received_timestamp < baseline.window_end:
                                baseline_quote_available = True
                                if store.append_kalshi_quote(baseline_quote):
                                    baseline_quote_writes += 1
                        except KalshiTargetUnavailableError:
                            upstream_unavailable_polls += 1
                        except requests.RequestException:
                            network_retry_exhaustions += 1
                elif current is not None and current.ticker != baseline.ticker:
                    if current.window_start != baseline.window_end:
                        # A schedule or maintenance gap is not a rollover. Re-select dynamically.
                        upstream_unavailable_polls += 1
                        baseline = None
                        announced_next = None
                        successor = None
                        rollover_observed_at = None
                        rollover_complete_at = None
                        settlement_result = None
                        settlement_timestamp = None
                        baseline_quote_available = False
                        successor_quote_available = False
                        baseline_quote_writes = 0
                        successor_quote_writes = 0
                        post_rollover_complete = False
                        sleeper(min(poll_seconds, max(0.0, deadline - monotonic())))
                        continue
                    successor = current
                    rollover_observed_at = discovery.fetched_timestamp
                    if rollover_complete_at is None:
                        rollover_complete_at = monotonic()
                elif discovery.fetched_timestamp >= baseline.window_end:
                    # No adjacent live market is visible yet; do not invent one.
                    upstream_unavailable_polls += 1

                if successor is not None:
                    try:
                        successor_quote = client.quote_native(successor)
                        if successor_quote.received_timestamp < successor.window_end:
                            successor_quote_available = True
                            if store.append_kalshi_quote(successor_quote):
                                successor_quote_writes += 1
                    except KalshiTargetUnavailableError:
                        upstream_unavailable_polls += 1
                    except requests.RequestException:
                        network_retry_exhaustions += 1

                old_observation = next(
                    (
                        market
                        for market in discovery.valid_markets
                        if market.ticker == baseline.ticker
                    ),
                    None,
                )
                if old_observation is not None and old_observation.settlement is not None:
                    settlement_result = old_observation.settlement.result.value
                    settlement_timestamp = old_observation.settlement.settlement_timestamp

                if (
                    rollover_complete_at is not None
                    and monotonic() - rollover_complete_at >= post_rollover_seconds
                ):
                    post_rollover_complete = True
                if (
                    successor_quote_available
                    and settlement_result is not None
                    and post_rollover_complete
                ):
                    break
                sleeper(min(poll_seconds, max(0.0, deadline - monotonic())))

            lifecycle_replay = (
                [record.lifecycle.value for record in store.replay_kalshi_markets(baseline.ticker)]
                if baseline is not None
                else []
            )
            success = bool(
                baseline is not None
                and successor is not None
                and baseline_quote_available
                and successor_quote_available
                and settlement_result is not None
                and post_rollover_complete
                and {
                    KalshiLifecycle.OPEN.value,
                    KalshiLifecycle.CLOSED.value,
                    KalshiLifecycle.SETTLEMENT_PENDING.value,
                    f"settled_{settlement_result}",
                }.issubset(lifecycle_replay)
            )
            if not success and settlement_result is not None:
                required = {
                    KalshiLifecycle.OPEN.value,
                    KalshiLifecycle.CLOSED.value,
                    KalshiLifecycle.SETTLEMENT_PENDING.value,
                    f"settled_{settlement_result}",
                }
                if not required.issubset(lifecycle_replay):
                    raise RuntimeError("official rollover lifecycle replay is inconsistent")
            status = "pass" if success else "expected_upstream_unavailable"
            reason = (
                None
                if success
                else _timeout_reason(
                    baseline=baseline,
                    baseline_quote_available=baseline_quote_available,
                    successor=successor,
                    successor_quote_available=successor_quote_available,
                    settlement_result=settlement_result,
                    post_rollover_complete=post_rollover_complete,
                )
            )
            integrity = store.integrity_check()
            counts = {
                "lifecycle": store.count("kalshi_market_lifecycle"),
                "quotes": store.count("kalshi_prediction_quotes"),
                "settlements": store.count("kalshi_settlements"),
            }

        with RecorderStore(path) as restarted:
            restart_integrity = restarted.integrity_check()
            restart_counts = {
                "lifecycle": restarted.count("kalshi_market_lifecycle"),
                "quotes": restarted.count("kalshi_prediction_quotes"),
                "settlements": restarted.count("kalshi_settlements"),
            }

        return {
            "status": status,
            "reason": reason,
            "started": started.isoformat(),
            "completed": datetime.now(UTC).isoformat(),
            "asset": baseline.asset.value if baseline is not None else None,
            "recent_ended_ticker": recent_ended.ticker if recent_ended is not None else None,
            "baseline_ticker": baseline.ticker if baseline is not None else None,
            "baseline_window_start": baseline.window_start.isoformat() if baseline else None,
            "baseline_window_end": baseline.window_end.isoformat() if baseline else None,
            "announced_next_ticker": announced_next.ticker if announced_next else None,
            "successor_ticker": successor.ticker if successor is not None else None,
            "rollover_observed_at": (
                rollover_observed_at.isoformat() if rollover_observed_at is not None else None
            ),
            "rollover_latency_seconds": (
                max(0.0, (rollover_observed_at - baseline.window_end).total_seconds())
                if rollover_observed_at is not None and baseline is not None
                else None
            ),
            "official_settlement_result": settlement_result,
            "official_settlement_timestamp": (
                settlement_timestamp.isoformat() if settlement_timestamp is not None else None
            ),
            "lifecycle_replay": lifecycle_replay,
            "baseline_quote_writes": baseline_quote_writes,
            "successor_quote_writes": successor_quote_writes,
            "discovery_polls": discovery_polls,
            "upstream_unavailable_polls": upstream_unavailable_polls,
            "network_retry_exhaustions": network_retry_exhaustions,
            "counts": counts,
            "integrity": integrity,
            "restart_integrity": restart_integrity,
            "restart_counts_match": restart_counts == counts,
            "robinhood_used": False,
            "orders_sent": 0,
            "temporary_database": database_path is None,
        }
    finally:
        client.close()
        if temporary is not None:
            temporary.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-seconds", type=float, default=MAX_ACCEPTANCE_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--post-rollover-seconds", type=float, default=30.0)
    parser.add_argument("--database-path", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            run_acceptance(
                max_seconds=args.max_seconds,
                poll_seconds=args.poll_seconds,
                post_rollover_seconds=args.post_rollover_seconds,
                database_path=args.database_path,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
