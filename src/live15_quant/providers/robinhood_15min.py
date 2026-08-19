"""Public-web discovery for Robinhood Live 15-minute event contracts.

This module intentionally uses only the public, server-rendered category page. It does not
call Robinhood private APIs, authenticate, or expose trading operations.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from live15_quant.config import ROBINHOOD_15MIN_PUBLIC_URL, Settings
from live15_quant.models import (
    Asset,
    ContractQuote,
    FifteenMinuteContract,
    FreshnessState,
    LifecycleState,
    SupportLevel,
)
from live15_quant.settlement import SETTLEMENT_SPECS

logger = logging.getLogger(__name__)

_TITLE_PATTERN = re.compile(
    r"^(?P<asset>BTC|ETH|Gold|Silver|XRP|WTI Oil|SOL|HYPE|DOGE|BNB) "
    r"15 min · (?P<start>\d{1,2}:\d{2})\N{EN DASH}(?P<end>\d{1,2}:\d{2}) "
    r"(?P<meridiem>AM|PM) (?P<zone>EDT|EST)$"
)
_TARGET_PATTERN = re.compile(r"^\$(?P<value>[\d,]+(?:\.\d+)?) or above$")
_PROBABILITY_PATTERN = re.compile(r"^(?P<value>\d+(?:\.\d+)?)%$")
_MONTH_DAY_PATTERN = re.compile(r"^(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2})$")
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
_VENUE_CANDIDATES = (
    "KalshiEX LLC",
    "ForecastEX, LLC",
    "Rothera Exchange and Clearing LLC",
)


class RobinhoodPublicPageError(ValueError):
    """Raised when the public 15-minute page cannot be safely normalized."""


class HttpResponse(Protocol):
    text: str
    headers: Mapping[str, str]
    url: str

    def raise_for_status(self) -> None: ...


class HttpSession(Protocol):
    def get(self, url: str, *, timeout: float, headers: Mapping[str, str]) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class _PublicCard:
    title: str
    target_price: Decimal
    yes_probability: Decimal | None
    source_url: str


class _NextDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_next_data = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("id") == "__NEXT_DATA__":
            self._in_next_data = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_next_data:
            self._in_next_data = False

    def handle_data(self, data: str) -> None:
        if self._in_next_data:
            self._parts.append(data)

    @property
    def payload(self) -> str:
        return "".join(self._parts)


class _CardParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self._base_url = base_url
        self._in_h2 = False
        self._h2_parts: list[str] = []
        self._title: str | None = None
        self._texts: list[str] = []
        self._source_url: str | None = None
        self.cards: dict[str, _PublicCard] = {}

    def _finish_card(self) -> None:
        if self._title is None or self._source_url is None:
            return
        title_match = _TITLE_PATTERN.fullmatch(self._title)
        if title_match is None:
            return
        target: Decimal | None = None
        probability: Decimal | None = None
        for text in self._texts:
            if target is None and (target_match := _TARGET_PATTERN.fullmatch(text)):
                target = Decimal(target_match.group("value").replace(",", ""))
                if target <= 0:
                    raise RobinhoodPublicPageError("target price must be positive")
            if probability is None and (probability_match := _PROBABILITY_PATTERN.fullmatch(text)):
                probability = Decimal(probability_match.group("value")) / Decimal(100)
                if not Decimal(0) <= probability <= Decimal(1):
                    raise RobinhoodPublicPageError("displayed probability is outside [0, 1]")
        if target is None:
            return
        asset = Asset(title_match.group("asset"))
        card = _PublicCard(
            title=self._title,
            target_price=target,
            yes_probability=probability,
            source_url=self._source_url,
        )
        existing = self.cards.get(self._title)
        if existing is not None and existing != card:
            raise RobinhoodPublicPageError(f"conflicting public quote cards for {asset}")
        self.cards[self._title] = card

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h2":
            self._finish_card()
            self._in_h2 = True
            self._h2_parts = []
            self._title = None
            self._texts = []
            self._source_url = None
            return
        if self._title is not None and tag == "a" and self._source_url is None:
            href = dict(attrs).get("href")
            if href and "/prediction-markets/" in href and "/events/" in href:
                candidate = urljoin(self._base_url, href)
                base = urlparse(self._base_url)
                parsed = urlparse(candidate)
                if parsed.scheme == base.scheme and parsed.netloc == base.netloc:
                    self._source_url = candidate

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self._in_h2:
            self._in_h2 = False
            title = "".join(self._h2_parts).strip()
            if _TITLE_PATTERN.fullmatch(title):
                self._title = title

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text:
            return
        if self._in_h2:
            self._h2_parts.append(text)
        elif self._title is not None:
            self._texts.append(text)

    def close(self) -> None:
        super().close()
        self._finish_card()


def _retrying_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _parse_next_data(html: str) -> Mapping[str, Any]:
    parser = _NextDataParser()
    parser.feed(html)
    if not parser.payload:
        raise RobinhoodPublicPageError("public page does not contain __NEXT_DATA__")
    try:
        payload = json.loads(parser.payload)
        page_props = payload["props"]["pageProps"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise RobinhoodPublicPageError("invalid public Robinhood page data") from error
    if not isinstance(page_props, Mapping):
        raise RobinhoodPublicPageError("Robinhood pageProps must be an object")
    return page_props


def _parse_cards(html: str, base_url: str) -> dict[str, _PublicCard]:
    parser = _CardParser(base_url)
    parser.feed(html)
    parser.close()
    return parser.cards


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RobinhoodPublicPageError(f"{name} must be an object")
    return value


def _asset_states(page_props: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    states = _mapping(page_props.get("eventStates"), "eventStates")
    result: dict[str, Mapping[str, Any]] = {}
    windows: dict[tuple[Asset, str], str] = {}
    for value in states.values():
        if not isinstance(value, Mapping):
            continue
        subtitle = value.get("subtitle")
        if not isinstance(subtitle, str):
            continue
        match = _TITLE_PATTERN.fullmatch(subtitle)
        if match is not None:
            asset = Asset(match.group("asset"))
            event_id = value.get("eventId")
            if not isinstance(event_id, str) or not event_id:
                raise RobinhoodPublicPageError(f"missing public event id for {asset}")
            window = (asset, subtitle)
            existing_event_id = windows.get(window)
            if existing_event_id is not None and existing_event_id != event_id:
                raise RobinhoodPublicPageError(f"conflicting public events for {asset}")
            existing = result.get(event_id)
            if existing is not None and existing != value:
                raise RobinhoodPublicPageError(f"conflicting metadata for event {event_id}")
            windows[window] = event_id
            result[event_id] = value
    return result


def _target(value: object) -> Decimal | None:
    if not isinstance(value, str) or (match := _TARGET_PATTERN.fullmatch(value)) is None:
        return None
    try:
        target = Decimal(match.group("value").replace(",", ""))
    except InvalidOperation:
        return None
    return target if target > 0 else None


def _contract_details(page_props: Mapping[str, Any]) -> dict[str, tuple[str, Decimal]]:
    layouts = _mapping(page_props.get("nodeLayouts"), "nodeLayouts")
    result: dict[str, tuple[str, Decimal]] = {}
    for layout in layouts.values():
        if not isinstance(layout, Mapping):
            continue
        results = layout.get("results")
        if not isinstance(results, Mapping):
            continue
        components = results.get("components")
        if not isinstance(components, list):
            continue
        for component in components:
            if not isinstance(component, Mapping):
                continue
            event_component = component.get("eventComponent")
            if not isinstance(event_component, Mapping):
                continue
            event_id = event_component.get("eventId")
            contract_info = event_component.get("contractInfo")
            if not isinstance(event_id, str) or not isinstance(contract_info, list):
                continue
            for contract in contract_info:
                if not isinstance(contract, Mapping):
                    continue
                contract_id = contract.get("contractId")
                target = _target(contract.get("longName")) or _target(contract.get("shortName"))
                if isinstance(contract_id, str) and target is not None:
                    details = (contract_id, target)
                    existing = result.get(event_id)
                    if existing is not None and existing != details:
                        raise RobinhoodPublicPageError(
                            f"conflicting public contracts for event {event_id}"
                        )
                    result[event_id] = details
                    break
    return result


def _event_date(value: object, reference: datetime) -> datetime:
    if not isinstance(value, str) or (match := _MONTH_DAY_PATTERN.fullmatch(value)) is None:
        raise RobinhoodPublicPageError("eventProgress is not a public month/day value")
    month = _MONTHS[match.group("month")]
    day = int(match.group("day"))
    try:
        candidates = [
            datetime(reference.year + delta, month, day, tzinfo=UTC) for delta in (-1, 0, 1)
        ]
    except ValueError as error:
        raise RobinhoodPublicPageError("eventProgress contains an invalid date") from error
    return min(candidates, key=lambda candidate: abs(candidate - reference))


def _window(title: str, event_progress: object, fetched_at: datetime) -> tuple[datetime, datetime]:
    match = _TITLE_PATTERN.fullmatch(title)
    if match is None:
        raise RobinhoodPublicPageError(f"invalid 15-minute title: {title}")
    event_date = _event_date(event_progress, fetched_at)
    start_hour, start_minute = (int(part) for part in match.group("start").split(":"))
    end_hour, end_minute = (int(part) for part in match.group("end").split(":"))
    if not (1 <= start_hour <= 12 and 1 <= end_hour <= 12):
        raise RobinhoodPublicPageError(f"title contains an invalid hour: {title}")
    if not (0 <= start_minute <= 59 and 0 <= end_minute <= 59):
        raise RobinhoodPublicPageError(f"title contains an invalid minute: {title}")
    end_meridiem = match.group("meridiem")
    start_meridiem = end_meridiem
    if start_hour == 11 and end_hour == 12:
        start_meridiem = "PM" if end_meridiem == "AM" else "AM"

    def hour24(hour: int, meridiem: str) -> int:
        return (hour % 12) + (12 if meridiem == "PM" else 0)

    offset = -4 if match.group("zone") == "EDT" else -5
    eastern = timezone(timedelta(hours=offset), match.group("zone"))
    start = datetime(
        event_date.year,
        event_date.month,
        event_date.day,
        hour24(start_hour, start_meridiem),
        start_minute,
        tzinfo=eastern,
    )
    end = start + timedelta(minutes=15)
    if (end.hour, end.minute) != (hour24(end_hour, end_meridiem), end_minute):
        raise RobinhoodPublicPageError(f"title is not an exact 15-minute window: {title}")
    return start.astimezone(UTC), end.astimezone(UTC)


def _lifecycle(value: object) -> LifecycleState:
    status = str(value).upper().removeprefix("EVENT_STATUS_")
    return {
        "IN_PROGRESS": LifecycleState.LIVE,
        "TRADING": LifecycleState.LIVE,
        "ACTIVE": LifecycleState.LIVE,
        "OPEN": LifecycleState.LIVE,
        "FINAL": LifecycleState.CLOSED,
        "CLOSED": LifecycleState.CLOSED,
        "INACTIVE": LifecycleState.CLOSED,
        "CANCELED": LifecycleState.CLOSED,
        "CANCELLED": LifecycleState.CLOSED,
        "SETTLED": LifecycleState.SETTLED,
        "UPCOMING": LifecycleState.UPCOMING,
        "SCHEDULED": LifecycleState.UPCOMING,
    }.get(status, LifecycleState.UNKNOWN)


def _source_age(headers: Mapping[str, str], fetched_at: datetime) -> int | None:
    ages: list[int] = []
    value = headers.get("Age") or headers.get("age")
    if value is not None:
        try:
            ages.append(max(int(value), 0))
        except ValueError:
            pass
    header_date = _header_date(headers)
    if header_date is not None:
        ages.append(max(int((fetched_at - header_date).total_seconds()), 0))
    return max(ages) if ages else None


def _header_date(headers: Mapping[str, str]) -> datetime | None:
    value = headers.get("Date") or headers.get("date")
    if value is None:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def parse_public_15min_page(
    html: str,
    *,
    source_url: str,
    headers: Mapping[str, str],
    fetched_at: datetime,
    max_source_age_seconds: float,
) -> tuple[FifteenMinuteContract, ...]:
    """Normalize the public category page without calling unpublished endpoints."""

    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    fetched_at = fetched_at.astimezone(UTC)
    page_props = _parse_next_data(html)
    cards = _parse_cards(html, source_url)
    states = _asset_states(page_props)
    contract_details = _contract_details(page_props)
    source_age = _source_age(headers, fetched_at)
    reference = _header_date(headers) or fetched_at
    contracts: list[FifteenMinuteContract] = []
    for event_id, state in states.items():
        subtitle = state.get("subtitle")
        if (
            not isinstance(subtitle, str)
            or (title_match := _TITLE_PATTERN.fullmatch(subtitle)) is None
        ):
            raise RobinhoodPublicPageError(f"missing public event subtitle for {event_id}")
        asset = Asset(title_match.group("asset"))
        card = cards.get(subtitle)
        if event_id not in contract_details:
            logger.warning(
                "Skipping public event placeholder without contract metadata",
                extra={
                    "event": "robinhood_contract_metadata_unavailable",
                    "asset": asset,
                    "event_id": event_id,
                    "lifecycle": _lifecycle(state.get("eventStatus")),
                },
            )
            continue
        contract_id, layout_target = contract_details[event_id]
        start, end = _window(subtitle, state.get("eventProgress"), reference)
        if card is not None and (card.title != subtitle or card.target_price != layout_target):
            logger.warning(
                "Ignoring a quote card that does not match public event metadata",
                extra={"event": "robinhood_quote_mismatch", "asset": asset},
            )
            card = None
        if card is None:
            logger.info(
                "Robinhood event has no server-rendered quote card",
                extra={"event": "robinhood_quote_unavailable", "asset": asset},
            )
        lifecycle = _lifecycle(state.get("eventStatus"))
        freshness = (
            FreshnessState.UNKNOWN
            if source_age is None
            else FreshnessState.STALE
            if source_age > max_source_age_seconds
            or end < fetched_at - timedelta(seconds=max_source_age_seconds)
            or (
                lifecycle is LifecycleState.LIVE
                and not (
                    start - timedelta(seconds=max_source_age_seconds)
                    <= fetched_at
                    <= end + timedelta(seconds=max_source_age_seconds)
                )
            )
            else FreshnessState.FRESH
        )
        yes_probability = card.yes_probability if card is not None else None
        quote_availability = (
            SupportLevel.UNSUPPORTED if yes_probability is None else SupportLevel.PARTIAL
        )
        contracts.append(
            FifteenMinuteContract(
                asset=asset,
                event_id=event_id,
                contract_id=contract_id,
                start_time=start,
                end_time=end,
                target_price=card.target_price if card is not None else layout_target,
                quote=ContractQuote(
                    yes_probability=yes_probability,
                    no_probability=None,
                    availability=quote_availability,
                ),
                venue=None,
                venue_candidates=_VENUE_CANDIDATES,
                settlement=SETTLEMENT_SPECS[asset],
                lifecycle_state=lifecycle,
                source_url=card.source_url if card is not None else source_url,
                fetched_at=fetched_at,
                freshness_state=freshness,
                source_age_seconds=source_age,
            )
        )
    if not contracts:
        raise RobinhoodPublicPageError("no supported Live 15-minute events found on public page")
    return tuple(
        sorted(
            contracts,
            key=lambda contract: (contract.start_time, contract.asset.value, contract.event_id),
        )
    )


class Robinhood15MinuteProvider:
    """Discover and normalize current 15-minute events from Robinhood's public webpage."""

    def __init__(self, settings: Settings, session: HttpSession | None = None) -> None:
        if settings.robinhood_15min_url != ROBINHOOD_15MIN_PUBLIC_URL:
            raise ValueError("Robinhood discovery URL must be the verified public 15-minute page")
        self._settings = settings
        self._session = session or _retrying_session()

    def discover(self) -> tuple[FifteenMinuteContract, ...]:
        try:
            response = self._session.get(
                self._settings.robinhood_15min_url,
                timeout=self._settings.request_timeout_seconds,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": "LIVE15_QUANT/0.2 public-research collector",
                },
            )
            response.raise_for_status()
            if response.url.rstrip("/") != ROBINHOOD_15MIN_PUBLIC_URL.rstrip("/"):
                raise RobinhoodPublicPageError(
                    "public Robinhood category request redirected to an unexpected URL"
                )
            fetched_at = datetime.now(UTC)
            contracts = parse_public_15min_page(
                response.text,
                source_url=self._settings.robinhood_15min_url,
                headers=response.headers,
                fetched_at=fetched_at,
                max_source_age_seconds=self._settings.robinhood_max_source_age_seconds,
            )
        except Exception:
            logger.exception(
                "Robinhood public 15-minute discovery failed",
                extra={
                    "event": "robinhood_15min_discovery_error",
                    "source_url": self._settings.robinhood_15min_url,
                },
            )
            raise
        logger.info(
            "Robinhood public 15-minute discovery completed",
            extra={
                "event": "robinhood_15min_discovery",
                "contract_count": len(contracts),
                "assets": tuple(contract.asset for contract in contracts),
            },
        )
        return contracts
