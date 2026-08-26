"""Deterministic evidence-layer reconciliation helpers.

This module only selects already-acquired historical market metadata.  It does not alter the
authoritative Recorder, Dataset v2, holdout, or any runtime state.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

STRATIFICATION_SCHEMA_VERSION = "evid-recon-001-stratified-v1"
H0_PROVENANCE = "H0_LIVE_NATIVE"
H1_PROVENANCE = "H1_KALSHI_OFFICIAL_HISTORY"
H2_PROVENANCE = "H2_DEPTHFEED_RECORDED_L2"


def _utc(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("market timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


def _evenly_spaced[T](items: list[T], count: int) -> list[T]:
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    indices = {round(index * (len(items) - 1) / (count - 1)) for index in range(count)}
    return [items[index] for index in sorted(indices)]


@dataclass(frozen=True, slots=True)
class StratifiedSelection:
    schema_version: str
    target_days: int
    events_per_asset_day: int
    selected_markets: tuple[Mapping[str, object], ...]
    selected_days: tuple[str, ...]
    per_day_counts: Mapping[str, int]
    per_asset_counts: Mapping[str, int]
    per_day_asset_counts: Mapping[str, Mapping[str, int]]

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(str(item["ticker"]) for item in self.selected_markets)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy": "evenly-spaced UTC day x asset x bounded events",
            "target_days": self.target_days,
            "events_per_asset_day": self.events_per_asset_day,
            "selected_days": list(self.selected_days),
            "selected_market_count": len(self.selected_markets),
            "per_day_counts": dict(self.per_day_counts),
            "per_asset_counts": dict(self.per_asset_counts),
            "per_day_asset_counts": {
                day: dict(counts) for day, counts in self.per_day_asset_counts.items()
            },
            "selected_markets": [dict(item) for item in self.selected_markets],
            "tickers": list(self.tickers),
        }


def select_stratified_markets(
    markets: Iterable[Mapping[str, object]],
    *,
    target_days: int = 7,
    events_per_asset_day: int = 3,
    assets: Iterable[str] | None = None,
) -> StratifiedSelection:
    """Select a bounded, deterministic multi-day market sample.

    The selector never depends on API/storage first-N order.  Days are evenly spaced over the
    available metadata range, and events are evenly spaced within each UTC-day/asset bucket.
    """

    if target_days <= 0 or events_per_asset_day <= 0:
        raise ValueError("target_days and events_per_asset_day must be positive")
    allowed = {str(asset) for asset in assets} if assets is not None else None
    buckets: dict[tuple[date, str], list[Mapping[str, object]]] = defaultdict(list)
    for market in markets:
        ticker = market.get("ticker")
        asset = market.get("asset")
        opened = market.get("open_time")
        if not all(isinstance(value, str) and value for value in (ticker, asset, opened)):
            raise ValueError("market metadata lacks ticker, asset, or open_time")
        if allowed is not None and str(asset) not in allowed:
            continue
        day = _utc(str(opened)).date()
        buckets[(day, str(asset))].append(market)
    available_days = sorted({day for day, _asset in buckets})
    if not available_days:
        return StratifiedSelection(
            STRATIFICATION_SCHEMA_VERSION,
            target_days,
            events_per_asset_day,
            (),
            (),
            {},
            {},
            {},
        )
    day_count = min(target_days, len(available_days))
    chosen_indices = (
        {round(index * (len(available_days) - 1) / (day_count - 1)) for index in range(day_count)}
        if day_count > 1
        else {len(available_days) // 2}
    )
    selected_days = tuple(available_days[index].isoformat() for index in sorted(chosen_indices))
    selected: list[Mapping[str, object]] = []
    for day in (available_days[index] for index in sorted(chosen_indices)):
        for asset in sorted(asset for bucket_day, asset in buckets if bucket_day == day):
            records = sorted(
                buckets[(day, asset)],
                key=lambda item: (str(item["open_time"]), str(item["ticker"])),
            )
            selected.extend(_evenly_spaced(records, events_per_asset_day))
    selected.sort(
        key=lambda item: (str(item["open_time"]), str(item["asset"]), str(item["ticker"]))
    )
    if len({str(item["ticker"]) for item in selected}) != len(selected):
        raise ValueError("stratified selection produced duplicate tickers")
    per_day = Counter(str(_utc(str(item["open_time"])).date()) for item in selected)
    per_asset = Counter(str(item["asset"]) for item in selected)
    per_day_asset: dict[str, Counter[str]] = defaultdict(Counter)
    for item in selected:
        per_day_asset[str(_utc(str(item["open_time"])).date())][str(item["asset"])] += 1
    return StratifiedSelection(
        STRATIFICATION_SCHEMA_VERSION,
        target_days,
        events_per_asset_day,
        tuple(selected),
        selected_days,
        dict(sorted(per_day.items())),
        dict(sorted(per_asset.items())),
        {day: dict(sorted(counts.items())) for day, counts in sorted(per_day_asset.items())},
    )
