"""Transport-neutral market-data boundary for the official Recorder.

This module is intentionally free of legacy websocket message and command
types.  A provider owns its transport, subscription lifecycle, sequence and
book coordinator.  The Recorder receives only the outcome that has already
passed that provider's reliability rules.

The production Recorder is still wired to the legacy adapter in this change.
The SDK adapter is restricted to isolated sinks until the Recorder's existing
legacy session loop is extracted onto this event contract in a later cutover.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from live15_quant.kalshi_gateway.canonical_ws import CanonicalSdkEvent
from live15_quant.kalshi_gateway.reliability import (
    KalshiReliabilityAdapter,
    ReliabilityState,
    ValidatedRecorderEvent,
)
from live15_quant.kalshi_lifecycle import KalshiLifecycle
from live15_quant.kalshi_ws import SynchronizedKalshiOrderBook
from live15_quant.models import Asset


class RecorderProviderState(StrEnum):
    STOPPED = "STOPPED"
    WAITING_SNAPSHOT = "WAITING_SNAPSHOT"
    SYNCHRONIZED = "SYNCHRONIZED"
    UNSYNCHRONIZED = "UNSYNCHRONIZED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class RecorderMarketDataEvent:
    """One validated domain fact accepted by the Recorder write boundary."""

    canonical: CanonicalSdkEvent
    state: RecorderProviderState
    authoritative: bool
    book: SynchronizedKalshiOrderBook | None
    lifecycle: KalshiLifecycle

    def __post_init__(self) -> None:
        if self.authoritative != (self.book is not None):
            raise ValueError("authoritative Recorder event requires exactly one complete book")
        if self.book is not None and self.book.ticker != self.canonical.ticker:
            raise ValueError("Recorder provider book/ticker identity mismatch")
        if self.authoritative and self.state is not RecorderProviderState.SYNCHRONIZED:
            raise ValueError("authoritative Recorder event must be synchronized")


class RecorderMarketDataProvider(Protocol):
    """Transport-neutral, single-owner source for Recorder market data."""

    @property
    def state(self) -> RecorderProviderState: ...

    @property
    def current_universe(self) -> Mapping[Asset, str]: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def events(self) -> AsyncIterator[RecorderMarketDataEvent]: ...


class _IsolatedReliabilitySink:
    """Accept reliability facts without writing the official Recorder store.

    This is deliberately an isolated test/shadow sink.  It proves that the
    SDK provider can emit the Recorder contract without allowing the SDK path
    to touch production raw/history tables before formal cutover.
    """

    def record_validated(self, *args: object, **kwargs: object) -> int:
        return 0

    def record_state_transitions(self, **kwargs: object) -> None:
        return None


def _state(value: ReliabilityState) -> RecorderProviderState:
    return RecorderProviderState(value.value)


class SdkRecorderMarketDataProvider:
    """Expose SDK reliability output without a second coordinator or sequence owner."""

    def __init__(self, adapter: KalshiReliabilityAdapter) -> None:
        self._adapter = adapter
        self._asset_by_ticker = dict(adapter.asset_by_ticker)
        self._stale_seconds = adapter.stale_seconds
        self._queue: asyncio.Queue[RecorderMarketDataEvent | None] = asyncio.Queue()
        self._started = False
        self._stopped = False

    @classmethod
    def isolated(
        cls,
        *,
        asset_by_ticker: dict[str, Asset],
        connection_id: str,
        stale_seconds: float,
    ) -> SdkRecorderMarketDataProvider:
        return cls(
            KalshiReliabilityAdapter(
                asset_by_ticker,
                _IsolatedReliabilitySink(),
                connection_id=connection_id,
                stale_seconds=stale_seconds,
            )
        )

    @property
    def state(self) -> RecorderProviderState:
        if self._stopped:
            return RecorderProviderState.STOPPED
        states = {item.state for item in self._adapter.assets.values()}
        if not states:
            return RecorderProviderState.STOPPED
        if states == {ReliabilityState.SYNCHRONIZED}:
            return RecorderProviderState.SYNCHRONIZED
        if ReliabilityState.QUARANTINED in states:
            return RecorderProviderState.QUARANTINED
        if ReliabilityState.UNSYNCHRONIZED in states or ReliabilityState.STALE in states:
            return RecorderProviderState.UNSYNCHRONIZED
        return RecorderProviderState.WAITING_SNAPSHOT

    @property
    def current_universe(self) -> Mapping[Asset, str]:
        return dict(self._adapter.ticker_by_asset)

    @property
    def connection_id(self) -> str:
        """Opaque canonical connection identity; not a transport handle."""

        return self._adapter.connection_id

    @property
    def synchronized_count(self) -> int:
        return sum(
            state.state is ReliabilityState.SYNCHRONIZED for state in self._adapter.assets.values()
        )

    @property
    def gap_count(self) -> int:
        return self._adapter.unrecovered_gap_count

    async def start(self) -> None:
        if self._stopped:
            raise RuntimeError("SDK Recorder provider cannot restart after stop")
        self._started = True

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        await self._queue.put(None)

    def accept(self, event: CanonicalSdkEvent) -> RecorderMarketDataEvent:
        if not self._started or self._stopped:
            raise RuntimeError("SDK Recorder provider is not running")
        validated = self._adapter.accept(event)
        provider_event = self._from_validated(validated)
        self._queue.put_nowait(provider_event)
        return provider_event

    async def connection_state_changed(
        self, old_state: str, new_state: str, observed_at: datetime
    ) -> None:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("provider state timestamp must be timezone-aware")
        self._adapter.connection_state_changed(old_state, new_state, observed_at.astimezone(UTC))

    async def begin_reconnect_session(
        self,
        *,
        connection_id: str,
        observed_at: datetime,
        old_state: str,
    ) -> None:
        """Quarantine one replaced SDK session and await new snapshots.

        The SDK owns reconnect/resubscribe and receives the replacement SID.
        LIVE15 only discards its old coordinator/session baseline here; the
        first post-reconnect snapshot for every ticker establishes the new
        authoritative state.
        """

        if not self._started or self._stopped:
            raise RuntimeError("SDK Recorder provider is not running")
        if not connection_id:
            raise ValueError("SDK reconnect session identity is required")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("provider state timestamp must be timezone-aware")
        self._adapter.connection_state_changed(
            old_state,
            "reconnecting",
            observed_at.astimezone(UTC),
        )
        self._adapter = KalshiReliabilityAdapter(
            self._asset_by_ticker,
            _IsolatedReliabilitySink(),
            connection_id=connection_id,
            stale_seconds=self._stale_seconds,
        )

    def events(self) -> AsyncIterator[RecorderMarketDataEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[RecorderMarketDataEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    @staticmethod
    def _from_validated(value: ValidatedRecorderEvent) -> RecorderMarketDataEvent:
        return RecorderMarketDataEvent(
            canonical=value.canonical,
            state=_state(value.state),
            authoritative=value.authoritative,
            book=value.book,
            lifecycle=value.lifecycle,
        )


class LegacyRecorderMarketDataProvider:
    """Legacy compatibility adapter declaration.

    The legacy source continues to own its existing coordinator, sequence and
    snapshot/resync processor.  This adapter deliberately has no SDK imports;
    the pending extraction will make it emit ``RecorderMarketDataEvent`` from
    that single owner instead of exposing websocket commands to Recorder code.
    """

    def __init__(self, universe: Mapping[Asset, str]) -> None:
        self._universe = dict(universe)
        self._state = RecorderProviderState.STOPPED

    @property
    def state(self) -> RecorderProviderState:
        return self._state

    @property
    def current_universe(self) -> Mapping[Asset, str]:
        return dict(self._universe)

    async def start(self) -> None:
        self._state = RecorderProviderState.WAITING_SNAPSHOT

    async def stop(self) -> None:
        self._state = RecorderProviderState.STOPPED

    def events(self) -> AsyncIterator[RecorderMarketDataEvent]:
        return self._no_events()

    async def _no_events(self) -> AsyncIterator[RecorderMarketDataEvent]:
        if False:  # pragma: no cover - maintains an empty async iterator contract
            yield RecorderMarketDataEvent  # type: ignore[misc]
