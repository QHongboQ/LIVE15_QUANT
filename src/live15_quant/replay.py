"""Deterministic readers for recorded observations; no strategy logic lives here."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from live15_quant.records import (
    CoinbaseTickRecord,
    RobinhoodDiagnosticRecord,
    RobinhoodSnapshotRecord,
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

    def event_diagnostics(self, event_id: str) -> Iterator[RobinhoodDiagnosticRecord]:
        """Read upstream/post-end diagnostics separately from training observations."""

        return self._store.replay_robinhood_diagnostics(event_id)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> ReplayReader:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
