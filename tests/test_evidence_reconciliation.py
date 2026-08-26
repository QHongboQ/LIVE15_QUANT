from datetime import UTC, datetime, timedelta

import pytest

from live15_quant.canonical_evidence import EvidenceReconciliationError
from live15_quant.evidence_reconciliation import (
    H0_PROVENANCE,
    H1_PROVENANCE,
    H2_PROVENANCE,
    select_stratified_markets,
)


def _markets() -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        {
            "ticker": f"KX{asset}-{day}-{slot}",
            "asset": asset,
            "open_time": (start + timedelta(days=day, minutes=slot * 15)).isoformat(),
            "close_time": (start + timedelta(days=day, minutes=slot * 15 + 15)).isoformat(),
        }
        for day in range(14)
        for asset in ("BTC", "ETH")
        for slot in range(8)
    ]


def test_selection_is_deterministic_and_stratified_by_day_and_asset() -> None:
    first = select_stratified_markets(_markets(), target_days=7, events_per_asset_day=3)
    second = select_stratified_markets(
        list(reversed(_markets())), target_days=7, events_per_asset_day=3
    )
    assert first.tickers == second.tickers
    assert len(first.selected_days) == 7
    assert first.per_day_counts == {day: 6 for day in first.selected_days}
    assert first.per_asset_counts == {"BTC": 21, "ETH": 21}


def test_selection_is_not_the_first_n_markets_from_storage_order() -> None:
    selected = select_stratified_markets(_markets(), target_days=7, events_per_asset_day=2)
    first_n = {str(item["ticker"]) for item in _markets()[:28]}
    assert set(selected.tickers) != first_n
    assert selected.selected_days == (
        "2026-01-01",
        "2026-01-03",
        "2026-01-05",
        "2026-01-07",
        "2026-01-10",
        "2026-01-12",
        "2026-01-14",
    )


def test_provenance_tiers_are_explicit_and_distinct() -> None:
    assert len({H0_PROVENANCE, H1_PROVENANCE, H2_PROVENANCE}) == 3


def test_selector_rejects_first_n_sampling_policy() -> None:
    with pytest.raises(EvidenceReconciliationError, match="first-N"):
        select_stratified_markets(_markets(), sampling_policy="first N API-order markets")
