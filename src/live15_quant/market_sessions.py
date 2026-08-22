"""Official session-aware availability for non-24/7 predictive inputs.

The schedules are feed-specific.  Gold and Silver follow the Pyth metals
schedule in America/New_York.  WTI uses the exact Pyth USOILSPOT (FXCM CFD)
schedule, which is documented in GMT rather than the generic CME WTI hours.
Holiday overrides are deliberately not guessed; they can be supplied from
official feed metadata when available.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from types import MappingProxyType
from zoneinfo import ZoneInfo

from live15_quant.models import Asset


class MarketDataState(StrEnum):
    HEALTHY = "healthy"
    MARKET_CLOSED = "market_closed"
    STALE = "stale"
    SOURCE_UNAVAILABLE = "source_unavailable"


class MarketSessionPhase(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DailyInterval:
    start: time
    end: time | None

    def __post_init__(self) -> None:
        if self.end is not None and self.start >= self.end:
            raise ValueError("session intervals must be ordered within one local date")


@dataclass(frozen=True, slots=True)
class MarketSessionCalendar:
    timezone: ZoneInfo
    weekly_intervals: Mapping[int, tuple[DailyInterval, ...]]
    holiday_intervals: Mapping[tuple[int, int], tuple[DailyInterval, ...]]
    verified_years: frozenset[int]

    def intervals_for_date(self, local_date: date) -> tuple[tuple[datetime, datetime], ...] | None:
        if local_date.year not in self.verified_years:
            return None
        intervals = self.holiday_intervals.get(
            (local_date.month, local_date.day), self.weekly_intervals.get(local_date.weekday(), ())
        )
        if not intervals:
            return ()
        return tuple(
            (
                datetime.combine(local_date, interval.start, self.timezone),
                (
                    datetime.combine(local_date, interval.end, self.timezone)
                    if interval.end is not None
                    else datetime.combine(local_date + timedelta(days=1), time.min, self.timezone)
                ),
            )
            for interval in intervals
        )

    def phase(self, observed_at: datetime) -> MarketSessionPhase:
        observed = _aware(observed_at).astimezone(self.timezone)
        intervals = self.intervals_for_date(observed.date())
        if intervals is None:
            return MarketSessionPhase.UNKNOWN
        if any(start <= observed < end for start, end in intervals):
            return MarketSessionPhase.OPEN
        return MarketSessionPhase.CLOSED

    def is_open(self, observed_at: datetime) -> bool:
        return self.phase(observed_at) is MarketSessionPhase.OPEN

    def current_session_started_at(self, observed_at: datetime) -> datetime | None:
        observed = _aware(observed_at).astimezone(self.timezone)
        for start, end in self.intervals_for_date(observed.date()) or ():
            if start <= observed < end:
                return start.astimezone(UTC)
        return None

    def open_intervals(
        self, start: datetime, end: datetime
    ) -> tuple[tuple[datetime, datetime], ...]:
        start_utc = _aware(start).astimezone(UTC)
        end_utc = _aware(end).astimezone(UTC)
        if end_utc <= start_utc:
            return ()
        local_start = start_utc.astimezone(self.timezone).date()
        local_end = end_utc.astimezone(self.timezone).date()
        intervals: list[tuple[datetime, datetime]] = []
        current = local_start
        while current <= local_end:
            for local_open, local_close in self.intervals_for_date(current) or ():
                opened = max(start_utc, local_open.astimezone(UTC))
                closed = min(end_utc, local_close.astimezone(UTC))
                if opened < closed:
                    intervals.append((opened, closed))
            current += timedelta(days=1)
        return tuple(intervals)


def _parse_day_schedule(value: str) -> tuple[DailyInterval, ...]:
    if value == "C":
        return ()
    if value == "O":
        return (DailyInterval(time.min, None),)
    intervals: list[DailyInterval] = []
    for item in value.split("&"):
        pieces = item.split("-")
        if len(pieces) != 2:
            raise ValueError("malformed official market day schedule")
        start_text, end_text = pieces
        if len(start_text) != 4 or len(end_text) != 4:
            raise ValueError("market schedule times must use HHMM")
        start_hour, start_minute = int(start_text[:2]), int(start_text[2:])
        end_hour, end_minute = int(end_text[:2]), int(end_text[2:])
        if (
            start_hour >= 24
            or start_minute >= 60
            or end_minute >= 60
            or end_hour > 24
            or (end_hour == 24 and end_minute != 0)
        ):
            raise ValueError("market schedule time is out of range")
        end = None if end_hour == 24 else time(end_hour, end_minute)
        intervals.append(DailyInterval(time(start_hour, start_minute), end))
    return tuple(intervals)


def parse_official_schedule(value: str, *, verified_years: frozenset[int]) -> MarketSessionCalendar:
    """Parse Pyth's documented Timezone;WeeklySchedule;Holidays format."""

    parts = value.split(";")
    if len(parts) != 3 or not verified_years:
        raise ValueError("official market schedule must include timezone, week, and year scope")
    timezone_name, weekly_text, holiday_text = parts
    weekly_parts = weekly_text.split(",")
    if len(weekly_parts) != 7:
        raise ValueError("official weekly schedule must contain Monday through Sunday")
    weekly = MappingProxyType(
        {weekday: _parse_day_schedule(day) for weekday, day in enumerate(weekly_parts)}
    )
    holidays: dict[tuple[int, int], tuple[DailyInterval, ...]] = {}
    if holiday_text:
        for item in holiday_text.split(","):
            date_text, separator, schedule_text = item.partition("/")
            if separator != "/" or len(date_text) != 4:
                raise ValueError("malformed official holiday schedule")
            month, day = int(date_text[:2]), int(date_text[2:])
            date(2000, month, day)
            holiday = (month, day)
            if holiday in holidays:
                raise ValueError("duplicate official holiday schedule")
            holidays[holiday] = _parse_day_schedule(schedule_text)
    return MarketSessionCalendar(
        timezone=ZoneInfo(timezone_name),
        weekly_intervals=weekly,
        holiday_intervals=MappingProxyType(holidays),
        verified_years=verified_years,
    )


