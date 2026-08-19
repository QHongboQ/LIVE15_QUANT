from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from live15_quant.config import ROBINHOOD_15MIN_PUBLIC_URL, Settings
from live15_quant.models import Asset, FreshnessState, LifecycleState, SupportLevel
from live15_quant.providers.robinhood_15min import (
    Robinhood15MinuteProvider,
    RobinhoodPublicPageError,
    _retrying_session,
    parse_public_15min_page,
)


def _page(
    *,
    title: str = "BTC 15 min · 2:15\N{EN DASH}2:30 PM EDT",
    event_progress: str = "Aug 19",
    status: str = "EVENT_STATUS_TRADING",
) -> str:
    event_id = "event-1"
    payload = {
        "props": {
            "pageProps": {
                "eventStates": {
                    event_id: {
                        "eventStatus": status,
                        "eventProgress": event_progress,
                        "subtitle": title,
                        "eventId": event_id,
                    }
                },
                "nodeLayouts": {
                    "layout": {
                        "results": {
                            "components": [
                                {
                                    "eventComponent": {
                                        "eventId": event_id,
                                        "contractInfo": [
                                            {
                                                "contractId": "contract-1",
                                                "longName": "$68,159.82 or above",
                                            }
                                        ],
                                    }
                                }
                            ]
                        }
                    }
                },
            }
        }
    }
    return (
        "<html><body>"
        f"<h2>{title}</h2>"
        "<span>$68,159.82 or above</span><span>61.5%</span>"
        '<a href="/us/en/prediction-markets/crypto/events/example/">View</a>'
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        "</body></html>"
    )


def _mutate_payload(html: str, mutate: Callable[[dict[str, Any]], None]) -> str:
    marker = '<script id="__NEXT_DATA__" type="application/json">'
    start = html.index(marker) + len(marker)
    end = html.index("</script>", start)
    payload = json.loads(html[start:end])
    mutate(payload)
    return f"{html[:start]}{json.dumps(payload)}{html[end:]}"


def test_parse_public_page_normalizes_contract_without_deriving_no_quote() -> None:
    fetched_at = datetime(2026, 8, 19, 18, 25, tzinfo=UTC)
    (contract,) = parse_public_15min_page(
        _page(),
        source_url="https://robinhood.com/us/en/prediction-markets/15-min/",
        headers={"Date": "Wed, 19 Aug 2026 18:25:00 GMT", "Age": "12"},
        fetched_at=fetched_at,
        max_source_age_seconds=360,
    )

    assert contract.asset is Asset.BTC
    assert contract.event_id == "event-1"
    assert contract.contract_id == "contract-1"
    assert contract.start_time == datetime(2026, 8, 19, 18, 15, tzinfo=UTC)
    assert contract.end_time == datetime(2026, 8, 19, 18, 30, tzinfo=UTC)
    assert contract.target_price == Decimal("68159.82")
    assert contract.quote.yes_probability == Decimal("0.615")
    assert contract.quote.no_probability is None
    assert contract.quote.availability is SupportLevel.PARTIAL
    assert contract.quote.is_executable is False
    assert contract.venue is None
    assert contract.venue_candidates == (
        "KalshiEX LLC",
        "ForecastEX, LLC",
        "Rothera Exchange and Clearing LLC",
    )
    assert contract.lifecycle_state is LifecycleState.LIVE
    assert contract.freshness_state is FreshnessState.FRESH
    assert contract.source_age_seconds == 12


def test_parse_cross_midnight_window_and_stale_snapshot() -> None:
    (contract,) = parse_public_15min_page(
        _page(
            title="BTC 15 min · 11:45\N{EN DASH}12:00 AM EDT",
            event_progress="Aug 19",
            status="EVENT_STATUS_FINAL",
        ),
        source_url="https://robinhood.com/us/en/prediction-markets/15-min/",
        headers={"Date": "Thu, 20 Aug 2026 04:01:00 GMT", "Age": "361"},
        fetched_at=datetime(2026, 8, 20, 4, 1, tzinfo=UTC),
        max_source_age_seconds=360,
    )

    assert contract.start_time == datetime(2026, 8, 20, 3, 45, tzinfo=UTC)
    assert contract.end_time == datetime(2026, 8, 20, 4, 0, tzinfo=UTC)
    assert contract.lifecycle_state is LifecycleState.CLOSED
    assert contract.freshness_state is FreshnessState.STALE


def test_parse_rejects_page_without_public_page_data() -> None:
    with pytest.raises(RobinhoodPublicPageError, match="__NEXT_DATA__"):
        parse_public_15min_page(
            "<html></html>",
            source_url="https://example.test/",
            headers={},
            fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
            max_source_age_seconds=360,
        )


