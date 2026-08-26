"""Read-only historical provider adapters for the HIST-002R research boundary.

This module deliberately does not import Recorder, Materializer, Paper, Risk, or model code.
The official Kalshi adapter uses the installed ``kalshi-sdk`` historical surface.  DepthFeed is
an optional, independently failing adapter for third-party historical L2 evidence only.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import requests

from live15_quant.secrets import resolve_secret_path

KALSHI_OFFICIAL = "kalshi_official"
DEPTHFEED_KALSHI_L2 = "depthfeed_kalshi_l2"
LIVE15_RECORDER_H0 = "live15_recorder_h0"
DEPTHFEED_NOT_CONFIGURED = "DEPTHFEED_NOT_CONFIGURED"
DEPTHFEED_INTEGRATION_READY_KEY_REQUIRED = "DEPTHFEED_INTEGRATION_READY_KEY_REQUIRED"
HISTORICAL_L2_SNAPSHOT = "HISTORICAL_L2_SNAPSHOT"
HISTORICAL_L2_DELTA = "HISTORICAL_L2_DELTA"
_MAX_PROBE_PAGES = 100


class HistoricalProviderError(RuntimeError):
    """A provider returned invalid data or is unavailable for this read-only task."""


class HistoricalProviderNotConfigured(HistoricalProviderError):
    """The optional provider has no configured credential."""


def _utc(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise HistoricalProviderError(f"malformed {field}") from error
    else:
        raise HistoricalProviderError(f"malformed {field}")
    if result.tzinfo is None or result.utcoffset() is None:
        raise HistoricalProviderError(f"{field} must be timezone-aware")
    return result.astimezone(UTC)


def _decimal(value: object, field: str, *, allow_none: bool = False) -> Decimal | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise HistoricalProviderError(f"malformed {field}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise HistoricalProviderError(f"malformed {field}") from error
    if not result.is_finite():
        raise HistoricalProviderError(f"malformed {field}")
    return result


def _dump(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        result = model_dump(mode="python")
        if isinstance(result, Mapping):
            return result
    result = getattr(value, "__dict__", None)
    if isinstance(result, Mapping) and result:
        return result
    attributes: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            item = getattr(value, name)
        except Exception:
            continue
        if callable(item):
            continue
        if item is None or isinstance(item, (str, int, float, bool, Decimal, datetime, list, dict)):
            attributes[name] = item
    if attributes:
        return attributes
    raise HistoricalProviderError("provider object is not a mapping")


@dataclass(frozen=True, slots=True)
class ProviderProvenance:
    provider_id: str
    tier: str
    endpoint_family: str
    acquisition_timestamp: datetime
    archive_floor: datetime | None = None

    def __post_init__(self) -> None:
        _utc(self.acquisition_timestamp, "acquisition_timestamp")
        if self.archive_floor is not None:
            _utc(self.archive_floor, "archive_floor")


@dataclass(frozen=True, slots=True)
class HistoricalCutoffRecord:
    market_settled_timestamp: datetime
    trades_created_timestamp: datetime
    orders_updated_timestamp: datetime
    provider: ProviderProvenance


@dataclass(frozen=True, slots=True)
class HistoricalMarketRecord:
    ticker: str
    event_ticker: str
    series_ticker: str | None
    open_time: datetime
    close_time: datetime
    status: str
    result: str | None
    provider: ProviderProvenance
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HistoricalTradeRecord:
    trade_id: str
    ticker: str
    created_time: datetime
    count: Decimal | None
    yes_price: Decimal | None
    no_price: Decimal | None
    taker_side: str
    provider: ProviderProvenance
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HistoricalCandlestickRecord:
    ticker: str
    end_period_timestamp: datetime
    provider: ProviderProvenance
    raw: Mapping[str, Any]

    @property
    def complete_at(self) -> datetime:
        """Candlestick end is the earliest time at which its value is knowable."""

        return self.end_period_timestamp


@dataclass(frozen=True, slots=True)
class SnapshotLevel:
    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        if self.price < 0 or self.size < 0:
            raise HistoricalProviderError("L2 levels cannot contain negative values")


@dataclass(frozen=True, slots=True)
class HistoricalL2Snapshot:
    ticker: str
    series: str | None
    base_asset: str | None
    market_type: str | None
    received_timestamp: datetime
    yes: tuple[SnapshotLevel, ...]
    no: tuple[SnapshotLevel, ...]
    provider: ProviderProvenance
    quality_class: str = HISTORICAL_L2_SNAPSHOT


@dataclass(frozen=True, slots=True)
class HistoricalL2Tick:
    ticker: str
    received_timestamp: datetime
    sequence: int
    kind: str
    side: str
    price: Decimal
    delta: Decimal
    resting_size: Decimal
    provider: ProviderProvenance
    quality_class: str = HISTORICAL_L2_DELTA

    def __post_init__(self) -> None:
        if self.sequence < 0 or self.kind not in {"snapshot", "delta"}:
            raise HistoricalProviderError("invalid historical tick sequence or kind")
        if self.side not in {"yes", "no"}:
            raise HistoricalProviderError("invalid historical tick side")


@dataclass(frozen=True, slots=True)
class AcquisitionManifest:
    provider: str
    endpoint_family: str
    query_bounds: Mapping[str, object]
    tickers: tuple[str, ...]
    archive_floor: datetime | None
    page_count: int
    row_count: int
    content_hash: str
    code_sha: str

    @classmethod
    def build(
        cls,
        *,
        provider: str,
        endpoint_family: str,
        query_bounds: Mapping[str, object],
        tickers: Sequence[str],
        archive_floor: datetime | None,
        page_count: int,
        rows: Iterable[Mapping[str, object]],
        code_sha: str,
    ) -> AcquisitionManifest:
        if page_count < 0 or not code_sha:
            raise HistoricalProviderError("invalid acquisition manifest counts or code SHA")
        ordered = tuple(sorted((dict(row) for row in rows), key=lambda row: str(row)))
        canonical = json.dumps(
            {
                "provider": provider,
                "endpoint_family": endpoint_family,
                "query_bounds": dict(query_bounds),
                "tickers": sorted(set(tickers)),
                "archive_floor": archive_floor.isoformat() if archive_floor else None,
                "rows": ordered,
                "code_sha": code_sha,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return cls(
            provider=provider,
            endpoint_family=endpoint_family,
            query_bounds=dict(query_bounds),
            tickers=tuple(sorted(set(tickers))),
            archive_floor=archive_floor,
            page_count=page_count,
            row_count=len(ordered),
            content_hash=hashlib.sha256(canonical).hexdigest(),
            code_sha=code_sha,
        )


@runtime_checkable
class HistoricalMarketProvider(Protocol):
    """Narrow official-contract history surface; never a realtime Recorder interface."""

    def cutoff(self) -> HistoricalCutoffRecord: ...

    def markets(self, **kwargs: object) -> tuple[HistoricalMarketRecord, ...]: ...

    def trades(self, **kwargs: object) -> tuple[HistoricalTradeRecord, ...]: ...

    def candlesticks(
        self, ticker: str, **kwargs: object
    ) -> tuple[HistoricalCandlestickRecord, ...]: ...


@runtime_checkable
class HistoricalOrderbookProvider(Protocol):
    """Optional historical L2 surface, explicitly separate from Recorder."""

    def snapshots(self, ticker: str, **kwargs: object) -> tuple[HistoricalL2Snapshot, ...]: ...


class KalshiOfficialHistoricalProvider:
    """SDK-first read-only wrapper for official Kalshi historical contract data."""

    provider_id = KALSHI_OFFICIAL
    tier = "H1_KALSHI_OFFICIAL_HISTORY"

    def __init__(self, client: object | None = None, *, base_url: str | None = None):
        self._owned_client = client is None
        if client is None:
            from kalshi import KalshiClient
            from kalshi.config import PRODUCTION_BASE_URL

            client = KalshiClient(base_url=base_url or PRODUCTION_BASE_URL)
        historical = getattr(client, "historical", None)
        if historical is None:
            raise HistoricalProviderError("kalshi-sdk historical resource is unavailable")
        self._client = client
        self._historical = historical

    def close(self) -> None:
        if self._owned_client:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> KalshiOfficialHistoricalProvider:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _provenance(
        self, endpoint: str, *, archive_floor: datetime | None = None
    ) -> ProviderProvenance:
        return ProviderProvenance(
            self.provider_id,
            self.tier,
            endpoint,
            datetime.now(UTC),
            archive_floor,
        )

    def cutoff(self) -> HistoricalCutoffRecord:
        raw = _dump(self._historical.cutoff())
        return HistoricalCutoffRecord(
            _utc(raw["market_settled_ts"], "market_settled_ts"),
            _utc(raw["trades_created_ts"], "trades_created_ts"),
            _utc(raw["orders_updated_ts"], "orders_updated_ts"),
            self._provenance("historical_cutoff"),
        )

    def _page(self, method: str, **kwargs: object) -> tuple[list[Mapping[str, Any]], str | None]:
        page = getattr(self._historical, method)(**kwargs)
        items = getattr(page, "items", None)
        cursor = getattr(page, "cursor", None)
        if not isinstance(items, list) or (
            cursor not in (None, "") and not isinstance(cursor, str)
        ):
            raise HistoricalProviderError(f"malformed SDK {method} page")
        return [_dump(item) for item in items], cursor or None

    def markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        tickers: Sequence[str] | str | None = None,
        max_pages: int = 1,
        limit: int = 1000,
    ) -> tuple[HistoricalMarketRecord, ...]:
        return self._markets_or_trades(
            "markets",
            series_ticker=series_ticker,
            event_ticker=event_ticker,
            tickers=tickers,
            max_pages=max_pages,
            limit=limit,
        )

    def _markets_or_trades(self, method: str, **kwargs: object) -> tuple[Any, ...]:
        max_pages = int(kwargs.pop("max_pages", 1))
        if not 1 <= max_pages <= _MAX_PROBE_PAGES:
            raise ValueError("max_pages is outside the bounded historical probe range")
        limit = int(kwargs.pop("limit", 1000))
        cursor: str | None = None
        output: list[Any] = []
        for _ in range(max_pages):
            params = dict(kwargs)
            params["limit"] = limit
            if cursor:
                params["cursor"] = cursor
            rows, next_cursor = self._page(method, **params)
            if method == "markets":
                output.extend(self._market_record(row) for row in rows)
            else:
                output.extend(self._trade_record(row) for row in rows)
            if not next_cursor:
                break
            cursor = next_cursor
        return tuple(output)

    def _market_record(self, raw: Mapping[str, Any]) -> HistoricalMarketRecord:
        ticker = raw.get("ticker")
        event = raw.get("event_ticker")
        status = raw.get("status")
        if not all(isinstance(value, str) and value for value in (ticker, event, status)):
            raise HistoricalProviderError("official historical market identity is malformed")
        return HistoricalMarketRecord(
            ticker,
            event,
            raw.get("series_ticker") if isinstance(raw.get("series_ticker"), str) else None,
            _utc(raw.get("open_time"), "open_time"),
            _utc(raw.get("close_time"), "close_time"),
            status,
            raw.get("result") if isinstance(raw.get("result"), str) else None,
            self._provenance("historical_markets"),
            dict(raw),
        )

    def _trade_record(self, raw: Mapping[str, Any]) -> HistoricalTradeRecord:
        trade_id = raw.get("trade_id")
        ticker = raw.get("ticker")
        side = raw.get("taker_side")
        if not all(isinstance(value, str) and value for value in (trade_id, ticker, side)):
            raise HistoricalProviderError("official historical trade identity is malformed")
        return HistoricalTradeRecord(
            trade_id,
            ticker,
            _utc(raw.get("created_time"), "created_time"),
            _decimal(raw.get("count"), "count", allow_none=True),
            _decimal(raw.get("yes_price"), "yes_price", allow_none=True),
            _decimal(raw.get("no_price"), "no_price", allow_none=True),
            side,
            self._provenance("historical_trades"),
            dict(raw),
        )

    def trades(
        self,
        *,
        ticker: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        max_pages: int = 1,
        limit: int = 1000,
    ) -> tuple[HistoricalTradeRecord, ...]:
        return self._markets_or_trades(
            "trades",
            ticker=ticker,
            min_ts=min_ts,
            max_ts=max_ts,
            max_pages=max_pages,
            limit=limit,
        )

    def candlesticks(
        self,
        ticker: str,
        *,
        start: datetime,
        end: datetime,
        period_interval: int,
    ) -> tuple[HistoricalCandlestickRecord, ...]:
        start_utc, end_utc = _utc(start, "start"), _utc(end, "end")
        if start_utc >= end_utc or period_interval <= 0:
            raise ValueError("candlestick bounds and period_interval must be positive")
        rows = self._historical.candlesticks(
            ticker,
            start_ts=int(start_utc.timestamp()),
            end_ts=int(end_utc.timestamp()),
            period_interval=period_interval,
        )
        return tuple(
            HistoricalCandlestickRecord(
                ticker,
                datetime.fromtimestamp(int(_dump(row)["end_period_ts"]), tz=UTC),
                self._provenance("historical_candlesticks"),
                dict(_dump(row)),
            )
            for row in rows
        )


class DepthFeedHistoricalOrderbookProvider:
    """Optional, read-only DepthFeed adapter for historical L2 snapshots and ticks."""

    provider_id = DEPTHFEED_KALSHI_L2
    tier = "H2_DEPTHFEED_RECORDED_L2"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        session: object | None = None,
    ) -> None:
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self._base_url = (base_url or os.environ.get("DEPTHFEED_BASE_URL", "")).rstrip("/")
        self._session = session or requests.Session()
        self._owned_session = session is None

    @classmethod
    def from_project_secret(
        cls, *, project_root: Path | None = None, session: object | None = None
    ):
        path = resolve_secret_path(
            None,
            name="depthfeed-api-key.txt",
            project_root=project_root,
            legacy_paths=(),
        )
        key = path.read_text(encoding="utf-8").strip() if path else None
        return cls(key, session=session)

    @property
    def status(self) -> str:
        return "CONFIGURED" if self._api_key else DEPTHFEED_NOT_CONFIGURED

    def close(self) -> None:
        if self._owned_session:
            close = getattr(self._session, "close", None)
            if callable(close):
                close()

    def _require_key(self) -> None:
        if not self._api_key:
            raise HistoricalProviderNotConfigured(DEPTHFEED_NOT_CONFIGURED)

    def _get(self, path: str, params: Mapping[str, object]) -> Mapping[str, Any]:
        self._require_key()
        if not self._base_url:
            raise HistoricalProviderError("DEPTHFEED_BASE_URL_REQUIRED")
        response = self._session.get(
            f"{self._base_url}{path}",
            params=dict(params),
            headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise HistoricalProviderError("DepthFeed response must be an object")
        return payload

    def _provenance(self, endpoint: str) -> ProviderProvenance:
        return ProviderProvenance(self.provider_id, self.tier, endpoint, datetime.now(UTC))

    def discover_markets(self, *, limit: int = 1) -> tuple[Mapping[str, Any], ...]:
        payload = self._get("/v3/kalshi/markets", {"limit": limit})
        raw = payload.get("markets", payload.get("data", []))
        if not isinstance(raw, list):
            raise HistoricalProviderError("DepthFeed markets must be a list")
        return tuple(item for item in raw if isinstance(item, Mapping))

    @staticmethod
    def _levels(raw: object, field: str) -> tuple[SnapshotLevel, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, list):
            raise HistoricalProviderError(f"DepthFeed {field} ladder must be a list")
        seen: set[Decimal] = set()
        output: list[SnapshotLevel] = []
        for item in raw:
            if isinstance(item, Mapping):
                price_value, size_value = item.get("price"), item.get("size", item.get("quantity"))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                price_value, size_value = item
            else:
                raise HistoricalProviderError(f"malformed DepthFeed {field} level")
            price = _decimal(price_value, f"{field}.price")
            size = _decimal(size_value, f"{field}.size")
            assert price is not None and size is not None
            if price in seen:
                raise HistoricalProviderError(f"duplicate price in {field} ladder")
            seen.add(price)
            output.append(SnapshotLevel(price, size))
        return tuple(output)

    def parse_snapshot(self, raw: Mapping[str, Any]) -> HistoricalL2Snapshot:
        ticker = raw.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            raise HistoricalProviderError("DepthFeed snapshot ticker is missing")
        received = raw.get("received_timestamp", raw.get("receive_timestamp", raw.get("timestamp")))
        return HistoricalL2Snapshot(
            ticker,
            raw.get("series") if isinstance(raw.get("series"), str) else raw.get("series_ticker"),
            raw.get("base_asset") if isinstance(raw.get("base_asset"), str) else None,
            raw.get("market_type") if isinstance(raw.get("market_type"), str) else None,
            _utc(received, "received_timestamp"),
            self._levels(raw.get("yes", raw.get("yes_levels")), "yes"),
            self._levels(raw.get("no", raw.get("no_levels")), "no"),
            self._provenance("historical_snapshots"),
        )

    def parse_tick(self, raw: Mapping[str, Any]) -> HistoricalL2Tick:
        ticker = raw.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            raise HistoricalProviderError("DepthFeed tick ticker is missing")
        received = raw.get("received_timestamp", raw.get("receive_timestamp", raw.get("timestamp")))
        sequence = raw.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise HistoricalProviderError("DepthFeed tick sequence is malformed")
        return HistoricalL2Tick(
            ticker,
            _utc(received, "received_timestamp"),
            sequence,
            str(raw.get("kind", "")),
            str(raw.get("side", "")),
            _decimal(raw.get("price"), "price"),
            _decimal(raw.get("delta"), "delta"),
            _decimal(raw.get("resting_size", raw.get("size")), "resting_size"),
            self._provenance("historical_ticks"),
        )

    def snapshots(
        self, ticker: str, *, max_pages: int = 1, limit: int = 1
    ) -> tuple[HistoricalL2Snapshot, ...]:
        self._require_key()
        if not 1 <= max_pages <= _MAX_PROBE_PAGES:
            raise ValueError("max_pages is outside the bounded historical probe range")
        cursor: str | None = None
        result: list[HistoricalL2Snapshot] = []
        for _ in range(max_pages):
            params: dict[str, object] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            payload = self._get(f"/v3/kalshi/{quote(ticker, safe='')}/snapshots", params)
            rows = payload.get("snapshots", payload.get("data", []))
            if not isinstance(rows, list):
                raise HistoricalProviderError("DepthFeed snapshots must be a list")
            result.extend(self.parse_snapshot(row) for row in rows if isinstance(row, Mapping))
            cursor = payload.get("next_cursor", payload.get("cursor")) or None
            if cursor is not None and not isinstance(cursor, str):
                raise HistoricalProviderError("DepthFeed cursor is malformed")
            if not cursor:
                break
        return tuple(result)

    def ticks(
        self, ticker: str, *, max_pages: int = 1, limit: int = 1
    ) -> tuple[HistoricalL2Tick, ...]:
        self._require_key()
        if not 1 <= max_pages <= _MAX_PROBE_PAGES:
            raise ValueError("max_pages is outside the bounded historical probe range")
        cursor: str | None = None
        result: list[HistoricalL2Tick] = []
        for _ in range(max_pages):
            params: dict[str, object] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            payload = self._get(f"/v3/kalshi/{quote(ticker, safe='')}/ticks", params)
            rows = payload.get("ticks", payload.get("data", []))
            if not isinstance(rows, list):
                raise HistoricalProviderError("DepthFeed ticks must be a list")
            result.extend(self.parse_tick(row) for row in rows if isinstance(row, Mapping))
            cursor = payload.get("next_cursor", payload.get("cursor")) or None
            if cursor is not None and not isinstance(cursor, str):
                raise HistoricalProviderError("DepthFeed cursor is malformed")
            if not cursor:
                break
        return tuple(result)


def filter_candlesticks_asof(
    candles: Iterable[HistoricalCandlestickRecord], decision_timestamp: datetime
) -> tuple[HistoricalCandlestickRecord, ...]:
    """Keep only candles whose completed period is available at the decision."""

    decision = _utc(decision_timestamp, "decision_timestamp")
    return tuple(candle for candle in candles if candle.complete_at <= decision)


def select_latest_asof(
    observations: Iterable[HistoricalL2Snapshot | HistoricalL2Tick],
    decision_timestamp: datetime,
) -> HistoricalL2Snapshot | HistoricalL2Tick | None:
    """Select only the latest available provider observation at/before a decision."""

    decision = _utc(decision_timestamp, "decision_timestamp")
    eligible = [item for item in observations if item.received_timestamp <= decision]
    return max(eligible, key=lambda item: item.received_timestamp) if eligible else None


def depthfeed_key_status(*, project_root: Path | None = None) -> str:
    """Return configuration status without exposing the key or its contents."""

    path = resolve_secret_path(None, name="depthfeed-api-key.txt", project_root=project_root)
    if path and path.is_file() and path.stat().st_size > 0:
        return "CONFIGURED"
    return (
        "CONFIGURED"
        if os.environ.get("DEPTHFEED_API_KEY", "").strip()
        else DEPTHFEED_NOT_CONFIGURED
    )
