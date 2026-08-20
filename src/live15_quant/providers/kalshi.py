"""Official unauthenticated Kalshi market-data provider for mapped 15-minute events."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote as url_quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from live15_quant.config import KALSHI_PUBLIC_API_BASE_URL, Settings
from live15_quant.models import (
    Asset,
    ExecutabilityClassification,
    FifteenMinuteContract,
    FreshnessState,
    KalshiNativeQuote,
    MappingConfidence,
    OrderBookLevel,
    PredictionMarketQuote,
    SourceTimestampKind,
    Venue,
    VenueMapping,
)

if TYPE_CHECKING:
    from live15_quant.kalshi_lifecycle import KalshiMarket

logger = logging.getLogger(__name__)

KALSHI_MARKET_DATA_DOCS = "https://docs.kalshi.com/getting_started/quick_start_market_data"
KALSHI_WEBSOCKET_DOCS = "https://docs.kalshi.com/getting_started/quick_start_websockets"
KALSHI_CRYPTO_15M_TERMS = "https://assets.kalshi.com/contract_terms/CRYPTO15M.pdf"
KALSHI_COMMODITY_15M_TERMS = "https://assets.kalshi.com/contract_terms/COMMOD15M.pdf"

KALSHI_15MIN_SERIES: Mapping[Asset, str] = {
    Asset.BTC: "KXBTC15M",
    Asset.ETH: "KXETH15M",
    Asset.GOLD: "KXGOLD15M",
    Asset.SILVER: "KXSILVER15M",
    Asset.XRP: "KXXRP15M",
    Asset.WTI_OIL: "KXWTI15M",
    Asset.SOL: "KXSOL15M",
    Asset.HYPE: "KXHYPE15M",
    Asset.DOGE: "KXDOGE15M",
    Asset.BNB: "KXBNB15M",
}

_TARGET_SUBTITLE = re.compile(r"Target Price:\s*\$([0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE)


class KalshiPublicApiError(RuntimeError):
    """Raised for malformed or inconsistent official Kalshi market data."""


class KalshiTargetUnavailableError(KalshiPublicApiError):
    """Raised only when an otherwise valid official market has no published target yet."""


class HttpResponse(Protocol):
    text: str
    headers: Mapping[str, str]
    url: str

    def raise_for_status(self) -> None: ...


class HttpSession(Protocol):
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, object] | None,
        timeout: float,
        headers: Mapping[str, str],
    ) -> HttpResponse: ...


def _retrying_session(retry_total: int = 4) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retry_total,
        connect=retry_total,
        read=retry_total,
        status=retry_total,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10))
    return session


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise KalshiPublicApiError(f"malformed {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise KalshiPublicApiError(f"malformed {field}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise KalshiPublicApiError(f"malformed {field}")
    return parsed.astimezone(UTC)


def _decimal(value: object, field: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise KalshiPublicApiError(f"malformed {field}")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise KalshiPublicApiError(f"malformed {field}") from error
    if not parsed.is_finite():
        raise KalshiPublicApiError(f"malformed {field}")
    return parsed


def _json(response: HttpResponse) -> Mapping[str, Any]:
    try:
        payload = json.loads(response.text, parse_float=Decimal, parse_int=Decimal)
    except (json.JSONDecodeError, TypeError) as error:
        raise KalshiPublicApiError("malformed Kalshi JSON payload") from error
    if not isinstance(payload, Mapping):
        raise KalshiPublicApiError("Kalshi payload must be an object")
    return payload


def _http_date(headers: Mapping[str, str]) -> datetime | None:
    raw = headers.get("Date") or headers.get("date")
    if raw is None:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _terms_for(asset: Asset) -> str:
    if asset in {Asset.GOLD, Asset.SILVER, Asset.WTI_OIL}:
        return KALSHI_COMMODITY_15M_TERMS
    return KALSHI_CRYPTO_15M_TERMS


def _market_target(market: Mapping[str, Any]) -> Decimal:
    """Read the explicit target string before a potentially lower-precision numeric strike."""

    subtitle = market.get("yes_sub_title")
    if isinstance(subtitle, str) and subtitle.strip():
        if re.fullmatch(r"Target Price:\s*TBD", subtitle.strip(), re.IGNORECASE):
            raise KalshiTargetUnavailableError("official market target is not published")
        match = _TARGET_SUBTITLE.fullmatch(subtitle)
        if match is None:
            raise KalshiPublicApiError("malformed official market target")
        target = _decimal(match.group(1).replace(",", ""), "yes_sub_title target")
    else:
        if subtitle is not None and not isinstance(subtitle, str):
            raise KalshiPublicApiError("malformed official market target")
        if market.get("floor_strike") is None:
            raise KalshiTargetUnavailableError("official market target is not published")
        target = _decimal(market.get("floor_strike"), "floor_strike")
    if target is None or target <= 0:
        raise KalshiPublicApiError("malformed official market target")
    return target


class KalshiOfficialQuoteProvider:
    """Read official Kalshi REST quotes without account or API credentials."""

    def __init__(
        self,
        settings: Settings,
        session: HttpSession | None = None,
        *,
        retry_total: int = 4,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if settings.kalshi_public_api_base_url != KALSHI_PUBLIC_API_BASE_URL:
            raise ValueError("Kalshi base URL must be the documented public production endpoint")
        if retry_total < 0:
            raise ValueError("retry_total must be non-negative")
        self._settings = settings
        self._owned_session = _retrying_session(retry_total) if session is None else None
        self._session = self._owned_session or session
        self._deadline_monotonic = deadline_monotonic
        self._monotonic = monotonic

    def close(self) -> None:
        if self._owned_session is not None:
            self._owned_session.close()

    @property
    def base_url(self) -> str:
        return self._settings.kalshi_public_api_base_url

    def get_public(
        self, path: str, params: Mapping[str, object] | None = None
    ) -> tuple[Mapping[str, Any], Mapping[str, str], str]:
        """Expose this provider's validated GET-only transport to native metadata readers."""

        return self._get(path, params)

    def _get(
        self, path: str, params: Mapping[str, object] | None = None
    ) -> tuple[Mapping[str, Any], Mapping[str, str], str]:
        url = f"{self._settings.kalshi_public_api_base_url}{path}"
        timeout = self._settings.request_timeout_seconds
        if self._deadline_monotonic is not None:
            remaining = self._deadline_monotonic - self._monotonic()
            if remaining <= 0:
                raise requests.Timeout("Kalshi acceptance deadline elapsed")
            timeout = min(timeout, max(remaining, 0.001))
        response = self._session.get(
            url,
            params=params,
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "LIVE15_QUANT/0.3 authorized-market-data",
            },
        )
        response.raise_for_status()
        if not response.url.startswith(f"{KALSHI_PUBLIC_API_BASE_URL}/"):
            raise KalshiPublicApiError("Kalshi request redirected outside the documented API")
        return _json(response), response.headers, response.url

    @staticmethod
    def series_for(asset: Asset) -> str:
        """Return an audited target series and reject values outside the ten-asset scope."""

        if not isinstance(asset, Asset) or asset not in KALSHI_15MIN_SERIES:
            raise KalshiPublicApiError("unsupported 15-minute asset")
        return KALSHI_15MIN_SERIES[asset]

    def _markets(self, asset: Asset) -> tuple[list[Mapping[str, Any]], Mapping[str, str]]:
        series = self.series_for(asset)
        payload, headers, _ = self._get(
            "/markets", {"series_ticker": series, "status": "open", "limit": 100}
        )
        raw_markets = payload.get("markets")
        if not isinstance(raw_markets, list):
            raise KalshiPublicApiError("Kalshi markets must be a list")
        markets: list[Mapping[str, Any]] = []
        for raw in raw_markets:
            if not isinstance(raw, Mapping):
                raise KalshiPublicApiError("Kalshi market must be an object")
            ticker = raw.get("ticker")
            event_ticker = raw.get("event_ticker")
            if (
                not isinstance(ticker, str)
                or not ticker.startswith(f"{series}-")
                or not isinstance(event_ticker, str)
                or not event_ticker.startswith(f"{series}-")
            ):
                raise KalshiPublicApiError(f"ticker mismatch for official series {series}")
            markets.append(raw)
        return markets, headers

    def map_contract(
        self, contract: FifteenMinuteContract
    ) -> tuple[VenueMapping, Mapping[str, Any] | None, Mapping[str, str]]:
        """Map only by exact official series, UTC window, and target; never by title."""

        series = self.series_for(contract.asset)
        markets, headers = self._markets(contract.asset)
        exact: list[Mapping[str, Any]] = []
        for market in markets:
            start = _timestamp(market.get("open_time"), "open_time")
            end = _timestamp(market.get("close_time"), "close_time")
            if start != contract.start_time.astimezone(UTC) or end != contract.end_time.astimezone(
                UTC
            ):
                continue
            target = _market_target(market)
            if target == contract.target_price:
                exact.append(market)

        evidence = (
            contract.source_url,
            f"{KALSHI_PUBLIC_API_BASE_URL}/series/{series}",
            KALSHI_MARKET_DATA_DOCS,
            _terms_for(contract.asset),
        )
        if len(exact) != 1:
            mapping = VenueMapping(
                asset=contract.asset,
                robinhood_event_id=contract.event_id,
                robinhood_contract_id=contract.contract_id,
                venue=Venue.KALSHI,
                venue_series=series,
                venue_ticker=None,
                confidence=MappingConfidence.PARTIAL,
                matched_fields=("official_asset_series",),
                evidence_urls=evidence,
                notes=(
                    "No unique Kalshi instrument matched exact UTC start/end and target"
                    if not exact
                    else "Multiple Kalshi instruments matched; mapping rejected as ambiguous"
                ),
            )
            return mapping, None, headers

        market = exact[0]
        ticker = market["ticker"]
        assert isinstance(ticker, str)
        mapping = VenueMapping(
            asset=contract.asset,
            robinhood_event_id=contract.event_id,
            robinhood_contract_id=contract.contract_id,
            venue=Venue.KALSHI,
            venue_series=series,
            venue_ticker=ticker,
            confidence=MappingConfidence.VERIFIED,
            matched_fields=(
                "official_asset_series",
                "start_time",
                "end_time",
                "target_price",
                "explicit_official_target",
                "unique_instrument",
            ),
            evidence_urls=(*evidence, f"{KALSHI_PUBLIC_API_BASE_URL}/markets/{ticker}"),
            notes="Exact deterministic join; no title or fuzzy-price matching used",
        )
        return mapping, market, headers

    def _orderbook(
        self, ticker: str
    ) -> tuple[tuple[OrderBookLevel, ...], tuple[OrderBookLevel, ...]]:
        payload, _, _ = self._get(
            f"/markets/{url_quote(ticker, safe='')}/orderbook",
            {"depth": self._settings.official_quote_orderbook_depth},
        )
        book = payload.get("orderbook_fp")
        if not isinstance(book, Mapping):
            raise KalshiPublicApiError("Kalshi orderbook_fp must be an object")

        def levels(field: str) -> tuple[OrderBookLevel, ...]:
            raw_levels = book.get(field)
            if raw_levels is None:
                return ()
            if not isinstance(raw_levels, list):
                raise KalshiPublicApiError(f"Kalshi {field} must be a list")
            result: list[OrderBookLevel] = []
            for raw in raw_levels:
                if not isinstance(raw, list) or len(raw) != 2:
                    raise KalshiPublicApiError(f"malformed Kalshi {field} level")
                price = _decimal(raw[0], f"{field} price")
                quantity = _decimal(raw[1], f"{field} quantity")
                assert price is not None and quantity is not None
                result.append(OrderBookLevel(price=price, quantity=quantity))
            return tuple(sorted(result, key=lambda level: level.price, reverse=True))

        return levels("yes_dollars"), levels("no_dollars")

    def quote_native(self, contract: KalshiMarket) -> KalshiNativeQuote:
        """Quote one exact Kalshi-native market without any Robinhood dependency."""

        expected_series = self.series_for(contract.asset)
        if contract.series != expected_series:
            raise KalshiPublicApiError("native market series does not match asset")
        payload, headers, _ = self._get(f"/markets/{url_quote(contract.ticker, safe='')}", None)
        market = payload.get("market")
        if not isinstance(market, Mapping):
            raise KalshiPublicApiError("Kalshi market detail must be an object")
        if (
            market.get("ticker") != contract.ticker
            or market.get("event_ticker") != contract.event_ticker
        ):
            raise KalshiPublicApiError("native market detail instrument mismatch")
        if (
            _timestamp(market.get("open_time"), "open_time") != contract.window_start
            or _timestamp(market.get("close_time"), "close_time") != contract.window_end
            or _market_target(market) != contract.target
        ):
            raise KalshiPublicApiError("native market detail metadata changed unexpectedly")
        yes_depth, no_depth = self._orderbook(contract.ticker)
        received = datetime.now(UTC)
        source_timestamp = _http_date(headers)
        source_age = (
            None
            if source_timestamp is None
            else max((received - source_timestamp).total_seconds(), 0.0)
        )
        freshness = (
            FreshnessState.UNKNOWN
            if source_age is None
            else FreshnessState.STALE
            if source_age > self._settings.official_quote_max_source_age_seconds
            else FreshnessState.FRESH
        )
        source = f"{KALSHI_PUBLIC_API_BASE_URL}/markets/{contract.ticker}"
        return KalshiNativeQuote(
            asset=contract.asset,
            series=contract.series,
            ticker=contract.ticker,
            event_ticker=contract.event_ticker,
            source_timestamp=source_timestamp,
            source_timestamp_kind=(
                SourceTimestampKind.HTTP_RESPONSE_DATE
                if source_timestamp is not None
                else SourceTimestampKind.UNAVAILABLE
            ),
            received_timestamp=received,
            yes_bid=_decimal(market.get("yes_bid_dollars"), "yes_bid_dollars", optional=True),
            yes_ask=_decimal(market.get("yes_ask_dollars"), "yes_ask_dollars", optional=True),
            no_bid=_decimal(market.get("no_bid_dollars"), "no_bid_dollars", optional=True),
            no_ask=_decimal(market.get("no_ask_dollars"), "no_ask_dollars", optional=True),
            last_trade=_decimal(
                market.get("last_price_dollars"), "last_price_dollars", optional=True
            ),
            volume=_decimal(market.get("volume_fp"), "volume_fp", optional=True),
            yes_bid_depth=yes_depth,
            no_bid_depth=no_depth,
            source=source,
            freshness=freshness,
            executability=ExecutabilityClassification.OFFICIAL_VENUE_ORDER_BOOK,
            evidence_urls=(
                KALSHI_MARKET_DATA_DOCS,
                f"{KALSHI_PUBLIC_API_BASE_URL}/series/{contract.series}",
                source,
                _terms_for(contract.asset),
            ),
        )

    def quotes_native(self, contracts: Sequence[KalshiMarket]) -> tuple[KalshiNativeQuote, ...]:
        result: list[KalshiNativeQuote] = []
        for contract in contracts:
            try:
                result.append(self.quote_native(contract))
            except Exception:
                logger.exception(
                    "Kalshi-native quote acquisition failed",
                    extra={
                        "event": "kalshi_native_quote_error",
                        "asset": contract.asset,
                        "venue_ticker": contract.ticker,
                    },
                )
        return tuple(result)

    def quote(self, contract: FifteenMinuteContract) -> PredictionMarketQuote | None:
        mapping, market, headers = self.map_contract(contract)
        if mapping.confidence is not MappingConfidence.VERIFIED or market is None:
            logger.warning(
                "Official venue mapping is not verified; quote suppressed",
                extra={
                    "event": "official_quote_mapping_unverified",
                    "asset": contract.asset,
                    "event_id": contract.event_id,
                    "mapping_confidence": mapping.confidence,
                },
            )
            return None
        assert mapping.venue is not None
        assert mapping.venue_series is not None
        assert mapping.venue_ticker is not None
        yes_depth, no_depth = self._orderbook(mapping.venue_ticker)

        received = datetime.now(UTC)
        source_timestamp = _http_date(headers)
        source_timestamp_kind = (
            SourceTimestampKind.HTTP_RESPONSE_DATE
            if source_timestamp is not None
            else SourceTimestampKind.UNAVAILABLE
        )
        source_age = (
            None
            if source_timestamp is None
            else max((received - source_timestamp).total_seconds(), 0.0)
        )
        freshness = (
            FreshnessState.UNKNOWN
            if source_age is None
            else FreshnessState.STALE
            if source_age > self._settings.official_quote_max_source_age_seconds
            else FreshnessState.FRESH
        )
        ticker = mapping.venue_ticker
        return PredictionMarketQuote(
            asset=contract.asset,
            robinhood_event_id=contract.event_id,
            robinhood_contract_id=contract.contract_id,
            venue=mapping.venue,
            venue_series=mapping.venue_series,
            venue_ticker=ticker,
            mapping_confidence=mapping.confidence,
            source_timestamp=source_timestamp,
            source_timestamp_kind=source_timestamp_kind,
            received_timestamp=received,
            yes_bid=_decimal(market.get("yes_bid_dollars"), "yes_bid_dollars", optional=True),
            yes_ask=_decimal(market.get("yes_ask_dollars"), "yes_ask_dollars", optional=True),
            no_bid=_decimal(market.get("no_bid_dollars"), "no_bid_dollars", optional=True),
            no_ask=_decimal(market.get("no_ask_dollars"), "no_ask_dollars", optional=True),
            last_trade=_decimal(
                market.get("last_price_dollars"), "last_price_dollars", optional=True
            ),
            volume=_decimal(market.get("volume_fp"), "volume_fp", optional=True),
            yes_bid_depth=yes_depth,
            no_bid_depth=no_depth,
            source=f"{KALSHI_PUBLIC_API_BASE_URL}/markets/{ticker}",
            freshness=freshness,
            executability=ExecutabilityClassification.OFFICIAL_VENUE_ORDER_BOOK,
            evidence_urls=mapping.evidence_urls,
        )

    def quotes(
        self, contracts: Sequence[FifteenMinuteContract]
    ) -> tuple[PredictionMarketQuote, ...]:
        """Return verified official quotes; failures are isolated per asset."""

        result: list[PredictionMarketQuote] = []
        for contract in contracts:
            try:
                item = self.quote(contract)
            except Exception:
                logger.exception(
                    "Official Kalshi quote acquisition failed",
                    extra={
                        "event": "official_quote_error",
                        "asset": contract.asset,
                        "event_id": contract.event_id,
                    },
                )
                continue
            if item is not None:
                result.append(item)
        return tuple(result)
