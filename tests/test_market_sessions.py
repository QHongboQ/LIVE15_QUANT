from datetime import UTC, datetime, timedelta

import pytest

from live15_quant.market_sessions import (
    MarketDataState,
    MarketSessionPhase,
    market_data_state,
    market_session,
    open_intervals_for_asset,
    parse_official_schedule,
)
from live15_quant.models import Asset


@pytest.mark.parametrize("asset", (Asset.GOLD, Asset.SILVER))
def test_metals_friday_close_weekend_and_sunday_reopen_are_dst_aware(asset: Asset) -> None:
    # August New York is EDT: Friday 17:00 / Sunday 18:00 are 21:00 / 22:00 UTC.
    assert market_session(asset).is_open(datetime(2026, 8, 21, 20, 59, tzinfo=UTC))
    assert not market_session(asset).is_open(datetime(2026, 8, 21, 21, 0, tzinfo=UTC))
    assert not market_session(asset).is_open(datetime(2026, 8, 23, 21, 59, tzinfo=UTC))
    assert market_session(asset).is_open(datetime(2026, 8, 23, 22, 0, tzinfo=UTC))

    # January New York is EST, so the same local boundaries are one UTC hour later.
    assert market_session(asset).is_open(datetime(2026, 1, 9, 21, 59, tzinfo=UTC))
    assert not market_session(asset).is_open(datetime(2026, 1, 9, 22, 0, tzinfo=UTC))
    assert market_session(asset).is_open(datetime(2026, 1, 11, 23, 0, tzinfo=UTC))


def test_weekday_maintenance_windows_are_feed_specific() -> None:
    # Metals: 17:00-18:00 America/New_York (21:00-22:00 UTC in August).
    assert not market_session(Asset.GOLD).is_open(datetime(2026, 8, 19, 21, 30, tzinfo=UTC))
    assert market_session(Asset.GOLD).is_open(datetime(2026, 8, 19, 22, 0, tzinfo=UTC))
    # Current exact USOILSPOT metadata: 21:00-22:00 GMT, with no DST shift.
    assert not market_session(Asset.WTI_OIL).is_open(datetime(2026, 8, 19, 21, 30, tzinfo=UTC))
    assert market_session(Asset.WTI_OIL).is_open(datetime(2026, 8, 19, 22, 0, tzinfo=UTC))


def test_closed_price_is_non_live_and_reopen_requires_a_new_observation() -> None:
    friday = datetime(2026, 8, 21, 20, 59, 55, tzinfo=UTC)
    saturday = datetime(2026, 8, 22, 4, 0, tzinfo=UTC)
    reopened = datetime(2026, 8, 23, 22, 0, 5, tzinfo=UTC)
    assert (
        market_data_state(
            Asset.GOLD,
            checked_at=saturday,
            latest_received=friday,
            max_age=timedelta(seconds=15),
        )
        is MarketDataState.MARKET_CLOSED
    )
    assert (
        market_data_state(
            Asset.GOLD,
            checked_at=reopened,
            latest_received=friday,
            max_age=timedelta(days=3),
        )
        is MarketDataState.STALE
    )
    assert (
        market_data_state(
            Asset.GOLD,
            checked_at=reopened,
            latest_received=reopened,
            max_age=timedelta(seconds=15),
        )
        is MarketDataState.HEALTHY
    )


def test_open_market_without_updates_is_stale_and_explicit_failure_is_unavailable() -> None:
    checked = datetime(2026, 8, 19, 20, 0, tzinfo=UTC)
    assert (
        market_data_state(
            Asset.WTI_OIL,
            checked_at=checked,
            latest_received=checked - timedelta(seconds=16),
            max_age=timedelta(seconds=15),
        )
        is MarketDataState.STALE
    )
    assert (
        market_data_state(
            Asset.WTI_OIL,
            checked_at=checked,
            latest_received=checked,
            max_age=timedelta(seconds=15),
            source_available=False,
        )
        is MarketDataState.SOURCE_UNAVAILABLE
    )


def test_open_intervals_exclude_normal_weekend_closure() -> None:
    start = datetime(2026, 8, 21, 20, 59, 50, tzinfo=UTC)
    end = datetime(2026, 8, 23, 22, 0, 10, tzinfo=UTC)
    assert open_intervals_for_asset(Asset.GOLD, start, end) == (
        (start, datetime(2026, 8, 21, 21, 0, tzinfo=UTC)),
        (datetime(2026, 8, 23, 22, 0, tzinfo=UTC), end),
    )


def test_official_holiday_overrides_and_unknown_year_fail_safe() -> None:
    metals = market_session(Asset.GOLD)
    assert metals.phase(datetime(2026, 4, 3, 16, 0, tzinfo=UTC)) is MarketSessionPhase.CLOSED
    assert (
        market_data_state(
            Asset.GOLD,
            checked_at=datetime(2027, 4, 2, 16, 0, tzinfo=UTC),
            latest_received=datetime(2027, 4, 2, 15, 59, 59, tzinfo=UTC),
            max_age=timedelta(seconds=15),
        )
        is MarketDataState.SOURCE_UNAVAILABLE
    )


def test_official_schedule_parser_rejects_incomplete_or_malformed_metadata() -> None:
    with pytest.raises(ValueError):
        parse_official_schedule("America/New_York;O,O,O;", verified_years=frozenset({2026}))
    with pytest.raises(ValueError):
        parse_official_schedule(
            "America/New_York;O,O,O,O,O,C,C;0230/C",
            verified_years=frozenset({2026}),
        )
    with pytest.raises(ValueError):
        parse_official_schedule(
            "America/New_York;0000-2430,O,O,O,O,C,C;",
            verified_years=frozenset({2026}),
        )
    with pytest.raises(ValueError):
        parse_official_schedule(
            "America/New_York;O,O,O,O,O,C,C;0101/C,0101/O",
            verified_years=frozenset({2026}),
        )
