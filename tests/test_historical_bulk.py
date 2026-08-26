from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from live15_quant.historical_bulk import (
    SERIES_BY_ASSET,
    HistoricalBulkStore,
    HistoricalWindow,
    dataset_identity,
    estimate_plan,
    resolve_window,
)

NOW = datetime(2026, 6, 26, tzinfo=UTC)


def test_resolve_window_is_exactly_90_calendar_days() -> None:
    window = resolve_window(NOW, days=90)
    assert window.start == datetime(2026, 3, 28, tzinfo=UTC)
    assert window.end == NOW
    assert window.days == 90


def test_preflight_headroom_is_explicit_and_bounded() -> None:
    window = HistoricalWindow(NOW - timedelta(days=90), NOW, 90)
    plan = estimate_plan(
        market_count=10_000,
        window=window,
        detail_market_cap=500,
        free_bytes=100 * 1024**3,
        depthfeed_status="DEPTHFEED_NOT_CONFIGURED",
    )
    assert plan.market_count == 10_000
    assert plan.trade_api_calls == 1_000
    assert plan.candle_api_calls == 1_000
    assert plan.storage_headroom_ok


def test_store_is_idempotent_and_records_conflicting_rows(tmp_path: Path) -> None:
    store = HistoricalBulkStore(tmp_path / "hist003.sqlite3")
    store.connection.execute(
        "INSERT INTO markets VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "kalshi_official",
            "T",
            "E",
            "BTC",
            "KXBTC15M",
            "2026-06-26T00:00:00+00:00",
            "2026-06-26T00:15:00+00:00",
            "settled",
            "yes",
            '{"v":1}',
        ),
    )
    store.connection.commit()
    assert (
        store._insert(
            "markets",
            ("kalshi_official", "T"),
            tuple(
                (
                    "kalshi_official",
                    "T",
                    "E",
                    "BTC",
                    "KXBTC15M",
                    "s",
                    "e",
                    "settled",
                    "yes",
                    '{"v":1}',
                )
            ),
        )
        == "duplicate"
    )
    assert (
        store._insert(
            "markets",
            ("kalshi_official", "T"),
            tuple(
                (
                    "kalshi_official",
                    "T",
                    "E",
                    "BTC",
                    "KXBTC15M",
                    "s",
                    "e",
                    "settled",
                    "yes",
                    '{"v":2}',
                )
            ),
        )
        == "conflict"
    )
    assert store.counts()["conflicts"] == 1
    store.close()


def test_dataset_identity_is_stable() -> None:
    window = HistoricalWindow(NOW - timedelta(days=90), NOW, 90)
    kwargs = {
        "code_sha": "abc",
        "window": window,
        "counts": {"markets": 2, "trades": 1},
        "manifests": ({"provider": "kalshi_official", "rows": 3},),
    }
    assert dataset_identity(**kwargs) == dataset_identity(**kwargs)
    assert set(SERIES_BY_ASSET) == {
        "BTC",
        "ETH",
        "SOL",
        "XRP",
        "DOGE",
        "BNB",
        "HYPE",
        "Gold",
        "Silver",
        "WTI",
    }
