"""Restartable Kalshi-native historical lifecycle and settlement backfill."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from live15_quant.kalshi_lifecycle import KalshiNativeMarketProvider
from live15_quant.models import Asset
from live15_quant.providers.kalshi import KALSHI_15MIN_SERIES
from live15_quant.storage import RecorderStore


@dataclass(frozen=True, slots=True)
class BackfillSummary:
    asset: Asset
    source_path: str
    pages: int
    market_observations_written: int
    settlements_available: int
    rejected_markets: int
    complete: bool
    next_cursor: str | None


class KalshiBackfillService:
    """Persist each page and cursor before advancing, making restarts deterministic."""

    def __init__(self, provider: KalshiNativeMarketProvider, store: RecorderStore) -> None:
        self._provider = provider
        self._store = store

    def run(
        self,
        asset: Asset,
        *,
        start: datetime,
        end: datetime,
        historical: bool,
        max_pages: int | None = None,
    ) -> BackfillSummary:
        if max_pages is not None and max_pages <= 0:
            raise ValueError("max_pages must be positive")
        source_path = "/historical/markets" if historical else "/markets"
        series = KALSHI_15MIN_SERIES[asset]
        saved = self._store.load_backfill_cursor(
            series=series, source_path=source_path, start=start, end=end
        )
        if saved is not None and saved[1]:
            return BackfillSummary(asset, source_path, 0, 0, 0, 0, True, None)
        cursor = saved[0] if saved is not None else None
        pages = written = settlements = rejected = 0
        next_cursor = cursor
        complete = False
        for page in self._provider.backfill_pages(
            asset,
            start=start,
            end=end,
            historical=historical,
            cursor=cursor,
        ):
            pages += 1
            rejected += len(page.rejected_tickers)
            for market in page.markets:
                written += int(self._store.append_kalshi_market(market))
                settlements += int(market.settlement is not None)
            next_cursor = page.next_cursor
            complete = next_cursor is None
            self._store.save_backfill_state(
                series=series,
                source_path=source_path,
                start=start,
                end=end,
                next_cursor=next_cursor,
                complete=complete,
                updated_at=datetime.now(UTC),
            )
            if max_pages is not None and pages >= max_pages:
                break
        return BackfillSummary(
            asset,
            source_path,
            pages,
            written,
            settlements,
            rejected,
            complete,
            next_cursor,
        )
