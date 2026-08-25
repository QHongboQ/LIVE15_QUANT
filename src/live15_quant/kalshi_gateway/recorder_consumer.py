"""Transport-neutral atomic consumer for validated Recorder market-data facts.

Providers own transport, ordering and book reconstruction.  This consumer
owns only the commit boundary: a domain writer must durably persist the event
before the consumer advances its in-memory checkpoint.  The official SQLite
writer remains to be extracted from the legacy storage serializer; until that
writer exists this class is exercised only against isolated writers.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from live15_quant.kalshi_gateway.canonical_ws import CanonicalEventType
from live15_quant.kalshi_gateway.recorder_provider import RecorderMarketDataEvent
from live15_quant.kalshi_ws import KalshiBookSide, KalshiBookSyncStatus, KalshiWsEventKind
from live15_quant.models import DataRole
from live15_quant.storage import KalshiWsPersistenceEvent, RecorderStore


class RecorderDomainWriteError(RuntimeError):
    """A validated event could not be durably committed."""


class RecorderDomainWriter(Protocol):
    """One atomic raw/history write for an already validated provider event."""

    def persist_market_data_event(self, event: RecorderMarketDataEvent) -> None: ...


class RecorderDomainBatchWriter(Protocol):
    def persist_market_data_events(self, events: tuple[RecorderMarketDataEvent, ...]) -> None: ...


@dataclass(frozen=True, slots=True)
class RecorderConsumerCheckpoint:
    """The last event known to have completed its durable write transaction."""

    connection_id: str
    subscription_id: int | None
    sequence: int | None


class RecorderMarketDataConsumer:
    """Persist validated provider events without transport/coordinator knowledge."""

    def __init__(
        self,
        writer: RecorderDomainWriter,
        *,
        batch_size: int = 128,
        flush_interval_seconds: float = 1.0,
        on_committed: Callable[[tuple[RecorderMarketDataEvent, ...]], None] | None = None,
    ) -> None:
        if batch_size < 1 or batch_size > 128 or flush_interval_seconds <= 0:
            raise ValueError("Recorder consumer batch configuration is invalid")
        self._writer = writer
        self._batch_writer = writer if hasattr(writer, "persist_market_data_events") else None
        self._batch_size = batch_size
        self._flush_interval_seconds = flush_interval_seconds
        self._checkpoint: RecorderConsumerCheckpoint | None = None
        self._seen: set[tuple[str, int | None, int | None, str]] = set()
        self._pending: list[
            tuple[tuple[str, int | None, int | None, str], RecorderMarketDataEvent]
        ] = []
        self._last_flush = time.monotonic()
        self._closed = False
        self._on_committed = on_committed

    @property
    def checkpoint(self) -> RecorderConsumerCheckpoint | None:
        return self._checkpoint

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def consume(self, event: RecorderMarketDataEvent) -> bool:
        canonical = event.canonical
        identity = (
            canonical.connection_id,
            canonical.subscription_id,
            canonical.sequence,
            canonical.event_type.value,
        )
        if identity in self._seen or any(key == identity for key, _ in self._pending):
            return False
        if self._batch_writer is not None:
            self._pending.append((identity, event))
            if (
                len(self._pending) >= self._batch_size
                or time.monotonic() - self._last_flush >= self._flush_interval_seconds
            ):
                self.flush()
            return True
        try:
            self._writer.persist_market_data_event(event)
        except Exception as error:
            # The checkpoint deliberately remains at the preceding committed
            # fact.  The provider must decide whether recovery/replay is safe.
            raise RecorderDomainWriteError("Recorder domain transaction failed") from error
        self._seen.add(identity)
        self._checkpoint = RecorderConsumerCheckpoint(
            connection_id=canonical.connection_id,
            subscription_id=canonical.subscription_id,
            sequence=canonical.sequence,
        )
        return True

    def flush(self) -> int:
        if not self._pending or self._batch_writer is None:
            return 0
        pending = tuple(self._pending)
        try:
            self._batch_writer.persist_market_data_events(tuple(event for _, event in pending))
        except Exception as error:
            raise RecorderDomainWriteError("Recorder domain batch transaction failed") from error
        for identity, event in pending:
            self._seen.add(identity)
            canonical = event.canonical
            self._checkpoint = RecorderConsumerCheckpoint(
                canonical.connection_id, canonical.subscription_id, canonical.sequence
            )
        self._pending.clear()
        self._last_flush = time.monotonic()
        if self._on_committed is not None:
            self._on_committed(tuple(event for _, event in pending))
        return len(pending)

    async def run_idle_flush(self, stop_event: asyncio.Event) -> None:
        """Flush partial batches on a bounded heartbeat owned by the host runtime."""

        while not self._closed:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._flush_interval_seconds)
            except TimeoutError:
                pass
            if self._pending:
                self.flush()
            if stop_event.is_set():
                return

    def close(self) -> int:
        if self._closed:
            return 0
        self._closed = True
        return self.flush()


class RecorderStoreDomainWriter:
    """Isolated adapter from validated SDK facts to the shared Store writer."""

    def __init__(self, store: RecorderStore) -> None:
        self._store = store

    def persist_market_data_event(self, event: RecorderMarketDataEvent) -> None:
        self.persist_market_data_events((event,))

    def persist_market_data_events(self, events: tuple[RecorderMarketDataEvent, ...]) -> None:
        persistence_events = tuple(
            value for event in events if (value := self._persistence_event(event)) is not None
        )
        if persistence_events:
            self._store.write_kalshi_ws_persistence_event_batch_atomic(persistence_events)

    @staticmethod
    def _persistence_event(
        event: RecorderMarketDataEvent,
    ) -> KalshiWsPersistenceEvent | None:
        canonical = event.canonical
        if canonical.event_type not in {CanonicalEventType.SNAPSHOT, CanonicalEventType.DELTA}:
            return None
        if canonical.subscription_id is None or canonical.sequence is None:
            raise RecorderDomainWriteError("sequenced book event is incomplete")
        if canonical.event_type is CanonicalEventType.SNAPSHOT:
            if canonical.market_id is None:
                raise RecorderDomainWriteError("snapshot market identity is incomplete")
            return KalshiWsPersistenceEvent(
                connection_id=canonical.connection_id,
                subscription_id=canonical.subscription_id,
                sequence=canonical.sequence,
                event_kind=KalshiWsEventKind.SNAPSHOT,
                ticker=canonical.ticker,
                market_id=canonical.market_id,
                market_tickers=(),
                side=None,
                price=None,
                quantity_delta=None,
                yes_bids=canonical.yes_bids,
                no_bids=canonical.no_bids,
                source_timestamp=canonical.exchange_timestamp,
                received_timestamp=canonical.sdk_receive_timestamp,
                parse_timestamp=canonical.sdk_receive_timestamp,
                sync_status_after=(
                    KalshiBookSyncStatus.SYNCHRONIZED
                    if event.authoritative
                    else KalshiBookSyncStatus.UNSYNCHRONIZED
                ),
                provenance=canonical.provenance,
                data_role=DataRole.CONTRACT_MARKET_QUOTE,
            )
        else:
            if (
                canonical.market_id is None
                or canonical.delta_side not in {"yes", "no"}
                or canonical.delta_price is None
                or canonical.delta_quantity is None
            ):
                raise RecorderDomainWriteError("delta payload is incomplete")
            return KalshiWsPersistenceEvent(
                connection_id=canonical.connection_id,
                subscription_id=canonical.subscription_id,
                sequence=canonical.sequence,
                event_kind=KalshiWsEventKind.DELTA,
                ticker=canonical.ticker,
                market_id=canonical.market_id,
                market_tickers=(),
                side=(KalshiBookSide.YES if canonical.delta_side == "yes" else KalshiBookSide.NO),
                price=canonical.delta_price,
                quantity_delta=canonical.delta_quantity,
                yes_bids=(),
                no_bids=(),
                source_timestamp=canonical.exchange_timestamp,
                received_timestamp=canonical.sdk_receive_timestamp,
                parse_timestamp=canonical.sdk_receive_timestamp,
                sync_status_after=(
                    KalshiBookSyncStatus.SYNCHRONIZED
                    if event.authoritative
                    else KalshiBookSyncStatus.UNSYNCHRONIZED
                ),
                provenance=canonical.provenance,
                data_role=DataRole.CONTRACT_MARKET_QUOTE,
            )
