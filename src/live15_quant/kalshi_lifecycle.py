"""Kalshi-native 15-minute market discovery and official settlement truth."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from live15_quant.models import Asset, DataRole
from live15_quant.providers.kalshi import (
    KALSHI_15MIN_SERIES,
    KALSHI_MARKET_DATA_DOCS,
    KalshiOfficialQuoteProvider,
    KalshiPublicApiError,
    KalshiTargetUnavailableError,
    _decimal,
    _market_target,
    _timestamp,
)

logger = logging.getLogger(__name__)


class KalshiLifecycle(StrEnum):
    UPCOMING = "upcoming"
    OPEN = "open"
    PAUSED = "paused"
    CLOSED = "closed"
    SETTLEMENT_PENDING = "settlement_pending"
    SETTLED_YES = "settled_yes"
    SETTLED_NO = "settled_no"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class KalshiResult(StrEnum):
    YES = "yes"
    NO = "no"


class WindowRelation(StrEnum):
    PREVIOUS = "previous"
    CURRENT = "current"
    NEXT = "next"
    FUTURE = "future"


@dataclass(frozen=True, slots=True)
class KalshiSettlementTruth:
    asset: Asset
    series: str
    ticker: str
    event_ticker: str
    window_start: datetime
    window_end: datetime
    target: Decimal
    result: KalshiResult
    settlement_timestamp: datetime
    settlement_value: Decimal | None
    expiration_value: str | None
    official_source: str
    fetched_timestamp: datetime
    role: DataRole = field(init=False, default=DataRole.SETTLEMENT_TRUTH)

    def __post_init__(self) -> None:
        if not all((self.series, self.ticker, self.event_ticker, self.official_source)):
            raise ValueError("settlement identifiers and source must not be empty")
        if self.series != KALSHI_15MIN_SERIES.get(self.asset):
            raise ValueError("settlement asset and exact series do not match")
        if not self.event_ticker.startswith(f"{self.series}-") or not self.ticker.startswith(
            f"{self.event_ticker}-"
        ):
            raise ValueError("settlement ticker hierarchy is inconsistent")
        for value in (
            self.window_start,
            self.window_end,
            self.settlement_timestamp,
            self.fetched_timestamp,
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("settlement timestamps must be timezone-aware")
        if self.window_end - self.window_start != timedelta(minutes=15):
            raise ValueError("settlement window must be exactly 15 minutes")
        if self.settlement_timestamp < self.window_end:
            raise ValueError("settlement timestamp cannot precede market close")
        if self.fetched_timestamp < self.settlement_timestamp:
            raise ValueError("settlement cannot be fetched before its official timestamp")
        if not self.target.is_finite() or self.target <= 0:
            raise ValueError("settlement target must be a positive finite Decimal")
        if self.settlement_value is not None and not self.settlement_value.is_finite():
            raise ValueError("settlement value must be finite")
        expected_value = Decimal(1) if self.result is KalshiResult.YES else Decimal(0)
        if self.settlement_value is not None and self.settlement_value != expected_value:
            raise ValueError("binary settlement value conflicts with official result")


@dataclass(frozen=True, slots=True)
class KalshiMarket:
    asset: Asset
    series: str
    ticker: str
    event_ticker: str
    window_start: datetime
    window_end: datetime
    target: Decimal
    lifecycle: KalshiLifecycle
    official_status: str
    fetched_timestamp: datetime
    source_url: str
    rules_primary: str
    rules_secondary: str
    settlement_timer_seconds: int
    determination_result: KalshiResult | None = None
    settlement: KalshiSettlementTruth | None = None

    def __post_init__(self) -> None:
        expected = KALSHI_15MIN_SERIES.get(self.asset)
        if self.series != expected:
            raise ValueError("market asset and exact series do not match")
        if not self.ticker.startswith(f"{self.series}-") or not self.event_ticker.startswith(
            f"{self.series}-"
        ):
            raise ValueError("market ticker does not match its exact series")
        if not self.ticker.startswith(f"{self.event_ticker}-"):
            raise ValueError("market ticker does not match its event ticker")
        if (
            self.window_start.tzinfo is None
            or self.window_start.utcoffset() is None
            or self.window_end.tzinfo is None
            or self.window_end.utcoffset() is None
        ):
            raise ValueError("market window must be timezone-aware")
        if self.window_end - self.window_start != timedelta(minutes=15):
            raise ValueError("market window must be exactly 15 minutes")
        if any(
            (
                self.window_start.second,
                self.window_start.microsecond,
                self.window_end.second,
                self.window_end.microsecond,
                self.window_start.minute % 15,
                self.window_end.minute % 15,
            )
        ):
            raise ValueError("market window must align to UTC quarter-hour boundaries")
        if not self.target.is_finite() or self.target <= 0:
            raise ValueError("market target must be a positive finite Decimal")
        if self.fetched_timestamp.tzinfo is None or self.fetched_timestamp.utcoffset() is None:
            raise ValueError("market fetched timestamp must be timezone-aware")
        if self.settlement is not None and (
            self.settlement.asset is not self.asset
            or self.settlement.series != self.series
            or self.settlement.ticker != self.ticker
            or self.settlement.event_ticker != self.event_ticker
            or self.settlement.window_start != self.window_start
            or self.settlement.window_end != self.window_end
            or self.settlement.target != self.target
        ):
            raise ValueError("settlement truth belongs to another market")


@dataclass(frozen=True, slots=True)
class KalshiDiscovery:
    asset: Asset
    fetched_timestamp: datetime
    previous: KalshiMarket | None
    current: KalshiMarket | None
    next: KalshiMarket | None
    future: tuple[KalshiMarket, ...]
    rejected_tickers: tuple[str, ...]

    @property
    def valid_markets(self) -> tuple[KalshiMarket, ...]:
        return tuple(
            market
            for market in (self.previous, self.current, self.next, *self.future)
            if market is not None
        )


@dataclass(frozen=True, slots=True)
class BackfillPage:
    asset: Asset
    source_path: str
    cursor_used: str | None
    next_cursor: str | None
    markets: tuple[KalshiMarket, ...]
    rejected_tickers: tuple[str, ...] = ()


_STATUS_LIFECYCLE: Mapping[str, KalshiLifecycle] = {
    "initialized": KalshiLifecycle.UPCOMING,
    "active": KalshiLifecycle.OPEN,
    "inactive": KalshiLifecycle.PAUSED,
    "closed": KalshiLifecycle.CLOSED,
    "determined": KalshiLifecycle.SETTLEMENT_PENDING,
    "disputed": KalshiLifecycle.SETTLEMENT_PENDING,
    "amended": KalshiLifecycle.SETTLEMENT_PENDING,
}


class KalshiLifecycleStateMachine:
    """Validate official/time-defined transitions without inferring a result."""

    _ALLOWED: Mapping[KalshiLifecycle, frozenset[KalshiLifecycle]] = {
        KalshiLifecycle.UPCOMING: frozenset(
            {KalshiLifecycle.UPCOMING, KalshiLifecycle.OPEN, KalshiLifecycle.INVALID}
        ),
        KalshiLifecycle.OPEN: frozenset(
            {KalshiLifecycle.OPEN, KalshiLifecycle.PAUSED, KalshiLifecycle.CLOSED}
        ),
        KalshiLifecycle.PAUSED: frozenset(
            {KalshiLifecycle.PAUSED, KalshiLifecycle.OPEN, KalshiLifecycle.CLOSED}
        ),
        KalshiLifecycle.CLOSED: frozenset(
            {KalshiLifecycle.CLOSED, KalshiLifecycle.SETTLEMENT_PENDING}
        ),
        KalshiLifecycle.SETTLEMENT_PENDING: frozenset(
            {
                KalshiLifecycle.SETTLEMENT_PENDING,
                KalshiLifecycle.SETTLED_YES,
                KalshiLifecycle.SETTLED_NO,
            }
        ),
        KalshiLifecycle.SETTLED_YES: frozenset({KalshiLifecycle.SETTLED_YES}),
        KalshiLifecycle.SETTLED_NO: frozenset({KalshiLifecycle.SETTLED_NO}),
        KalshiLifecycle.INVALID: frozenset({KalshiLifecycle.INVALID}),
        KalshiLifecycle.UNKNOWN: frozenset(
            {KalshiLifecycle.UNKNOWN, KalshiLifecycle.OPEN, KalshiLifecycle.CLOSED}
        ),
    }

    @classmethod
    def transition(
        cls, current: KalshiLifecycle, observed: KalshiLifecycle
    ) -> tuple[KalshiLifecycle, ...]:
        if (
            current in {KalshiLifecycle.UPCOMING, KalshiLifecycle.PAUSED, KalshiLifecycle.UNKNOWN}
            and observed is KalshiLifecycle.SETTLEMENT_PENDING
        ):
            return (KalshiLifecycle.CLOSED, KalshiLifecycle.SETTLEMENT_PENDING)
        if current in {
            KalshiLifecycle.UPCOMING,
            KalshiLifecycle.PAUSED,
            KalshiLifecycle.UNKNOWN,
        } and observed in {
            KalshiLifecycle.SETTLED_YES,
            KalshiLifecycle.SETTLED_NO,
        }:
            return (KalshiLifecycle.CLOSED, KalshiLifecycle.SETTLEMENT_PENDING, observed)
        if (
            current
            in {
                KalshiLifecycle.OPEN,
                KalshiLifecycle.PAUSED,
            }
            and observed is KalshiLifecycle.SETTLEMENT_PENDING
        ):
            return (KalshiLifecycle.CLOSED, KalshiLifecycle.SETTLEMENT_PENDING)
        if current in {KalshiLifecycle.OPEN, KalshiLifecycle.PAUSED} and observed in {
            KalshiLifecycle.SETTLED_YES,
            KalshiLifecycle.SETTLED_NO,
        }:
            return (
                KalshiLifecycle.CLOSED,
                KalshiLifecycle.SETTLEMENT_PENDING,
                observed,
            )
        if current is KalshiLifecycle.CLOSED and observed in {
            KalshiLifecycle.SETTLED_YES,
            KalshiLifecycle.SETTLED_NO,
        }:
            return (KalshiLifecycle.SETTLEMENT_PENDING, observed)
        if observed not in cls._ALLOWED[current]:
            raise KalshiPublicApiError(f"invalid lifecycle transition {current} -> {observed}")
        return (observed,)


class KalshiNativeMarketProvider:
    """Discover exact official 15-minute markets without Robinhood metadata."""

    def __init__(
        self,
        client: KalshiOfficialQuoteProvider,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._now = now or (lambda: datetime.now(UTC))

    @staticmethod
    def _result(raw: object) -> KalshiResult | None:
        if raw in (None, ""):
            return None
        try:
            return KalshiResult(str(raw).lower())
        except ValueError as error:
            raise KalshiPublicApiError("unsupported official market result") from error

    def parse_market(
        self,
        asset: Asset,
        raw: Mapping[str, Any],
        fetched_at: datetime,
        *,
        source_path: str = "/markets",
    ) -> KalshiMarket:
        if source_path not in {"/markets", "/historical/markets"}:
            raise ValueError("unsupported official market source path")
        series = KALSHI_15MIN_SERIES[asset]
        ticker = raw.get("ticker")
        event_ticker = raw.get("event_ticker")
        status = raw.get("status")
        if not isinstance(ticker, str) or not isinstance(event_ticker, str):
            raise KalshiPublicApiError("market identifiers are missing")
        if not isinstance(status, str):
            raise KalshiPublicApiError("market status is missing")
        start = _timestamp(raw.get("open_time"), "open_time")
        end = _timestamp(raw.get("close_time"), "close_time")
        target = _market_target(raw)
        result = self._result(raw.get("result"))
        if status not in {*_STATUS_LIFECYCLE, "finalized"}:
            raise KalshiPublicApiError(f"unsupported official market status {status!r}")
        if status in {"determined", "disputed", "amended"} and result is None:
            raise KalshiPublicApiError(f"{status} market is missing an official result")
        if status in {"initialized", "active", "inactive", "closed"} and result is not None:
            raise KalshiPublicApiError(f"{status} market unexpectedly contains a result")
        settlement: KalshiSettlementTruth | None = None
        if status == "finalized":
            settlement_ts = _timestamp(raw.get("settlement_ts"), "settlement_ts")
            if result is None:
                raise KalshiPublicApiError("finalized market is missing an official result")
            value_raw = raw.get("settlement_value_dollars")
            value = _decimal(value_raw, "settlement_value_dollars", optional=True)
            settlement = KalshiSettlementTruth(
                asset=asset,
                series=series,
                ticker=ticker,
                event_ticker=event_ticker,
                window_start=start,
                window_end=end,
                target=target,
                result=result,
                settlement_timestamp=settlement_ts,
                settlement_value=value,
                expiration_value=(
                    str(raw["expiration_value"])
                    if raw.get("expiration_value") not in (None, "")
                    else None
                ),
                official_source=f"{self._client.base_url}{source_path}/{ticker}",
                fetched_timestamp=fetched_at,
            )
            lifecycle = (
                KalshiLifecycle.SETTLED_YES
                if result is KalshiResult.YES
                else KalshiLifecycle.SETTLED_NO
            )
        else:
            lifecycle = _STATUS_LIFECYCLE[status]
        timer = raw.get("settlement_timer_seconds", 0)
        if (
            isinstance(timer, bool)
            or not isinstance(timer, (int, Decimal))
            or Decimal(timer) != Decimal(timer).to_integral_value()
            or int(timer) < 0
        ):
            raise KalshiPublicApiError("malformed settlement timer")
        return KalshiMarket(
            asset=asset,
            series=series,
            ticker=ticker,
            event_ticker=event_ticker,
            window_start=start,
            window_end=end,
            target=target,
            lifecycle=lifecycle,
            official_status=status,
            fetched_timestamp=fetched_at,
            source_url=f"{self._client.base_url}{source_path}/{ticker}",
            rules_primary=str(raw.get("rules_primary") or ""),
            rules_secondary=str(raw.get("rules_secondary") or ""),
            settlement_timer_seconds=int(timer),
            determination_result=result,
            settlement=settlement,
        )

    def _pages(
        self, path: str, params: Mapping[str, object], *, cursor: str | None = None
    ) -> Iterator[tuple[list[Mapping[str, Any]], str | None, str | None]]:
        seen: set[str] = set()
        current = cursor
        while True:
            page_params = dict(params)
            if current:
                if current in seen:
                    raise KalshiPublicApiError("Kalshi pagination cursor cycle detected")
                seen.add(current)
                page_params["cursor"] = current
            payload, _, _ = self._client.get_public(path, page_params)
            items = payload.get("markets")
            if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
                raise KalshiPublicApiError("Kalshi markets page is malformed")
            next_cursor = payload.get("cursor") or None
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise KalshiPublicApiError("Kalshi pagination cursor is malformed")
            yield list(items), current, next_cursor
            if not next_cursor:
                break
            current = next_cursor

    def discover(self, asset: Asset, now: datetime | None = None) -> KalshiDiscovery:
        observed = now or self._now()
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("discovery timestamp must be timezone-aware")
        query_time = observed.astimezone(UTC)
        params = {
            "series_ticker": KALSHI_15MIN_SERIES[asset],
            "min_close_ts": int((query_time - timedelta(minutes=30)).timestamp()),
            "max_close_ts": int((query_time + timedelta(hours=1)).timestamp()),
            "limit": 1000,
            "mve_filter": "exclude",
        }
        raw_items: list[Mapping[str, Any]] = []
        for items, _, _ in self._pages("/markets", params):
            raw_items.extend(items)
        fetched = query_time if now is not None else self._now().astimezone(UTC)
        parsed: list[KalshiMarket] = []
        rejected: list[str] = []
        seen_tickers: set[str] = set()
        for raw in raw_items:
            ticker = raw.get("ticker")
            if not isinstance(ticker, str):
                raise KalshiPublicApiError("market candidate is missing ticker")
            if ticker in seen_tickers:
                raise KalshiPublicApiError(f"duplicate market candidate {ticker}")
            seen_tickers.add(ticker)
            try:
                parsed.append(self.parse_market(asset, raw, fetched))
            except KalshiTargetUnavailableError:
                rejected.append(ticker)
        by_window: dict[tuple[datetime, datetime], list[KalshiMarket]] = {}
        for market in parsed:
            by_window.setdefault((market.window_start, market.window_end), []).append(market)
        conflict = next((items for items in by_window.values() if len(items) != 1), None)
        if conflict is not None:
            raise KalshiPublicApiError("conflicting markets occupy the same official UTC window")
        unique = sorted(parsed, key=lambda market: (market.window_start, market.ticker))
        current_items = [m for m in unique if m.window_start <= fetched < m.window_end]
        if len(current_items) > 1:
            raise KalshiPublicApiError("multiple current markets found for one exact series")
        current = current_items[0] if current_items else None
        boundary = (
            current.window_start
            if current is not None
            else fetched.replace(minute=(fetched.minute // 15) * 15, second=0, microsecond=0)
        )
        previous_items = [m for m in unique if m.window_end == boundary]
        next_boundary = boundary + timedelta(minutes=15)
        next_items = [m for m in unique if m.window_start == next_boundary]
        if len(previous_items) > 1 or len(next_items) > 1:
            raise KalshiPublicApiError("ambiguous adjacent market window")
        future = tuple(m for m in unique if m.window_start > next_boundary)
        return KalshiDiscovery(
            asset=asset,
            fetched_timestamp=fetched,
            previous=previous_items[0] if previous_items else None,
            current=current,
            next=next_items[0] if next_items else None,
            future=future,
            rejected_tickers=tuple(sorted(rejected)),
        )

    def discover_all(self, now: datetime | None = None) -> tuple[KalshiDiscovery, ...]:
        return tuple(self.discover(asset, now) for asset in KALSHI_15MIN_SERIES)

    def backfill_pages(
        self,
        asset: Asset,
        *,
        start: datetime,
        end: datetime,
        historical: bool,
        cursor: str | None = None,
    ) -> Iterator[BackfillPage]:
        if (
            start.tzinfo is None
            or start.utcoffset() is None
            or end.tzinfo is None
            or end.utcoffset() is None
            or start >= end
        ):
            raise ValueError("backfill range must be timezone-aware and increasing")
        path = "/historical/markets" if historical else "/markets"
        params: dict[str, object] = {
            "series_ticker": KALSHI_15MIN_SERIES[asset],
            "limit": 1000,
        }
        if not historical:
            params.update(
                {
                    "min_close_ts": int(start.timestamp()),
                    "max_close_ts": int(end.timestamp()),
                    "mve_filter": "exclude",
                }
            )
        for raw_items, used, next_cursor in self._pages(path, params, cursor=cursor):
            fetched = self._now().astimezone(UTC)
            markets: list[KalshiMarket] = []
            rejected: list[str] = []
            seen: set[str] = set()
            for raw in raw_items:
                ticker = raw.get("ticker")
                if not isinstance(ticker, str):
                    raise KalshiPublicApiError("historical candidate is missing ticker")
                try:
                    market = self.parse_market(asset, raw, fetched, source_path=path)
                except KalshiTargetUnavailableError:
                    rejected.append(ticker)
                    continue
                if market.ticker in seen:
                    raise KalshiPublicApiError("duplicate ticker within backfill page")
                seen.add(market.ticker)
                if market.window_end >= start and market.window_start < end:
                    markets.append(market)
            yield BackfillPage(
                asset=asset,
                source_path=path,
                cursor_used=used,
                next_cursor=next_cursor,
                markets=tuple(sorted(markets, key=lambda item: (item.window_start, item.ticker))),
                rejected_tickers=tuple(sorted(rejected)),
            )

    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return (KALSHI_MARKET_DATA_DOCS,)
