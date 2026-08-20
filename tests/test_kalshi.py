from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest

from live15_quant.config import KALSHI_PUBLIC_API_BASE_URL, Settings
from live15_quant.models import (
    Asset,
    FreshnessState,
    MappingConfidence,
    SourceTimestampKind,
    Venue,
)
from live15_quant.providers.kalshi import (
    KALSHI_15MIN_SERIES,
    KalshiOfficialQuoteProvider,
    KalshiPublicApiError,
    _retrying_session,
)
from live15_quant.settlement import SETTLEMENT_SPECS
from tests.test_storage import contract


class FakeResponse:
    def __init__(
        self,
        payload: object,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        raw_text: str | None = None,
    ) -> None:
        self.text = json.dumps(payload) if raw_text is None else raw_text
        self.url = url
        self.headers = headers or {"Date": format_datetime(datetime.now(UTC))}

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self, market_payload: object, orderbook_payload: object | None = None) -> None:
        self.market_payload = market_payload
        self.orderbook_payload = (
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.5100", "12.340000"]],
                    "no_dollars": [["0.4800", "8.900000"]],
                }
            }
            if orderbook_payload is None
            else orderbook_payload
        )
        self.headers = {"Date": format_datetime(datetime.now(UTC))}
        self.calls: list[tuple[str, object]] = []
        self.timeouts: list[float] = []

    def get(self, url, *, params, timeout, headers):
        self.calls.append((url, params))
        self.timeouts.append(timeout)
        payload = self.orderbook_payload if url.endswith("/orderbook") else self.market_payload
        return FakeResponse(payload, url, headers=self.headers)


def market(**updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "ticker": "KXBTC15M-26AUG201215-00",
        "event_ticker": "KXBTC15M-26AUG201215",
        "title": "This title is deliberately ignored by mapping",
        "open_time": "2026-08-20T12:00:00Z",
        "close_time": "2026-08-20T12:15:00Z",
        "floor_strike": "68159.82000001",
        "yes_sub_title": "Target Price: $68159.82000001",
        "yes_bid_dollars": "0.5100",
        "yes_ask_dollars": "0.5200",
        "no_bid_dollars": "0.4800",
        "no_ask_dollars": "0.4900",
        "last_price_dollars": "0.5150",
        "volume_fp": "1234.567890123456789",
        "updated_time": datetime.now(UTC).isoformat(),
    }
    result.update(updates)
    return result


def provider(session: FakeSession, **settings: object) -> KalshiOfficialQuoteProvider:
    return KalshiOfficialQuoteProvider(Settings(**settings), session=session)