def test_parse_rejects_malformed_next_data() -> None:
    html = _page()
    start = html.index("{", html.index("__NEXT_DATA__"))
    end = html.index("</script>", start)

    with pytest.raises(RobinhoodPublicPageError, match="invalid public"):
        parse_public_15min_page(
            f"{html[:start]}{{not-json{html[end:]}",
            source_url=ROBINHOOD_15MIN_PUBLIC_URL,
            headers={},
            fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
            max_source_age_seconds=360,
        )


@pytest.mark.parametrize(
    "title",
    [
        "BTC 1 hour · 2:15\N{EN DASH}3:15 PM EDT",
        "ADA 15 min · 2:15\N{EN DASH}2:30 PM EDT",
    ],
)
def test_parse_rejects_non_15minute_and_unsupported_assets(title: str) -> None:
    with pytest.raises(RobinhoodPublicPageError, match="no supported"):
        parse_public_15min_page(
            _page(title=title),
            source_url=ROBINHOOD_15MIN_PUBLIC_URL,
            headers={},
            fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
            max_source_age_seconds=360,
        )


def test_missing_nested_layout_fails_with_domain_error() -> None:
    html = _mutate_payload(
        _page(), lambda payload: payload["props"]["pageProps"].pop("nodeLayouts")
    )

    with pytest.raises(RobinhoodPublicPageError, match="nodeLayouts must be an object"):
        parse_public_15min_page(
            html,
            source_url=ROBINHOOD_15MIN_PUBLIC_URL,
            headers={},
            fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
            max_source_age_seconds=360,
        )


def test_conflicting_duplicate_asset_events_fail_safely() -> None:
    def duplicate(payload: dict[str, Any]) -> None:
        states = payload["props"]["pageProps"]["eventStates"]
        states["event-2"] = {**states["event-1"], "eventId": "event-2"}

    with pytest.raises(RobinhoodPublicPageError, match="conflicting public events"):
        parse_public_15min_page(
            _mutate_payload(_page(), duplicate),
            source_url=ROBINHOOD_15MIN_PUBLIC_URL,
            headers={},
            fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
            max_source_age_seconds=360,
        )


def test_duplicate_identical_quote_card_is_deduplicated() -> None:
    card = (
        "<h2>BTC 15 min · 2:15\N{EN DASH}2:30 PM EDT</h2>"
        "<span>$68,159.82 or above</span><span>61.5%</span>"
        '<a href="/us/en/prediction-markets/crypto/events/example/">View</a>'
    )
    html = _page().replace('<script id="__NEXT_DATA__"', f'{card}<script id="__NEXT_DATA__"')

    contracts = parse_public_15min_page(
        html,
        source_url=ROBINHOOD_15MIN_PUBLIC_URL,
        headers={"Date": "Wed, 19 Aug 2026 18:25:00 GMT"},
        fetched_at=datetime(2026, 8, 19, 18, 25, tzinfo=UTC),
        max_source_age_seconds=360,
    )

    assert len(contracts) == 1


def test_old_header_date_cannot_be_fresh_even_with_low_cache_age() -> None:
    (contract,) = parse_public_15min_page(
        _page(status="EVENT_STATUS_FINAL"),
        source_url=ROBINHOOD_15MIN_PUBLIC_URL,
        headers={"Date": "Wed, 19 Aug 2026 18:25:00 GMT", "Age": "1"},
        fetched_at=datetime(2026, 8, 19, 18, 45, tzinfo=UTC),
        max_source_age_seconds=360,
    )

    assert contract.source_age_seconds == 1200
    assert contract.freshness_state is FreshnessState.STALE


def test_missing_lifecycle_status_remains_unknown() -> None:
    html = _mutate_payload(
        _page(),
        lambda payload: payload["props"]["pageProps"]["eventStates"]["event-1"].pop("eventStatus"),
    )

    (contract,) = parse_public_15min_page(
        html,
        source_url=ROBINHOOD_15MIN_PUBLIC_URL,
        headers={"Date": "Wed, 19 Aug 2026 18:25:00 GMT"},
        fetched_at=datetime(2026, 8, 19, 18, 25, tzinfo=UTC),
        max_source_age_seconds=360,
    )

    assert contract.lifecycle_state is LifecycleState.UNKNOWN


def test_invalid_displayed_probability_fails_safely() -> None:
    with pytest.raises(RobinhoodPublicPageError, match="probability is outside"):
        parse_public_15min_page(
            _page().replace("61.5%", "101%"),
            source_url=ROBINHOOD_15MIN_PUBLIC_URL,
            headers={},
            fetched_at=datetime(2026, 8, 19, 18, 25, tzinfo=UTC),
            max_source_age_seconds=360,
        )