_METALS = parse_official_schedule(
    "America/New_York;"
    "0000-1700&1800-2400,0000-1700&1800-2400,0000-1700&1800-2400,"
    "0000-1700&1800-2400,0000-1700,C,1800-2400;"
    "0119/0000-1430&1800-2400,0216/0000-1430&1800-2400,0402/0000-1700,"
    "0403/C,0525/0000-1430&1800-2400,0619/0000-1300,0703/0000-1300,"
    "0907/0000-1430&1800-2400,1126/0000-1430&1800-2400,1127/0000-1445,"
    "1224/0000-1345,1225/C,1231/0000-1700,0101/C",
    verified_years=frozenset({2026}),
)

_WTI_USOILSPOT = parse_official_schedule(
    "GMT;0000-2100&2200-2400,0000-2100&2200-2400,0000-2100&2200-2400,"
    "0000-2100&2200-2400,0000-2045,C,2200-2400;"
    "0101/C,0102/0700-2045,0119/0000-1930&2300-2400",
    verified_years=frozenset({2026}),
)

MARKET_SESSION_BY_ASSET: Mapping[Asset, MarketSessionCalendar] = MappingProxyType(
    {
        Asset.GOLD: _METALS,
        Asset.SILVER: _METALS,
        Asset.WTI_OIL: _WTI_USOILSPOT,
    }
)


def market_session(asset: Asset) -> MarketSessionCalendar | None:
    return MARKET_SESSION_BY_ASSET.get(asset)


def market_data_state(
    asset: Asset,
    *,
    checked_at: datetime,
    latest_received: datetime | None,
    max_age: timedelta,
    source_available: bool = True,
) -> MarketDataState:
    checked = _aware(checked_at).astimezone(UTC)
    if max_age <= timedelta(0):
        raise ValueError("market-data max age must be positive")
    calendar = market_session(asset)
    if calendar is not None:
        phase = calendar.phase(checked)
        if phase is MarketSessionPhase.UNKNOWN:
            return MarketDataState.SOURCE_UNAVAILABLE
        if phase is MarketSessionPhase.CLOSED:
            return MarketDataState.MARKET_CLOSED
    if not source_available or latest_received is None:
        return MarketDataState.SOURCE_UNAVAILABLE
    received = _aware(latest_received).astimezone(UTC)
    if received > checked:
        return MarketDataState.SOURCE_UNAVAILABLE
    if calendar is not None:
        opened = calendar.current_session_started_at(checked)
        if opened is not None and received < opened:
            return MarketDataState.STALE
    if checked - received > max_age:
        return MarketDataState.STALE
    return MarketDataState.HEALTHY


def open_intervals_for_asset(
    asset: Asset, start: datetime, end: datetime
) -> tuple[tuple[datetime, datetime], ...]:
    calendar = market_session(asset)
    if calendar is None:
        return ((_aware(start).astimezone(UTC), _aware(end).astimezone(UTC)),)
    return calendar.open_intervals(start, end)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("market-session timestamps must be timezone-aware")
    return value
