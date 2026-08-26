"""Bounded, resumable HIST-003 official-history acquisition substrate.

The default detail cap is deliberately explicit: market metadata is acquired for the complete
90-day requested window, while per-market trades/candles can be resumed in bounded batches. Raw
rows and checkpoints live in ignored data roots; only small summaries belong in Git.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic

from live15_quant.historical_providers import (
    HistoricalMarketRecord,
    KalshiOfficialHistoricalProvider,
)

HIST003_SCHEMA_VERSION = "1.0.0"
HIST003_DEFAULT_DETAIL_MARKET_CAP = 500
SERIES_BY_ASSET = {
    "BTC": "KXBTC15M",
    "ETH": "KXETH15M",
    "SOL": "KXSOL15M",
    "XRP": "KXXRP15M",
    "DOGE": "KXDOGE15M",
    "BNB": "KXBNB15M",
    "HYPE": "KXHYPE15M",
    "Gold": "KXGOLD15M",
    "Silver": "KXSILVER15M",
    "WTI": "KXWTI15M",
}


class HistoricalBulkError(RuntimeError):
    """Acquisition planning or storage failed closed."""


@dataclass(frozen=True, slots=True)
class HistoricalWindow:
    start: datetime
    end: datetime
    days: int

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start >= self.end:
            raise HistoricalBulkError("historical window must be increasing and timezone-aware")
        if self.days != (self.end - self.start).days:
            raise HistoricalBulkError("historical window days do not match bounds")


@dataclass(frozen=True, slots=True)
class PreflightEstimate:
    window: HistoricalWindow
    market_count: int
    metadata_api_calls: int
    trade_api_calls: int
    candle_api_calls: int
    detail_market_cap: int
    estimated_raw_bytes: int
    free_bytes: int
    safe_headroom_bytes: int
    depthfeed_status: str

    @property
    def storage_headroom_ok(self) -> bool:
        return self.free_bytes - self.estimated_raw_bytes >= self.safe_headroom_bytes

    def to_dict(self) -> dict[str, object]:
        return {
            "start": self.window.start.isoformat(),
            "end": self.window.end.isoformat(),
            "days": self.window.days,
            "market_count": self.market_count,
            "metadata_api_calls": self.metadata_api_calls,
            "trade_api_calls": self.trade_api_calls,
            "candle_api_calls": self.candle_api_calls,
            "detail_market_cap": self.detail_market_cap,
            "estimated_raw_bytes": self.estimated_raw_bytes,
            "free_bytes": self.free_bytes,
            "safe_headroom_bytes": self.safe_headroom_bytes,
            "storage_headroom_ok": self.storage_headroom_ok,
            "depthfeed_status": self.depthfeed_status,
        }


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _row(record: object) -> Mapping[str, object]:
    raw = getattr(record, "raw", None)
    return raw if isinstance(raw, Mapping) else {}


class HistoricalBulkStore:
    """SQLite-backed normalized store with conflict-safe idempotent inserts."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS acquisition_metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS markets (
                provider TEXT NOT NULL, ticker TEXT NOT NULL, event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL, series TEXT NOT NULL, open_time TEXT NOT NULL,
                close_time TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
                raw_json TEXT NOT NULL, PRIMARY KEY (provider, ticker)
            );
            CREATE TABLE IF NOT EXISTS trades (
                provider TEXT NOT NULL, trade_id TEXT NOT NULL, ticker TEXT NOT NULL,
                created_time TEXT NOT NULL, raw_json TEXT NOT NULL,
                PRIMARY KEY (provider, trade_id)
            );
            CREATE TABLE IF NOT EXISTS candles (
                provider TEXT NOT NULL, ticker TEXT NOT NULL, interval_seconds INTEGER NOT NULL,
                end_period_ts INTEGER NOT NULL, raw_json TEXT NOT NULL,
                PRIMARY KEY (provider, ticker, interval_seconds, end_period_ts)
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                provider TEXT NOT NULL, asset TEXT NOT NULL, stage TEXT NOT NULL,
                cursor TEXT, completed INTEGER NOT NULL, failed INTEGER NOT NULL,
                retries INTEGER NOT NULL, last_error TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY (provider, asset, stage)
            );
            CREATE TABLE IF NOT EXISTS conflicts (
                provider TEXT NOT NULL, identity TEXT NOT NULL, kind TEXT NOT NULL,
                existing_json TEXT NOT NULL, incoming_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def metadata(self, key: str, value: object) -> None:
        self.connection.execute(
            "INSERT INTO acquisition_metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _json(value)),
        )

    def checkpoint(
        self,
        provider: str,
        asset: str,
        stage: str,
        *,
        cursor: str | None,
        completed: bool,
        failed: bool = False,
        retries: int = 0,
        last_error: str | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO checkpoints VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(provider,asset,stage) DO UPDATE SET cursor=excluded.cursor, "
            "completed=excluded.completed, failed=excluded.failed, retries=excluded.retries, "
            "last_error=excluded.last_error, updated_at=excluded.updated_at",
            (
                provider,
                asset,
                stage,
                cursor,
                int(completed),
                int(failed),
                retries,
                last_error,
                datetime.now(UTC).isoformat(),
            ),
        )
        self.connection.commit()

    def checkpoint_state(
        self, provider: str, asset: str, stage: str
    ) -> tuple[str | None, bool] | None:
        row = self.connection.execute(
            "SELECT cursor,completed FROM checkpoints WHERE provider=? AND asset=? AND stage=?",
            (provider, asset, stage),
        ).fetchone()
        return (row[0], bool(row[1])) if row else None

    def _insert(self, table: str, identity: tuple[object, ...], values: tuple[object, ...]) -> str:
        columns = {
            "markets": (
                "provider,ticker,event_ticker,asset,series,open_time,close_time,status,result,"
                "raw_json"
            ),
            "trades": "provider,trade_id,ticker,created_time,raw_json",
            "candles": "provider,ticker,interval_seconds,end_period_ts,raw_json",
        }[table]
        placeholders = ",".join("?" for _ in values)
        key_columns = {
            "markets": "provider,ticker",
            "trades": "provider,trade_id",
            "candles": "provider,ticker,interval_seconds,end_period_ts",
        }[table]
        existing = self.connection.execute(
            f"SELECT raw_json FROM {table} WHERE ({key_columns})=("
            + ",".join("?" * len(identity))
            + ")",
            identity,
        ).fetchone()
        incoming = str(values[-1])
        if existing is not None:
            if existing[0] != incoming:
                self.connection.execute(
                    "INSERT INTO conflicts VALUES(?,?,?,?,?,?)",
                    (
                        str(identity[0]),
                        _json(identity),
                        table,
                        str(existing[0]),
                        incoming,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                self.connection.commit()
                return "conflict"
            return "duplicate"
        self.connection.execute(f"INSERT INTO {table}({columns}) VALUES({placeholders})", values)
        return "inserted"

    def insert_market(self, asset: str, series: str, record: HistoricalMarketRecord) -> str:
        raw = _json(record.raw)
        return self._insert(
            "markets",
            ("kalshi_official", record.ticker),
            (
                "kalshi_official",
                record.ticker,
                record.event_ticker,
                asset,
                record.series_ticker or series,
                record.open_time.isoformat(),
                record.close_time.isoformat(),
                record.status,
                record.result,
                raw,
            ),
        )

    def insert_trade(self, record: object) -> str:
        raw = _json(_row(record))
        return self._insert(
            "trades",
            ("kalshi_official", record.trade_id),
            (
                "kalshi_official",
                record.trade_id,
                record.ticker,
                record.created_time.isoformat(),
                raw,
            ),
        )

    def insert_candle(self, ticker: str, record: object, interval_seconds: int = 60) -> str:
        raw = _json(_row(record))
        end_ts = int(record.end_period_timestamp.timestamp())
        return self._insert(
            "candles",
            ("kalshi_official", ticker, interval_seconds, end_ts),
            ("kalshi_official", ticker, interval_seconds, end_ts, raw),
        )

    def counts(self) -> dict[str, int]:
        return {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("markets", "trades", "candles", "conflicts")
        }


def resolve_window(cutoff: datetime, days: int = 90) -> HistoricalWindow:
    end = cutoff.astimezone(UTC)
    return HistoricalWindow(end - timedelta(days=days), end, days)


def estimate_plan(
    *,
    market_count: int,
    window: HistoricalWindow,
    detail_market_cap: int,
    free_bytes: int,
    depthfeed_status: str,
) -> PreflightEstimate:
    if detail_market_cap <= 0 or detail_market_cap > market_count:
        detail_market_cap = max(1, min(market_count, detail_market_cap))
    metadata_calls = max(1, (market_count + 999) // 1000)
    detail_calls = detail_market_cap * 2
    # Conservative research-store estimate; raw provider JSON can be larger, hence 25% headroom.
    estimated = market_count * 6_000 + detail_market_cap * 35_000
    return PreflightEstimate(
        window,
        market_count,
        metadata_calls,
        detail_calls,
        detail_calls,
        detail_market_cap,
        estimated,
        free_bytes,
        max(5 * 1024**3, int(estimated * 0.25)),
        depthfeed_status,
    )


def _within(record: HistoricalMarketRecord, window: HistoricalWindow) -> bool:
    return record.close_time > window.start and record.open_time < window.end


def acquire_official(
    *,
    provider: KalshiOfficialHistoricalProvider,
    store: HistoricalBulkStore,
    window: HistoricalWindow,
    detail_market_cap: int,
) -> dict[str, object]:
    started = monotonic()
    api_calls = {
        "historical_cutoff": 1,
        "historical_markets": 0,
        "historical_trades": 0,
        "historical_candlesticks": 0,
    }
    asset_counts: dict[str, dict[str, int]] = {
        asset: {"markets": 0, "trades": 0, "candles": 0} for asset in SERIES_BY_ASSET
    }
    selected: list[tuple[str, HistoricalMarketRecord]] = []
    selected_by_asset: dict[str, int] = {asset: 0 for asset in SERIES_BY_ASSET}
    per_asset_cap = max(1, detail_market_cap // len(SERIES_BY_ASSET))
    page_counts: dict[str, int] = {}
    for asset, series in SERIES_BY_ASSET.items():
        page_counts[asset] = 0
        try:
            state = store.checkpoint_state("kalshi_official", asset, "markets")
            if state is not None and state[1]:
                continue
            start_cursor = state[0] if state is not None else None
            for records, cursor, page_number in provider.market_pages(
                series_ticker=series, cursor=start_cursor
            ):
                api_calls["historical_markets"] += 1
                page_counts[asset] = page_number
                for record in records:
                    if _within(record, window):
                        if store.insert_market(asset, series, record) == "inserted":
                            asset_counts[asset]["markets"] += 1
                        if (
                            selected_by_asset[asset] < per_asset_cap
                            and len(selected) < detail_market_cap
                        ):
                            selected.append((asset, record))
                            selected_by_asset[asset] += 1
                store.checkpoint(
                    "kalshi_official", asset, "markets", cursor=cursor, completed=False
                )
                if records and min(item.close_time for item in records) <= window.start:
                    break
        except Exception as error:
            store.checkpoint(
                "kalshi_official",
                asset,
                "markets",
                cursor=None,
                completed=False,
                failed=True,
                last_error=str(error)[:240],
            )
            continue
        store.checkpoint("kalshi_official", asset, "markets", cursor=None, completed=True)

    for asset, market in selected:
        try:
            trades = provider.trades(
                ticker=market.ticker,
                min_ts=int(window.start.timestamp()),
                max_ts=int(window.end.timestamp()),
                max_pages=100,
                limit=1000,
            )
            api_calls["historical_trades"] += 1
            for trade in trades:
                if store.insert_trade(trade) == "inserted":
                    asset_counts[asset]["trades"] += 1
            candles = provider.candlesticks(
                market.ticker,
                # The official endpoint caps a request at 5,000 candles.  A
                # 15-minute market's own completed window is the narrowest
                # truthful request and avoids asking for unrelated history.
                start=max(window.start, market.open_time),
                end=min(window.end, market.close_time),
                period_interval=1,
            )
            api_calls["historical_candlesticks"] += 1
            for candle in candles:
                if store.insert_candle(market.ticker, candle) == "inserted":
                    asset_counts[asset]["candles"] += 1
        except Exception as error:
            store.checkpoint(
                "kalshi_official",
                asset,
                f"detail:{market.ticker}",
                cursor=None,
                completed=False,
                failed=True,
                last_error=str(error)[:240],
            )
            continue
        store.checkpoint(
            "kalshi_official", asset, f"detail:{market.ticker}", cursor=None, completed=True
        )
    store.connection.commit()
    return {
        "api_calls": api_calls,
        "page_counts": page_counts,
        "asset_counts": asset_counts,
        "detail_markets_acquired": len(selected),
        "detail_market_cap": detail_market_cap,
        "detail_cap_applied": True,
        "elapsed_seconds": round(monotonic() - started, 3),
        "partial_source_coverage": len(selected)
        < sum(item["markets"] for item in asset_counts.values()),
    }


def dataset_identity(
    *,
    code_sha: str,
    window: HistoricalWindow,
    counts: Mapping[str, int],
    manifests: Iterable[Mapping[str, object]],
) -> str:
    payload = {
        "schema": HIST003_SCHEMA_VERSION,
        "code_sha": code_sha,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "counts": dict(counts),
        "manifests": sorted((dict(item) for item in manifests), key=_json),
    }
    return "historical-research-" + hashlib.sha256(_json(payload).encode()).hexdigest()[:24]