def test_all_ten_assets_have_explicit_audited_series() -> None:
    assert KALSHI_15MIN_SERIES == {
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


@pytest.mark.parametrize("asset", tuple(Asset))
def test_exact_mapping_algorithm_applies_to_every_target_asset(asset: Asset) -> None:
    series = KALSHI_15MIN_SERIES[asset]
    candidate = market(
        ticker=f"{series}-26AUG201215-00",
        event_ticker=f"{series}-26AUG201215",
    )
    target = replace(contract(), asset=asset, settlement=SETTLEMENT_SPECS[asset])

    mapping, raw_market, _ = provider(FakeSession({"markets": [candidate]})).map_contract(target)

    assert raw_market == candidate
    assert mapping.confidence is MappingConfidence.VERIFIED
    assert mapping.venue_series == series
    assert mapping.venue_ticker == candidate["ticker"]


def test_exact_window_target_and_series_create_verified_mapping_without_title_match() -> None:
    source = provider(FakeSession({"markets": [market()]}))

    mapping, raw_market, _ = source.map_contract(contract())

    assert raw_market is not None
    assert mapping.confidence is MappingConfidence.VERIFIED
    assert mapping.venue is Venue.KALSHI
    assert mapping.venue_series == "KXBTC15M"
    assert mapping.venue_ticker == "KXBTC15M-26AUG201215-00"
    assert mapping.matched_fields == (
        "official_asset_series",
        "start_time",
        "end_time",
        "target_price",
        "explicit_official_target",
        "unique_instrument",
    )


def test_quote_preserves_decimal_precision_and_explicit_sides() -> None:
    source = provider(FakeSession({"markets": [market()]}))

    quote = source.quote(contract())

    assert quote is not None
    assert str(quote.yes_bid) == "0.5100"
    assert str(quote.yes_ask) == "0.5200"
    assert str(quote.no_bid) == "0.4800"
    assert str(quote.no_ask) == "0.4900"
    assert str(quote.last_trade) == "0.5150"
    assert str(quote.volume) == "1234.567890123456789"
    assert str(quote.yes_bid_depth[0].quantity) == "12.340000"
    assert quote.source_timestamp_kind is SourceTimestampKind.HTTP_RESPONSE_DATE


def test_quote_model_rejects_unverified_mapping() -> None:
    quote = provider(FakeSession({"markets": [market()]})).quote(contract())

    assert quote is not None
    with pytest.raises(ValueError, match="verified venue mapping"):
        replace(quote, mapping_confidence=MappingConfidence.PARTIAL)


def test_missing_bid_ask_remain_none_and_are_not_derived() -> None:
    source = provider(
        FakeSession(
            {
                "markets": [
                    market(
                        yes_bid_dollars=None,
                        yes_ask_dollars=None,
                        no_bid_dollars=None,
                        no_ask_dollars=None,
                    )
                ]
            }
        )
    )

    quote = source.quote(contract())

    assert quote is not None
    assert (quote.yes_bid, quote.yes_ask, quote.no_bid, quote.no_ask) == (None, None, None, None)


def test_old_http_date_marks_quote_stale() -> None:
    session = FakeSession({"markets": [market()]})
    session.headers = {"Date": "Wed, 01 Jan 2020 00:00:00 GMT"}

    quote = provider(session, official_quote_max_source_age_seconds=1).quote(contract())

    assert quote is not None
    assert quote.freshness is FreshnessState.STALE


def test_market_updated_time_is_not_misclassified_as_quote_timestamp() -> None:
    quote = provider(FakeSession({"markets": [market()]})).quote(contract())

    assert quote is not None
    assert quote.source_timestamp_kind is SourceTimestampKind.HTTP_RESPONSE_DATE


def test_malformed_market_payload_fails_safely() -> None:
    with pytest.raises(KalshiPublicApiError, match="markets must be a list"):
        provider(FakeSession({"markets": "not-a-list"})).map_contract(contract())


def test_ticker_mismatch_is_rejected() -> None:
    with pytest.raises(KalshiPublicApiError, match="ticker mismatch"):
        provider(FakeSession({"markets": [market(ticker="WRONG-1")]})).map_contract(contract())


def test_nonmatching_target_is_partial_and_produces_no_quote() -> None:
    source = provider(
        FakeSession({"markets": [market(floor_strike="1.00", yes_sub_title="Target Price: $1.00")]})
    )

    mapping, raw_market, _ = source.map_contract(contract())

    assert mapping.confidence is MappingConfidence.PARTIAL
    assert mapping.venue_ticker is None
    assert raw_market is None
    assert source.quote(contract()) is None


def test_unrelated_window_without_target_does_not_break_exact_candidate() -> None:
    unrelated = market(
        ticker="KXBTC15M-26AUG201230-00",
        event_ticker="KXBTC15M-26AUG201230",
        open_time="2026-08-20T12:15:00Z",
        close_time="2026-08-20T12:30:00Z",
    )
    unrelated.pop("floor_strike")
    unrelated.pop("yes_sub_title")
    expected = market()
    source = provider(FakeSession({"markets": [unrelated, expected]}))

    mapping, raw_market, _ = source.map_contract(contract())

    assert mapping.confidence is MappingConfidence.VERIFIED
    assert raw_market == expected


def test_exact_window_without_target_still_fails_safely() -> None:
    candidate = market()
    candidate.pop("floor_strike")
    candidate.pop("yes_sub_title")

    with pytest.raises(KalshiPublicApiError, match="target"):
        provider(FakeSession({"markets": [candidate]})).map_contract(contract())


def test_explicit_target_preserves_precision_beyond_numeric_floor_strike() -> None:
    source = provider(
        FakeSession(
            {
                "markets": [
                    market(
                        floor_strike="68159.82",
                        yes_sub_title="Target Price: $68159.82000001",
                    )
                ]
            }
        )
    )

    mapping, raw_market, _ = source.map_contract(contract())

    assert raw_market is not None
    assert mapping.confidence is MappingConfidence.VERIFIED


def test_asset_outside_audited_scope_is_rejected() -> None:
    source = provider(FakeSession({"markets": []}))

    with pytest.raises(KalshiPublicApiError, match="unsupported"):
        source.series_for("ADA")  # type: ignore[arg-type]


def test_malformed_orderbook_fails_instead_of_emitting_incomplete_quote() -> None:
    source = provider(
        FakeSession(
            {"markets": [market()]},
            orderbook_payload={"orderbook_fp": {"yes_dollars": "bad", "no_dollars": []}},
        )
    )

    with pytest.raises(KalshiPublicApiError, match="yes_dollars must be a list"):
        source.quote(contract())


def test_retry_policy_covers_disconnects_rate_limits_and_server_errors() -> None:
    adapter = _retrying_session().get_adapter(KALSHI_PUBLIC_API_BASE_URL)

    assert adapter.max_retries.total == 4
    assert adapter.max_retries.connect == 4
    assert adapter.max_retries.read == 4
    assert {429, 500, 502, 503, 504} <= set(adapter.max_retries.status_forcelist)


def test_deadline_aware_transport_caps_request_timeout() -> None:
    session = FakeSession({"markets": []})
    source = KalshiOfficialQuoteProvider(
        Settings(request_timeout_seconds=10),
        session=session,
        deadline_monotonic=time.monotonic() + 0.5,
    )

    source.get_public("/markets")

    assert 0 < session.timeouts[0] <= 0.5
