from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from live15_quant.backfill import KalshiBackfillService
from live15_quant.kalshi_lifecycle import (
    BackfillPage,
    KalshiLifecycle,
    KalshiLifecycleStateMachine,
    KalshiNativeMarketProvider,
    KalshiResult,
)
from live15_quant.models import (
    Asset,
    ExecutabilityClassification,
    FreshnessState,
    KalshiNativeQuote,
    OrderBookLevel,
    SourceTimestampKind,
)
from live15_quant.providers.kalshi import KALSHI_15MIN_SERIES, KalshiPublicApiError
from live15_quant.storage import RecorderStorageError, RecorderStore

NOW = datetime(2026, 8, 20, 12, 7, tzinfo=UTC)
FINALIZED_FETCH = NOW.replace(minute=16)


def raw_market(
    asset: Asset = Asset.BTC,
    *,
    start: datetime = datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    status: str = "active",
    target: str = "68,159.82000001",
    suffix: str = "00",
    result: str = "",
) -> dict[str, Any]:
    series = KALSHI_15MIN_SERIES[asset]
    event = f"{series}-{start:%y%b%d%H%M}".upper()
    return {
        "ticker": f"{event}-{suffix}",
        "event_ticker": event,
        "open_time": start.isoformat().replace("+00:00", "Z"),
        "close_time": (start + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
        "yes_sub_title": f"Target Price: ${target}",
        "floor_strike": target.replace(",", ""),
        "status": status,
        "result": result,
        "settlement_ts": (
            (start + timedelta(minutes=15, seconds=24)).isoformat().replace("+00:00", "Z")
            if status == "finalized"
            else None
        ),
        "settlement_value_dollars": (
            "1.0000" if result == "yes" else "0.0000" if result == "no" else None
        ),
        "expiration_value": "68160.12000000" if status == "finalized" else "",
        "settlement_timer_seconds": 15,
        "rules_primary": "Official primary rule",
        "rules_secondary": "Official secondary rule",
    }


class FakePublicClient:
    base_url = "https://external-api.kalshi.com/trade-api/v2"

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_public(self, path: str, params: dict[str, object] | None = None):
        self.calls.append((path, params or {}))
        page = self.pages.pop(0)
        return page, {}, f"{self.base_url}{path}"


def provider(*pages: dict[str, Any]) -> KalshiNativeMarketProvider:
    return KalshiNativeMarketProvider(FakePublicClient(list(pages)))  # type: ignore[arg-type]


def quote(ticker: str, event_ticker: str, received: datetime) -> KalshiNativeQuote:
    return KalshiNativeQuote(
        asset=Asset.BTC,
        series="KXBTC15M",
        ticker=ticker,
        event_ticker=event_ticker,
        source_timestamp=received,
        source_timestamp_kind=SourceTimestampKind.HTTP_RESPONSE_DATE,
        received_timestamp=received,
        yes_bid=Decimal("0.5000"),
        yes_ask=Decimal("0.5100"),
        no_bid=Decimal("0.4900"),
        no_ask=Decimal("0.5000"),
        last_trade=Decimal("0.5050"),
        volume=Decimal("10.0000"),
        yes_bid_depth=(OrderBookLevel(Decimal("0.5000"), Decimal("1.00")),),
        no_bid_depth=(OrderBookLevel(Decimal("0.4900"), Decimal("1.00")),),
        source="https://external-api.kalshi.com/trade-api/v2/markets/test",
        freshness=FreshnessState.FRESH,
        executability=ExecutabilityClassification.OFFICIAL_VENUE_ORDER_BOOK,
        evidence_urls=("https://docs.kalshi.com/",),
    )


@pytest.mark.parametrize("asset", tuple(KALSHI_15MIN_SERIES))
def test_all_ten_exact_series_parse_without_robinhood(asset: Asset) -> None:
    parsed = provider().parse_market(asset, raw_market(asset), NOW)

    assert parsed.series == KALSHI_15MIN_SERIES[asset]
    assert parsed.lifecycle is KalshiLifecycle.OPEN
    assert parsed.target == Decimal("68159.82000001")


def test_native_discovery_classifies_previous_current_next_and_future() -> None:
    starts = [NOW.replace(minute=0), NOW.replace(minute=15), NOW.replace(minute=30)]
    previous = raw_market(start=starts[0] - timedelta(minutes=15), status="finalized", result="yes")
    current = raw_market(start=starts[0])
    following = raw_market(start=starts[1], status="initialized", target="68160.1")
    future = raw_market(start=starts[2], status="initialized", target="68161.2")
    discovery = provider(
        {"markets": [future, current, previous, following], "cursor": ""}
    ).discover(Asset.BTC, NOW)

    assert discovery.previous is not None and discovery.previous.ticker == previous["ticker"]
    assert discovery.current is not None and discovery.current.ticker == current["ticker"]
    assert discovery.next is not None and discovery.next.ticker == following["ticker"]
    assert [item.ticker for item in discovery.future] == [future["ticker"]]

    with pytest.raises(ValueError, match="timezone-aware"):
        provider({"markets": []}).discover(Asset.BTC, NOW.replace(tzinfo=None))


def test_default_discovery_timestamp_is_local_receive_time() -> None:
    received = NOW + timedelta(seconds=2)
    times = iter((NOW, received))
    reader = KalshiNativeMarketProvider(
        FakePublicClient([{"markets": [raw_market()], "cursor": ""}]),  # type: ignore[arg-type]
        now=lambda: next(times),
    )

    discovery = reader.discover(Asset.BTC)

    assert discovery.fetched_timestamp == received
    assert discovery.current is not None
    assert discovery.current.fetched_timestamp == received


def test_exact_market_followup_validates_ticker_and_historical_path() -> None:
    raw = raw_market(status="finalized", result="yes")
    reader = provider({"market": raw})

    market = reader.get_market(Asset.BTC, raw["ticker"], historical=True)

    assert market.lifecycle is KalshiLifecycle.SETTLED_YES
    assert reader._client.calls[0][0] == f"/historical/markets/{raw['ticker']}"
    with pytest.raises(KalshiPublicApiError, match="exact series"):
        reader.get_market(Asset.ETH, raw["ticker"])


def test_finalized_transition_exposes_truth_only_on_terminal_observation() -> None:
    finalized = provider().parse_market(
        Asset.BTC, raw_market(status="finalized", result="yes"), FINALIZED_FETCH
    )

    observations = KalshiLifecycleStateMachine.observations(KalshiLifecycle.OPEN, finalized)

    assert [item.lifecycle for item in observations] == [
        KalshiLifecycle.CLOSED,
        KalshiLifecycle.SETTLEMENT_PENDING,
        KalshiLifecycle.SETTLED_YES,
    ]
    assert [item.settlement is not None for item in observations] == [False, False, True]
    assert observations[0].determination_result is None


def test_missing_or_malformed_target_fails_closed() -> None:
    malformed = raw_market()
    malformed["yes_sub_title"] = "Target price: TBD"
    malformed["floor_strike"] = None
    discovery = provider({"markets": [malformed], "cursor": ""}).discover(Asset.BTC, NOW)

    assert discovery.current is None
    assert discovery.rejected_tickers == (malformed["ticker"],)
    with pytest.raises(KalshiPublicApiError, match="target"):
        provider().parse_market(Asset.BTC, malformed, NOW)

    malformed_timer = raw_market()
    malformed_timer["settlement_timer_seconds"] = Decimal("1.5")
    with pytest.raises(KalshiPublicApiError, match="timer"):
        provider().parse_market(Asset.BTC, malformed_timer, NOW)


def test_duplicate_and_conflicting_candidates_fail_closed() -> None:
    candidate = raw_market()
    with pytest.raises(KalshiPublicApiError, match="duplicate"):
        provider({"markets": [candidate, candidate], "cursor": ""}).discover(Asset.BTC, NOW)

    conflict = raw_market(suffix="01", target="68160.0")
    with pytest.raises(KalshiPublicApiError, match="conflicting"):
        provider({"markets": [candidate, conflict], "cursor": ""}).discover(Asset.BTC, NOW)


@pytest.mark.parametrize(
    ("status", "result", "lifecycle"),
    [
        ("initialized", "", KalshiLifecycle.UPCOMING),
        ("active", "", KalshiLifecycle.OPEN),
        ("inactive", "", KalshiLifecycle.PAUSED),
        ("closed", "", KalshiLifecycle.CLOSED),
        ("determined", "yes", KalshiLifecycle.SETTLEMENT_PENDING),
        ("finalized", "yes", KalshiLifecycle.SETTLED_YES),
        ("finalized", "no", KalshiLifecycle.SETTLED_NO),
    ],
)
def test_official_lifecycle_and_settlement_mapping(
    status: str, result: str, lifecycle: KalshiLifecycle
) -> None:
    fetched = FINALIZED_FETCH if status == "finalized" else NOW
    parsed = provider().parse_market(Asset.BTC, raw_market(status=status, result=result), fetched)

    assert parsed.lifecycle is lifecycle
    if status == "finalized":
        assert parsed.settlement is not None
        assert parsed.settlement.result is KalshiResult(result)
    else:
        assert parsed.settlement is None


def test_finalized_market_cannot_be_observed_before_official_settlement_time() -> None:
    with pytest.raises(ValueError, match="before its official timestamp"):
        provider().parse_market(
            Asset.BTC,
            raw_market(status="finalized", result="yes"),
            NOW,
        )


def test_state_machine_emits_closed_before_pending_and_rejects_result_flip() -> None:
    assert KalshiLifecycleStateMachine.transition(
        KalshiLifecycle.OPEN, KalshiLifecycle.SETTLEMENT_PENDING
    ) == (KalshiLifecycle.CLOSED, KalshiLifecycle.SETTLEMENT_PENDING)
    assert KalshiLifecycleStateMachine.transition(
        KalshiLifecycle.UPCOMING, KalshiLifecycle.SETTLED_NO
    ) == (
        KalshiLifecycle.CLOSED,
        KalshiLifecycle.SETTLEMENT_PENDING,
        KalshiLifecycle.SETTLED_NO,
    )
    with pytest.raises(KalshiPublicApiError, match="invalid lifecycle"):
        KalshiLifecycleStateMachine.transition(
            KalshiLifecycle.SETTLED_YES, KalshiLifecycle.SETTLED_NO
        )


def test_state_machine_classifies_only_safe_stale_regressions() -> None:
    assert KalshiLifecycleStateMachine.is_stale_regression(
        KalshiLifecycle.SETTLEMENT_PENDING, KalshiLifecycle.CLOSED
    )
    for settled in (KalshiLifecycle.SETTLED_YES, KalshiLifecycle.SETTLED_NO):
        assert KalshiLifecycleStateMachine.is_stale_regression(settled, KalshiLifecycle.CLOSED)
        assert KalshiLifecycleStateMachine.is_stale_regression(
            settled, KalshiLifecycle.SETTLEMENT_PENDING
        )
    assert not KalshiLifecycleStateMachine.is_stale_regression(
        KalshiLifecycle.SETTLED_YES, KalshiLifecycle.SETTLED_NO
    )
    assert not KalshiLifecycleStateMachine.is_stale_regression(
        KalshiLifecycle.OPEN, KalshiLifecycle.UPCOMING
    )


def test_pause_reactivation_and_pause_to_settlement_are_explicit() -> None:
    assert KalshiLifecycleStateMachine.transition(KalshiLifecycle.OPEN, KalshiLifecycle.PAUSED) == (
        KalshiLifecycle.PAUSED,
    )
    assert KalshiLifecycleStateMachine.transition(KalshiLifecycle.PAUSED, KalshiLifecycle.OPEN) == (
        KalshiLifecycle.OPEN,
    )
    assert KalshiLifecycleStateMachine.transition(
        KalshiLifecycle.PAUSED, KalshiLifecycle.SETTLEMENT_PENDING
    ) == (KalshiLifecycle.CLOSED, KalshiLifecycle.SETTLEMENT_PENDING)


def test_only_unpublished_target_is_rejected_while_malformed_payload_fails() -> None:
    unavailable = raw_market()
    unavailable["yes_sub_title"] = "Target Price: TBD"
    unavailable["floor_strike"] = None
    discovery = provider({"markets": [unavailable], "cursor": ""}).discover(Asset.BTC, NOW)
    assert discovery.rejected_tickers == (unavailable["ticker"],)

    malformed = raw_market()
    malformed["open_time"] = "not-a-timestamp"
    with pytest.raises(KalshiPublicApiError, match="open_time"):
        provider({"markets": [malformed], "cursor": ""}).discover(Asset.BTC, NOW)

    unknown = raw_market(status="mystery")
    with pytest.raises(KalshiPublicApiError, match="unsupported official market status"):
        provider({"markets": [unknown], "cursor": ""}).discover(Asset.BTC, NOW)

    inconsistent = raw_market(status="active", result="yes")
    with pytest.raises(KalshiPublicApiError, match="unexpectedly contains a result"):
        provider({"markets": [inconsistent], "cursor": ""}).discover(Asset.BTC, NOW)


def test_backfill_paginates_sorts_filters_and_detects_cursor_cycle() -> None:
    first = raw_market(start=NOW.replace(minute=0), status="finalized", result="yes")
    second = raw_market(
        start=NOW.replace(minute=0) - timedelta(minutes=15), status="finalized", result="no"
    )
    reader = provider(
        {"markets": [first], "cursor": "next"},
        {"markets": [second], "cursor": ""},
    )
    pages = list(
        reader.backfill_pages(
            Asset.BTC,
            start=NOW - timedelta(hours=1),
            end=NOW + timedelta(hours=1),
            historical=True,
        )
    )

    assert len(pages) == 2
    assert pages[0].next_cursor == "next"
    assert pages[1].cursor_used == "next"
    assert (
        pages[0].markets[0].source_url.endswith(f"/historical/markets/{pages[0].markets[0].ticker}")
    )
    assert pages[0].markets[0].settlement is not None
    assert "/historical/markets/" in pages[0].markets[0].settlement.official_source
    with pytest.raises(KalshiPublicApiError, match="cursor cycle"):
        list(
            provider(
                {"markets": [], "cursor": "again"},
                {"markets": [], "cursor": "again"},
            ).backfill_pages(
                Asset.BTC,
                start=NOW - timedelta(hours=1),
                end=NOW,
                historical=True,
            )
        )


def test_settlement_is_immutable_restart_safe_and_replay_deterministic(tmp_path) -> None:
    path = tmp_path / "native.sqlite3"
    market = provider().parse_market(
        Asset.BTC, raw_market(status="finalized", result="yes"), FINALIZED_FETCH
    )
    assert market.settlement is not None
    with RecorderStore(path) as store:
        assert store.append_kalshi_market(market)
        assert not store.append_kalshi_settlement(market.settlement)
        archived_evidence = replace(
            market.settlement,
            official_source=market.settlement.official_source.replace(
                "/markets/", "/historical/markets/"
            ),
            fetched_timestamp=NOW + timedelta(days=30),
        )
        assert not store.append_kalshi_settlement(archived_evidence)
    with RecorderStore(path) as store:
        replay = tuple(store.replay_kalshi_settlements(series="KXBTC15M"))
        assert len(replay) == 1 and replay[0].result is KalshiResult.YES
        conflict = replace(
            market.settlement,
            result=KalshiResult.NO,
            settlement_value=Decimal("0.0000"),
        )
        with pytest.raises(RecorderStorageError, match="conflicting official settlement"):
            store.append_kalshi_settlement(conflict)
        assert store.count("kalshi_settlement_conflicts") == 1


@pytest.mark.parametrize(
    ("initial_expiration_value", "later_expiration_value"),
    (
        (None, "68160.12000000"),
        ("68160.12000000", "68160.13000000"),
        ("68160.12000000", None),
    ),
)
def test_expiration_value_is_non_terminal_metadata_and_does_not_replace_stored_truth(
    tmp_path, initial_expiration_value, later_expiration_value
) -> None:
    market = provider().parse_market(
        Asset.BTC, raw_market(status="finalized", result="yes"), FINALIZED_FETCH
    )
    assert market.settlement is not None
    initial = replace(market.settlement, expiration_value=initial_expiration_value)
    later_observation = replace(
        initial,
        expiration_value=later_expiration_value,
        fetched_timestamp=initial.fetched_timestamp + timedelta(seconds=15),
    )

    with RecorderStore(tmp_path / "late-expiration-value.sqlite3") as store:
        assert store.append_kalshi_settlement(initial)
        stored_before = store._connection.execute(
            "SELECT expiration_value,content_hash FROM kalshi_settlements WHERE ticker=?",
            (initial.ticker,),
        ).fetchone()
        assert not store.append_kalshi_settlement(later_observation)
        stored_after = store._connection.execute(
            "SELECT expiration_value,content_hash FROM kalshi_settlements WHERE ticker=?",
            (initial.ticker,),
        ).fetchone()
        assert tuple(stored_after) == tuple(stored_before)
        assert store.count("kalshi_settlement_conflicts") == 0


@pytest.mark.parametrize(
    "changes",
    (
        {"target": Decimal("68161.00")},
        {"settlement_timestamp": datetime(2026, 8, 20, 12, 15, 25, tzinfo=UTC)},
        {"result": KalshiResult.NO, "settlement_value": Decimal("0.0000")},
    ),
)
def test_material_settlement_truth_changes_remain_fail_closed(tmp_path, changes) -> None:
    market = provider().parse_market(
        Asset.BTC, raw_market(status="finalized", result="yes"), FINALIZED_FETCH
    )
    assert market.settlement is not None
    conflicting = replace(market.settlement, **changes)

    with RecorderStore(tmp_path / "material-settlement-conflict.sqlite3") as store:
        assert store.append_kalshi_settlement(market.settlement)
        with pytest.raises(RecorderStorageError, match="conflicting official settlement"):
            store.append_kalshi_settlement(conflicting)
        assert store.count("kalshi_settlement_conflicts") == 1


def test_binary_settlement_value_cannot_disagree_with_result() -> None:
    market = provider().parse_market(
        Asset.BTC, raw_market(status="finalized", result="yes"), FINALIZED_FETCH
    )
    assert market.settlement is not None

    with pytest.raises(ValueError, match="binary settlement value conflicts"):
        replace(market.settlement, settlement_value=Decimal("0.0000"))


def test_training_join_has_no_future_or_label_leakage(tmp_path) -> None:
    market = provider().parse_market(
        Asset.BTC, raw_market(status="finalized", result="yes"), FINALIZED_FETCH
    )
    assert market.settlement is not None
    pre = market.window_start + timedelta(minutes=5)
    after = market.window_start + timedelta(minutes=10)
    observed_market = replace(
        market,
        lifecycle=KalshiLifecycle.OPEN,
        official_status="active",
        fetched_timestamp=market.window_start + timedelta(seconds=1),
        determination_result=None,
        settlement=None,
    )
    with RecorderStore(tmp_path / "labels.sqlite3") as store:
        store.append_kalshi_market(observed_market)
        determined_market = replace(
            market,
            lifecycle=KalshiLifecycle.SETTLEMENT_PENDING,
            official_status="determined",
            fetched_timestamp=pre - timedelta(seconds=1),
            settlement=None,
        )
        store.append_kalshi_market(determined_market)
        store.append_kalshi_quote(
            replace(
                quote(
                    market.ticker, market.event_ticker, market.window_start - timedelta(seconds=1)
                ),
                yes_bid=Decimal("0.4800"),
                yes_ask=Decimal("0.4900"),
            )
        )
        store.append_kalshi_quote(quote(market.ticker, market.event_ticker, pre))
        store.append_kalshi_quote(quote(market.ticker, market.event_ticker, after))
        store.append_kalshi_quote(
            replace(
                quote(market.ticker, market.event_ticker, market.window_end),
                yes_bid=Decimal("0.5200"),
                yes_ask=Decimal("0.5300"),
            )
        )
        store.append_kalshi_settlement(market.settlement)
        example = store.join_training_label(market.ticker, pre)

        assert len(example.observations) == 1
        assert example.observations[0].received_timestamp == pre
        assert example.label.result is KalshiResult.YES
        assert not hasattr(example.market, "result")
        assert not hasattr(example.market, "determination_result")
        assert example.market.lifecycle is KalshiLifecycle.OPEN
        assert not hasattr(example.observations[0], "settlement_timestamp")
        with pytest.raises(RecorderStorageError, match="pre-settlement"):
            store.join_training_label(market.ticker, market.settlement.settlement_timestamp)
        with pytest.raises(RecorderStorageError, match="pre-settlement"):
            store.join_training_label(market.ticker, market.window_end)
        with pytest.raises(RecorderStorageError, match="pre-settlement"):
            store.join_training_label(market.ticker, market.window_start - timedelta(seconds=1))


def test_native_quote_storage_preserves_precision_and_fresh_observation_time(tmp_path) -> None:
    first = quote("KXBTC15M-26AUG201200-00", "KXBTC15M-26AUG201200", NOW)
    precise = replace(
        first,
        yes_bid=Decimal("0.500000000000000001"),
        yes_ask=Decimal("0.510000000000000001"),
        received_timestamp=NOW + timedelta(seconds=2),
    )
    with RecorderStore(tmp_path / "native-quotes.sqlite3") as store:
        assert store.append_kalshi_quote(first) is True
        duplicate = replace(first, received_timestamp=NOW + timedelta(seconds=1))
        assert store.append_kalshi_quote(duplicate) is True
        assert store.append_kalshi_quote(duplicate) is False
        assert store.append_kalshi_quote(precise) is True
        replayed = list(store.replay_kalshi_quotes(first.ticker))

    assert [record.received_timestamp for record in replayed] == [
        NOW,
        NOW + timedelta(seconds=1),
        NOW + timedelta(seconds=2),
    ]
    assert replayed[-1].yes_bid == Decimal("0.500000000000000001")


def test_native_quote_storage_rejects_conflicting_same_receive_timestamp(tmp_path) -> None:
    first = quote("KXBTC15M-26AUG201200-00", "KXBTC15M-26AUG201200", NOW)
    conflicting = replace(first, yes_bid=Decimal("0.49"))

    with RecorderStore(tmp_path / "native-quote-conflict.sqlite3") as store:
        assert store.append_kalshi_quote(first) is True
        with pytest.raises(RecorderStorageError, match="conflicting Kalshi quote fact"):
            store.append_kalshi_quote(conflicting)

        replayed = list(store.replay_kalshi_quotes(first.ticker))
        assert len(replayed) == 1
        assert replayed[0].yes_bid == first.yes_bid
    assert replayed[-1].series == "KXBTC15M"


def test_native_quote_requires_explicit_source_timestamp_semantics() -> None:
    with pytest.raises(ValueError, match="classified unavailable"):
        replace(
            quote("KXBTC15M-26AUG201200-00", "KXBTC15M-26AUG201200", NOW),
            source_timestamp=None,
        )


def test_backfill_resume_state_uses_exact_series_path_and_range(tmp_path) -> None:
    start, end = NOW - timedelta(days=1), NOW
    with RecorderStore(tmp_path / "resume.sqlite3") as store:
        store.save_backfill_state(
            series="KXBTC15M",
            source_path="/historical/markets",
            start=start,
            end=end,
            next_cursor="cursor-2",
            complete=False,
            updated_at=NOW,
        )

        assert store.load_backfill_cursor(
            series="KXBTC15M", source_path="/historical/markets", start=start, end=end
        ) == ("cursor-2", False)


def test_backfill_service_resumes_after_page_boundary_without_duplicates(tmp_path) -> None:
    first_market = provider().parse_market(
        Asset.BTC, raw_market(status="finalized", result="yes"), FINALIZED_FETCH
    )
    second_market = provider().parse_market(
        Asset.BTC,
        raw_market(
            start=NOW.replace(minute=0) - timedelta(minutes=15),
            status="finalized",
            result="no",
        ),
        NOW,
    )

    class FakeBackfill:
        def backfill_pages(self, asset, *, start, end, historical, cursor=None):
            del start, end, historical
            if cursor is None:
                yield BackfillPage(asset, "/historical/markets", None, "page-2", (first_market,))
            else:
                assert cursor == "page-2"
                yield BackfillPage(asset, "/historical/markets", "page-2", None, (second_market,))

    path = tmp_path / "backfill.sqlite3"
    start, end = NOW - timedelta(days=1), NOW + timedelta(days=1)
    with RecorderStore(path) as store:
        first = KalshiBackfillService(FakeBackfill(), store).run(  # type: ignore[arg-type]
            Asset.BTC, start=start, end=end, historical=True, max_pages=1
        )
        assert not first.complete and first.next_cursor == "page-2"
    with RecorderStore(path) as store:
        resumed = KalshiBackfillService(FakeBackfill(), store).run(  # type: ignore[arg-type]
            Asset.BTC, start=start, end=end, historical=True
        )
        assert resumed.complete
        assert store.count("kalshi_settlements") == 2
        replayed = tuple(store.replay_kalshi_settlements())
        assert len(replayed) == 2
        assert [item.window_start for item in replayed] == sorted(
            item.window_start for item in replayed
        )


def test_historical_restart_replay_deduplicates_semantic_lifecycle(tmp_path) -> None:
    path = tmp_path / "historical-dedup.sqlite3"
    second_fetch = NOW + timedelta(hours=1)
    first = provider().parse_market(
        Asset.BTC,
        raw_market(status="finalized", result="yes"),
        FINALIZED_FETCH,
        source_path="/historical/markets",
    )
    repeated = replace(first, fetched_timestamp=second_fetch)
    with RecorderStore(path) as store:
        assert store.append_kalshi_market(first)
    with RecorderStore(path) as store:
        assert not store.append_kalshi_market(repeated)
        assert len(tuple(store.replay_kalshi_markets(first.ticker))) == 1
        assert store.count("kalshi_settlements") == 1


def test_training_join_rejects_ticker_bound_metadata_mismatch(tmp_path) -> None:
    market = provider().parse_market(
        Asset.BTC, raw_market(status="finalized", result="yes"), FINALIZED_FETCH
    )
    assert market.settlement is not None
    observed = replace(
        market,
        lifecycle=KalshiLifecycle.OPEN,
        official_status="active",
        fetched_timestamp=market.window_start + timedelta(seconds=1),
        determination_result=None,
        settlement=None,
    )
    decision = market.window_start + timedelta(minutes=5)
    with RecorderStore(tmp_path / "join-mismatch.sqlite3") as store:
        store.append_kalshi_market(observed)
        store.append_kalshi_quote(quote(market.ticker, market.event_ticker, decision))
        store.append_kalshi_settlement(market.settlement)
        store._connection.execute(
            "UPDATE kalshi_market_lifecycle SET target='1.0' WHERE ticker=?",
            (market.ticker,),
        )
        store._connection.commit()
        with pytest.raises(RecorderStorageError, match="metadata does not match"):
            store.join_training_label(market.ticker, decision)


def test_training_join_rejects_quote_asset_mismatch_even_with_same_ticker(tmp_path) -> None:
    market = provider().parse_market(
        Asset.BTC, raw_market(status="finalized", result="yes"), FINALIZED_FETCH
    )
    assert market.settlement is not None
    observed = replace(
        market,
        lifecycle=KalshiLifecycle.OPEN,
        official_status="active",
        fetched_timestamp=market.window_start + timedelta(seconds=1),
        determination_result=None,
        settlement=None,
    )
    decision = market.window_start + timedelta(minutes=5)
    with RecorderStore(tmp_path / "quote-join-mismatch.sqlite3") as store:
        store.append_kalshi_market(observed)
        store.append_kalshi_quote(quote(market.ticker, market.event_ticker, decision))
        store.append_kalshi_settlement(market.settlement)
        store._connection.execute(
            "UPDATE kalshi_prediction_quotes SET asset='ETH' WHERE ticker=?",
            (market.ticker,),
        )
        store._connection.commit()
        with pytest.raises(RecorderStorageError, match="quote does not match"):
            store.join_training_label(market.ticker, decision)
