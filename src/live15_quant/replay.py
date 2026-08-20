"""Deterministic readers for recorded observations; no strategy logic lives here."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from live15_quant.records import (
    CoinbaseTickRecord,
    KalshiMarketRecord,
    KalshiNativeQuoteRecord,
    KalshiSettlementRecord,
    PredictionQuoteRecord,
    RobinhoodDiagnosticRecord,
    RobinhoodSnapshotRecord,
    TrainingLabelExample,
)
from live15_quant.storage import RecorderStore


class ReplayReader:
    """Read append-only history in a stable timestamp/insertion order."""

    def __init__(self, path: Path) -> None:
        self._store = RecorderStore(path)

    def event(self, event_id: str) -> Iterator[RobinhoodSnapshotRecord]:
        return self._store.replay_robinhood(event_id)

    def coinbase(self, product: str) -> Iterator[CoinbaseTickRecord]:
        return self._store.replay_coinbase(product)

    def quotes(self, event_id: str) -> Iterator[PredictionQuoteRecord]:
        """Replay official venue quotes by receive timestamp and insertion id."""

        return self._store.replay_prediction_quotes(event_id)

    def kalshi_market(self, ticker: str) -> Iterator[KalshiMarketRecord]:
        return self._store.replay_kalshi_markets(ticker)

    def kalshi_quotes(self, ticker: str) -> Iterator[KalshiNativeQuoteRecord]:
        """Replay Kalshi-native quotes without Robinhood identifier aliases."""

        return self._store.replay_kalshi_quotes(ticker)

    def kalshi_settlements(self, *, series: str | None = None) -> Iterator[KalshiSettlementRecord]:
        return self._store.replay_kalshi_settlements(series=series)

    def training_label(self, ticker: str, decision_timestamp: datetime) -> TrainingLabelExample:
        return self._store.join_training_label(ticker, decision_timestamp)

    def event_diagnostics(self, event_id: str) -> Iterator[RobinhoodDiagnosticRecord]:
        """Read upstream/post-end diagnostics separately from training observations."""

        return self._store.replay_robinhood_diagnostics(event_id)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> ReplayReader:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