def test_mismatched_quote_card_is_not_attached_to_event() -> None:
    html = _page().replace("$68,159.82 or above", "$1.00 or above", 1)

    (contract,) = parse_public_15min_page(
        html,
        source_url=ROBINHOOD_15MIN_PUBLIC_URL,
        headers={"Date": "Wed, 19 Aug 2026 18:25:00 GMT"},
        fetched_at=datetime(2026, 8, 19, 18, 25, tzinfo=UTC),
        max_source_age_seconds=360,
    )

    assert contract.target_price == Decimal("68159.82")
    assert contract.quote.yes_probability is None
    assert contract.quote.availability is SupportLevel.UNSUPPORTED


def test_est_noon_boundary_is_normalized_to_utc() -> None:
    (contract,) = parse_public_15min_page(
        _page(
            title="BTC 15 min · 11:45\N{EN DASH}12:00 PM EST",
            event_progress="Dec 1",
        ),
        source_url=ROBINHOOD_15MIN_PUBLIC_URL,
        headers={"Date": "Tue, 01 Dec 2026 16:50:00 GMT", "Age": "1"},
        fetched_at=datetime(2026, 12, 1, 16, 50, tzinfo=UTC),
        max_source_age_seconds=360,
    )

    assert contract.start_time == datetime(2026, 12, 1, 16, 45, tzinfo=UTC)
    assert contract.end_time == datetime(2026, 12, 1, 17, 0, tzinfo=UTC)


def test_retry_policy_is_bounded_to_public_gets() -> None:
    retry = _retrying_session().get_adapter("https://").max_retries

    assert retry.total == 3
    assert retry.backoff_factor == 0.5
    assert retry.allowed_methods == frozenset({"GET"})


class FakeResponse:
    text = _page()
    url = ROBINHOOD_15MIN_PUBLIC_URL
    headers: ClassVar[dict[str, str]] = {
        "Date": "Wed, 19 Aug 2026 18:25:00 GMT",
        "Age": "1",
    }

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.request: tuple[str, dict[str, Any]] | None = None

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.request = (url, kwargs)
        return FakeResponse()


class RedirectedResponse(FakeResponse):
    url = "https://robinhood.com/login"


class RedirectedSession(FakeSession):
    def get(self, url: str, **kwargs: Any) -> RedirectedResponse:
        self.request = (url, kwargs)
        return RedirectedResponse()


class DisconnectedSession:
    def get(self, _url: str, **_kwargs: Any) -> FakeResponse:
        raise ConnectionError("network disconnected")


def test_provider_uses_only_configured_public_page() -> None:
    session = FakeSession()
    settings = Settings()

    contracts = Robinhood15MinuteProvider(settings, session=session).discover()

    assert len(contracts) == 1
    assert session.request is not None
    assert session.request[0] == ROBINHOOD_15MIN_PUBLIC_URL
    assert session.request[1]["timeout"] == 10.0


def test_structured_event_is_retained_when_visible_quote_card_is_absent() -> None:
    html = _page().replace(
        "<h2>BTC 15 min · 2:15\N{EN DASH}2:30 PM EDT</h2>"
        "<span>$68,159.82 or above</span><span>61.5%</span>"
        '<a href="/us/en/prediction-markets/crypto/events/example/">View</a>',
        "",
    )

    (contract,) = parse_public_15min_page(
        html,
        source_url="https://robinhood.com/us/en/prediction-markets/15-min/",
        headers={"Date": "Wed, 19 Aug 2026 18:25:00 GMT", "Age": "1"},
        fetched_at=datetime(2026, 8, 19, 18, 25, tzinfo=UTC),
        max_source_age_seconds=360,
    )

    assert contract.target_price == Decimal("68159.82")
    assert contract.quote.yes_probability is None
    assert contract.quote.availability is SupportLevel.UNSUPPORTED
    assert contract.source_url.endswith("/prediction-markets/15-min/")


def test_provider_rejects_unverified_discovery_url() -> None:
    with pytest.raises(ValueError, match="verified public"):
        Robinhood15MinuteProvider(
            Settings(robinhood_15min_url="https://example.test/private-api"),
            session=FakeSession(),
        )


def test_provider_rejects_unexpected_redirect() -> None:
    with pytest.raises(RobinhoodPublicPageError, match="redirected"):
        Robinhood15MinuteProvider(Settings(), session=RedirectedSession()).discover()


def test_provider_propagates_network_disconnect() -> None:
    with pytest.raises(ConnectionError, match="network disconnected"):
        Robinhood15MinuteProvider(Settings(), session=DisconnectedSession()).discover()
