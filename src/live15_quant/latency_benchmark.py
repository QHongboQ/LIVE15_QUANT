"""Bounded low-latency source benchmark isolated from the production recorder."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from live15_quant.providers.low_latency import BenchmarkSource, BenchmarkTick


@dataclass(frozen=True, slots=True)
class CompletedBenchmarkTick:
    tick: BenchmarkTick
    enqueued_timestamp: datetime
    enqueued_monotonic_ns: int
    persistence_completed_timestamp: datetime
    persistence_completed_monotonic_ns: int


@dataclass(slots=True)
class _TimelineState:
    prior_source_timestamp: datetime | None = None
    prior_received_monotonic_ns: int | None = None
    prior_event_id: str | None = None
    duplicates: int = 0
    out_of_order: int = 0


class LatencyBenchmarkStore:
    """Temporary append-only SQLite sink used only to measure local commit cost."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic_ns: Callable[[], int] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic_ns = monotonic_ns or time.perf_counter_ns
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute(
            """
            CREATE TABLE benchmark_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                provider TEXT NOT NULL,
                symbol TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                price TEXT NOT NULL,
                confidence TEXT,
                bid TEXT,
                ask TEXT,
                source_timestamp TEXT NOT NULL,
                socket_received_timestamp TEXT NOT NULL,
                parse_completed_timestamp TEXT NOT NULL,
                enqueued_timestamp TEXT NOT NULL,
                socket_received_monotonic_ns INTEGER NOT NULL,
                parse_completed_monotonic_ns INTEGER NOT NULL,
                enqueued_monotonic_ns INTEGER NOT NULL,
                source_event_id TEXT NOT NULL,
                timestamp_semantics TEXT NOT NULL,
                provenance TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def append(
        self,
        tick: BenchmarkTick,
        enqueued_timestamp: datetime,
        enqueued_monotonic_ns: int,
    ) -> tuple[datetime, int]:
        if enqueued_monotonic_ns < tick.parse_completed_monotonic_ns:
            raise ValueError("benchmark enqueue monotonic time precedes parsing")
        self._connection.execute(
            """
            INSERT INTO benchmark_observations (
                asset,provider,symbol,instrument_id,price,confidence,bid,ask,source_timestamp,
                socket_received_timestamp,parse_completed_timestamp,enqueued_timestamp,
                socket_received_monotonic_ns,parse_completed_monotonic_ns,enqueued_monotonic_ns,
                source_event_id,timestamp_semantics,provenance
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                tick.asset.value,
                tick.provider.value,
                tick.symbol,
                tick.instrument_id,
                str(tick.price),
                str(tick.confidence) if tick.confidence is not None else None,
                str(tick.bid) if tick.bid is not None else None,
                str(tick.ask) if tick.ask is not None else None,
                tick.source_timestamp.isoformat(),
                tick.socket_received_timestamp.isoformat(),
                tick.parse_completed_timestamp.isoformat(),
                enqueued_timestamp.isoformat(),
                tick.socket_received_monotonic_ns,
                tick.parse_completed_monotonic_ns,
                enqueued_monotonic_ns,
                tick.source_event_id,
                tick.timestamp_semantics.value,
                tick.provenance,
            ),
        )
        self._connection.commit()
        return self._clock(), self._monotonic_ns()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> LatencyBenchmarkStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class LowLatencyBenchmarkRunner:
    """Run independent official streams with a bounded queue and absolute timeout."""

    def __init__(
        self,
        sources: Sequence[BenchmarkSource],
        *,
        queue_size: int = 4096,
        clock: Callable[[], datetime] | None = None,
        monotonic_ns: Callable[[], int] | None = None,
        duration_waiter: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not sources:
            raise ValueError("at least one benchmark source is required")
        if queue_size <= 0:
            raise ValueError("benchmark queue size must be positive")
        self._sources = tuple(sources)
        self._queue_size = queue_size
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic_ns = monotonic_ns or time.perf_counter_ns
        self._duration_waiter = duration_waiter
        self._source_errors: dict[str, str] = {}
        self._queue_drops = 0

    async def run(self, seconds: float) -> dict[str, Any]:
        if not 1 <= seconds <= 300:
            raise ValueError("benchmark duration must be within 1..300 seconds")
        queue: asyncio.Queue[tuple[BenchmarkTick, datetime, int]] = asyncio.Queue(
            maxsize=self._queue_size
        )
        completed: list[CompletedBenchmarkTick] = []
        with tempfile.TemporaryDirectory(prefix="live15-latency-") as directory:
            with LatencyBenchmarkStore(
                Path(directory) / "benchmark.sqlite3",
                clock=self._clock,
                monotonic_ns=self._monotonic_ns,
            ) as store:
                consumer = asyncio.create_task(self._consume(queue, store, completed))
                producers = [
                    asyncio.create_task(self._produce(source, queue)) for source in self._sources
                ]
                try:
                    await self._duration_waiter(seconds)
                finally:
                    # Cancel active reads first. In particular, requests' blocking SSE
                    # iterator can otherwise make Response.close wait for its read lock.
                    for task in producers:
                        task.cancel()
                    await asyncio.gather(*producers, return_exceptions=True)
                    await asyncio.gather(
                        *(source.close() for source in self._sources), return_exceptions=True
                    )
                    joiner = asyncio.create_task(queue.join())
                    await asyncio.wait((consumer, joiner), return_when=asyncio.FIRST_COMPLETED)
                    consumer_error: BaseException | None = None
                    if consumer.done() and not consumer.cancelled():
                        consumer_error = consumer.exception() or RuntimeError(
                            "benchmark persistence consumer stopped unexpectedly"
                        )
                    if not joiner.done():
                        joiner.cancel()
                    await asyncio.gather(joiner, return_exceptions=True)
                    consumer.cancel()
                    await asyncio.gather(consumer, return_exceptions=True)
                    if consumer_error is not None:
                        raise consumer_error
        return summarize_benchmark(
            completed,
            self._sources,
            source_errors=self._source_errors,
            queue_drops=self._queue_drops,
            duration_seconds=seconds,
        )

    async def _produce(
        self,
        source: BenchmarkSource,
        queue: asyncio.Queue[tuple[BenchmarkTick, datetime, int]],
    ) -> None:
        source_name = type(source).__name__
        try:
            async for tick in source.ticks():
                enqueued_monotonic_ns = self._monotonic_ns()
                enqueued = self._clock()
                try:
                    queue.put_nowait((tick, enqueued, enqueued_monotonic_ns))
                except asyncio.QueueFull:
                    self._queue_drops += 1
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._source_errors[source_name] = type(error).__name__

    @staticmethod
    async def _consume(
        queue: asyncio.Queue[tuple[BenchmarkTick, datetime, int]],
        store: LatencyBenchmarkStore,
        completed: list[CompletedBenchmarkTick],
    ) -> None:
        while True:
            tick, enqueued, enqueued_monotonic_ns = await queue.get()
            try:
                persisted, persisted_monotonic_ns = store.append(
                    tick, enqueued, enqueued_monotonic_ns
                )
                completed.append(
                    CompletedBenchmarkTick(
                        tick,
                        enqueued,
                        enqueued_monotonic_ns,
                        persisted,
                        persisted_monotonic_ns,
                    )
                )
            finally:
                queue.task_done()


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _milliseconds(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() * 1000


def _monotonic_milliseconds(start_ns: int, end_ns: int) -> float:
    return (end_ns - start_ns) / 1_000_000


def _summary_for_samples(samples: list[CompletedBenchmarkTick]) -> dict[str, Any]:
    state = _TimelineState()
    source_receive: list[float] = []
    socket_parse: list[float] = []
    parse_enqueue: list[float] = []
    enqueue_persist: list[float] = []
    receive_persist: list[float] = []
    gaps: list[float] = []
    prices: set[Decimal] = set()
    stale = 0
    negative_latency = 0
    wall_clock_regressions = 0
    for sample in samples:
        tick = sample.tick
        latency = _milliseconds(tick.source_timestamp, tick.socket_received_timestamp)
        source_receive.append(latency)
        if latency < 0:
            negative_latency += 1
        if (
            tick.parse_completed_timestamp < tick.socket_received_timestamp
            or sample.enqueued_timestamp < tick.parse_completed_timestamp
            or sample.persistence_completed_timestamp < sample.enqueued_timestamp
        ):
            wall_clock_regressions += 1
        socket_parse.append(
            _monotonic_milliseconds(
                tick.socket_received_monotonic_ns, tick.parse_completed_monotonic_ns
            )
        )
        parse_enqueue.append(
            _monotonic_milliseconds(tick.parse_completed_monotonic_ns, sample.enqueued_monotonic_ns)
        )
        enqueue_persist.append(
            _monotonic_milliseconds(
                sample.enqueued_monotonic_ns, sample.persistence_completed_monotonic_ns
            )
        )
        receive_persist.append(
            _monotonic_milliseconds(
                tick.socket_received_monotonic_ns,
                sample.persistence_completed_monotonic_ns,
            )
        )
        if state.prior_received_monotonic_ns is not None:
            gaps.append(
                (tick.socket_received_monotonic_ns - state.prior_received_monotonic_ns)
                / 1_000_000_000
            )
        if (
            state.prior_source_timestamp is not None
            and tick.source_timestamp < state.prior_source_timestamp
        ):
            state.out_of_order += 1
        if state.prior_event_id == tick.source_event_id:
            state.duplicates += 1
        if (tick.socket_received_timestamp - tick.source_timestamp).total_seconds() > 15:
            stale += 1
        state.prior_source_timestamp = tick.source_timestamp
        state.prior_received_monotonic_ns = tick.socket_received_monotonic_ns
        state.prior_event_id = tick.source_event_id
        prices.add(tick.price)
    return {
        "observations": len(samples),
        "distinct_prices": len(prices),
        "source_receive_latency_ms": _distribution(source_receive),
        "socket_parse_ms": _distribution(socket_parse),
        "parse_enqueue_ms": _distribution(parse_enqueue),
        "enqueue_persist_ms": _distribution(enqueue_persist),
        "receive_persist_ms": _distribution(receive_persist),
        "receive_gap_seconds": _distribution(gaps),
        "max_gap_seconds": max(gaps) if gaps else None,
        "stale_rate": stale / len(samples) if samples else None,
        "negative_latency_observations": negative_latency,
        "clock_offset_warning": negative_latency > 0,
        "local_wall_clock_regressions": wall_clock_regressions,
        "duplicates": state.duplicates,
        "out_of_order": state.out_of_order,
    }


def _distribution(values: list[float]) -> dict[str, float | None]:
    return {
        "median": median(values) if values else None,
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def summarize_benchmark(
    completed: Sequence[CompletedBenchmarkTick],
    sources: Sequence[BenchmarkSource],
    *,
    source_errors: dict[str, str],
    queue_drops: int,
    duration_seconds: float,
) -> dict[str, Any]:
    grouped: defaultdict[tuple[str, str], list[CompletedBenchmarkTick]] = defaultdict(list)
    for sample in completed:
        grouped[(sample.tick.asset.value, sample.tick.provider.value)].append(sample)
    return {
        "schema_version": "1.0.0",
        "duration_seconds": duration_seconds,
        "temporary_store_deleted": True,
        "production_recorder_touched": False,
        "queue_drops": queue_drops,
        "measurement_complete": queue_drops == 0 and not source_errors,
        "source_errors": dict(sorted(source_errors.items())),
        "sources": {
            type(source).__name__: {
                "connection_attempts": source.diagnostics.connection_attempts,
                "reconnects": source.diagnostics.reconnects,
                "malformed_messages": source.diagnostics.malformed_messages,
                "transport_errors": source.diagnostics.transport_errors,
            }
            for source in sources
        },
        "assets": {
            f"{asset}:{provider}": _summary_for_samples(samples)
            for (asset, provider), samples in sorted(grouped.items())
        },
    }
