from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from live15_quant.historical_providers import (
    DEPTHFEED_FREE_PLAN_LOOKBACK_DAYS,
    DEPTHFEED_NOT_CONFIGURED,
    AcquisitionManifest,
    DepthFeedFreePlanRangeError,
    DepthFeedHistoricalOrderbookProvider,
    DepthFeedHistoricalRange,
    DepthFeedHttpError,
    HistoricalL2Snapshot,
    HistoricalProviderError,
    KalshiOfficialHistoricalProvider,
    SnapshotLevel,
    filter_candlesticks_asof,
    select_latest_asof,
    validate_depthfeed_free_plan_range,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def fresh_depthfeed_range() -> DepthFeedHistoricalRange:
    """Return a real-clock-safe public range for adapter plumbing tests."""

    end = datetime.now(UTC) - timedelta(seconds=1)
    return validate_depthfeed_free_plan_range(end - timedelta(minutes=15), end, now=end)


class FakeHistorical:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def cutoff(self):
        self.calls.append(("cutoff", {}))
        return type(
            "Cutoff",
            (),
            {
                "market_settled_ts": NOW,
                "trades_created_ts": NOW,
                "orders_updated_ts": NOW,
                "market_positions_last_updated_ts": None,
            },
        )()

    def markets(self, **kwargs):
        self.calls.append(("markets", kwargs))
        item = type(
            "Market",
            (),
            {
                "ticker": "KXBTC15M-TEST",
                "event_ticker": "KXBTC15M-TEST",
                "series_ticker": "KXBTC15M",
                "open_time": NOW,
                "close_time": NOW + timedelta(minutes=15),
                "status": "settled",
                "result": "yes",
            },
        )()
        return type("Page", (), {"items": [item], "cursor": None})()

    def trades(self, **kwargs):
        self.calls.append(("trades", kwargs))
        item = type(
            "Trade",
            (),
            {
                "trade_id": "trade-1",
                "ticker": "KXBTC15M-TEST",
                "count": Decimal("2"),
                "yes_price": Decimal("0.51"),
                "no_price": Decimal("0.49"),
                "taker_side": "yes",
                "created_time": NOW,
            },
        )()
        return type("Page", (), {"items": [item], "cursor": None})()

    def candlesticks(self, ticker, **kwargs):
        self.calls.append(("candlesticks", {"ticker": ticker, **kwargs}))
        item = type("Candle", (), {"end_period_ts": int(NOW.timestamp()), "volume": Decimal("3")})()
        return [item]


class FakeClient:
    def __init__(self) -> None:
        self.historical = FakeHistorical()

    def close(self) -> None:
        pass


def test_official_adapter_uses_sdk_historical_surface_and_keeps_provenance() -> None:
    adapter = KalshiOfficialHistoricalProvider(FakeClient())

    cutoff = adapter.cutoff()
    markets = adapter.markets(series_ticker="KXBTC15M", max_pages=1)
    trades = adapter.trades(ticker="KXBTC15M-TEST", max_pages=1)
    candles = adapter.candlesticks(
        "KXBTC15M-TEST", start=NOW - timedelta(minutes=1), end=NOW, period_interval=1
    )

    assert cutoff.provider.provider_id == "kalshi_official"
    assert markets[0].provider.tier == "H1_KALSHI_OFFICIAL_HISTORY"
    assert trades[0].provider.endpoint_family == "historical_trades"
    assert candles[0].provider.provider_id == "kalshi_official"


def test_depthfeed_missing_key_is_independent_and_explicit() -> None:
    adapter = DepthFeedHistoricalOrderbookProvider(api_key=None)

    assert adapter.status == DEPTHFEED_NOT_CONFIGURED
    with pytest.raises(HistoricalProviderError, match=DEPTHFEED_NOT_CONFIGURED):
        adapter.discover_markets(limit=1)


def test_depthfeed_snapshot_parsing_rejects_bad_ladders_and_separates_ticks() -> None:
    adapter = DepthFeedHistoricalOrderbookProvider(api_key="test-key")
    snapshot = adapter.parse_snapshot(
        {
            "ticker": "KXBTC15M-TEST",
            "series": "KXBTC15M",
            "base_asset": "BTC",
            "market_type": "15m",
            "received_timestamp": NOW.isoformat(),
            "yes": [["0.50", "2"]],
            "no": [["0.49", "3"]],
        }
    )
    tick = adapter.parse_tick(
        {
            "ticker": "KXBTC15M-TEST",
            "received_timestamp": NOW.isoformat(),
            "sequence": 4,
            "kind": "delta",
            "side": "yes",
            "price": "0.50",
            "delta": "1",
            "resting_size": "3",
        }
    )

    assert snapshot.quality_class == "HISTORICAL_L2_SNAPSHOT"
    assert snapshot.yes == (SnapshotLevel(Decimal("0.50"), Decimal("2")),)
    assert tick.quality_class == "HISTORICAL_L2_DELTA"
    assert tick.kind == "delta"
    with pytest.raises(HistoricalProviderError, match="duplicate price"):
        adapter.parse_snapshot(
            {
                "ticker": "KXBTC15M-TEST",
                "received_timestamp": NOW.isoformat(),
                "yes": [["0.50", "1"], ["0.50", "2"]],
                "no": [],
            }
        )


def test_depthfeed_real_snapshot_timestamp_shape_is_interpreted_as_utc() -> None:
    adapter = DepthFeedHistoricalOrderbookProvider(api_key="test-key")
    snapshot = adapter.parse_snapshot(
        {
            "ticker": "KXBTC15M-26AUG280100-00",
            "series": "KXBTC15M",
            "base_asset": "BTC",
            "market_type": "15m",
            "timestamp": "2026-08-28 04:45:59.390",
            "yes": [["0.50", "2"]],
            "no": [["0.49", "3"]],
        }
    )
    assert snapshot.received_timestamp == datetime(2026, 8, 28, 4, 45, 59, 390000, tzinfo=UTC)


def test_asof_selection_never_uses_future_observation() -> None:
    adapter = DepthFeedHistoricalOrderbookProvider(api_key="test-key")
    earlier = adapter.parse_snapshot(
        {
            "ticker": "KXBTC15M-TEST",
            "received_timestamp": (NOW - timedelta(seconds=1)).isoformat(),
            "yes": [],
            "no": [],
        }
    )
    later = adapter.parse_snapshot(
        {
            "ticker": "KXBTC15M-TEST",
            "received_timestamp": (NOW + timedelta(seconds=1)).isoformat(),
            "yes": [],
            "no": [],
        }
    )
    assert select_latest_asof((earlier, later), NOW) is earlier


def test_manifest_is_deterministic_and_candles_require_completed_period() -> None:
    rows = ({"ticker": "KXBTC15M-TEST", "received": NOW.isoformat()},)
    first = AcquisitionManifest.build(
        provider="depthfeed_kalshi_l2",
        endpoint_family="snapshots",
        query_bounds={"start": "2026-08-26T11:00:00Z", "end": "2026-08-26T12:00:00Z"},
        tickers=("KXBTC15M-TEST",),
        archive_floor=NOW - timedelta(days=1),
        page_count=1,
        rows=rows,
        code_sha="abc123",
    )
    second = AcquisitionManifest.build(
        provider="depthfeed_kalshi_l2",
        endpoint_family="snapshots",
        query_bounds={"start": "2026-08-26T11:00:00Z", "end": "2026-08-26T12:00:00Z"},
        tickers=("KXBTC15M-TEST",),
        archive_floor=NOW - timedelta(days=1),
        page_count=1,
        rows=rows,
        code_sha="abc123",
    )
    assert first.content_hash == second.content_hash
    assert not filter_candlesticks_asof((), NOW)


class FakeDepthResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeHttpErrorResponse(FakeDepthResponse):
    status_code = 429

    def __init__(self, payload):
        super().__init__(payload)
        self.headers = {"Retry-After": "2"}

    def raise_for_status(self):
        import requests

        raise requests.HTTPError("rate limited")


class FakeDepthSession:
    def __init__(self):
        self.calls = []

    def get(self, url, *, params, headers, timeout):
        self.calls.append((url, params, headers, timeout))
        cursor = params.get("cursor")
        if url.endswith("/snapshots"):
            payload = {
                "snapshots": [
                    {
                        "ticker": "KXBTC15M-TEST",
                        "received_timestamp": NOW.isoformat(),
                        "yes": [],
                        "no": [],
                    }
                ],
                "next_cursor": None if cursor else "next",
            }
        else:
            payload = {"ticks": [], "next_cursor": None}
        return FakeDepthResponse(payload)


def test_depthfeed_snapshot_pagination_is_bounded_and_key_is_not_logged() -> None:
    session = FakeDepthSession()
    adapter = DepthFeedHistoricalOrderbookProvider(
        api_key="opaque-secret", base_url="https://depthfeed.test", session=session
    )
    snapshots = adapter.snapshots(
        "KXBTC15M-TEST", historical_range=fresh_depthfeed_range(), max_pages=2, limit=1
    )

    assert len(snapshots) == 2
    assert len(session.calls) == 2
    assert all("opaque-secret" not in str(call[:2]) for call in session.calls)
    assert isinstance(snapshots[0], HistoricalL2Snapshot)


def test_depthfeed_adapter_appends_v3_to_documented_root() -> None:
    session = FakeDepthSession()
    adapter = DepthFeedHistoricalOrderbookProvider(
        api_key="opaque-secret", base_url="https://api.depthfeed.com", session=session
    )
    adapter.discover_markets(limit=1, historical_range=fresh_depthfeed_range())
    assert session.calls[0][0].startswith("https://api.depthfeed.com/v3/kalshi/markets")


def test_depthfeed_http_errors_are_sanitized_and_endpoint_scoped() -> None:
    class RateLimitedSession:
        def get(self, *args, **kwargs):
            return FakeHttpErrorResponse({})

    adapter = DepthFeedHistoricalOrderbookProvider(
        api_key="opaque-secret", base_url="https://depthfeed.test", session=RateLimitedSession()
    )
    with pytest.raises(DepthFeedHttpError, match="DEPTHFEED_HTTP_429:snapshots") as error:
        adapter.snapshots("KXBTC15M-TEST", historical_range=fresh_depthfeed_range())
    assert error.value.status_code == 429
    assert error.value.endpoint_family == "snapshots"
    assert error.value.retry_after == "2"


def test_depthfeed_free_plan_range_is_explicit_and_fail_closed() -> None:
    now = datetime(2026, 8, 28, tzinfo=UTC)
    allowed = validate_depthfeed_free_plan_range(
        now - timedelta(days=DEPTHFEED_FREE_PLAN_LOOKBACK_DAYS), now, now=now
    )
    assert allowed.as_query_params() == {
        "start_time": "2026-08-21T00:00:00+00:00",
        "end_time": "2026-08-28T00:00:00+00:00",
    }
    with pytest.raises(DepthFeedFreePlanRangeError, match="DEPTHFEED_FREE_PLAN_LOOKBACK_EXCEEDED"):
        validate_depthfeed_free_plan_range(
            now - timedelta(days=DEPTHFEED_FREE_PLAN_LOOKBACK_DAYS, microseconds=1), now, now=now
        )
    with pytest.raises(DepthFeedFreePlanRangeError, match="DEPTHFEED_REQUEST_END_IN_FUTURE"):
        validate_depthfeed_free_plan_range(
            now - timedelta(minutes=15), now + timedelta(seconds=1), now=now
        )


def test_depthfeed_provider_revalidates_public_range_before_network_access() -> None:
    session = FakeDepthSession()
    adapter = DepthFeedHistoricalOrderbookProvider(
        api_key="opaque-secret", base_url="https://depthfeed.test", session=session
    )
    bypass = DepthFeedHistoricalRange(
        NOW - timedelta(days=8), NOW - timedelta(days=7, minutes=59), NOW, NOW
    )
    with pytest.raises(DepthFeedFreePlanRangeError, match="DEPTHFEED_FREE_PLAN_LOOKBACK_EXCEEDED"):
        adapter.snapshots("KXBTC15M-TEST", historical_range=bypass)
    assert session.calls == []
