from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from live15_quant.config import Settings
from live15_quant.models import (
    Asset,
    FreshnessState,
    SecondaryPriceSemantics,
    UnderlyingObservation,
    UnderlyingProvider,
)
from live15_quant.native_recorder import KalshiNativeRecorder
from live15_quant.providers.low_latency import (
    BenchmarkTick,
    LowLatencyProvider,
    SourceDiagnostics,
    SourceTimestampSemantics,
)
from live15_quant.secondary import (
    build_secondary_feature_boundary,
    secondary_from_benchmark_tick,
)
from live15_quant.secondary_diagnostics import build_secondary_diagnostics
from live15_quant.storage import RecorderStorageError, RecorderStore, SecondaryAppendStatus
from tests.test_native_recorder import FakeDiscovery, FakeQuotes, OneTickStream

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def bnb_tick(
    *, event_id: str = "123", source: datetime = NOW, received: datetime | None = None
) -> BenchmarkTick:
    observed = received or source + timedelta(milliseconds=25)
    return BenchmarkTick(
        asset=Asset.BNB,
        provider=LowLatencyProvider.BINANCE_SPOT,
        symbol="BNB/USDT",
        instrument_id="BNBUSDT",
        price=Decimal("871.123456789"),
        source_timestamp=source,
        socket_received_timestamp=observed,
        parse_completed_timestamp=observed,
        timestamp_semantics=SourceTimestampSemantics.TRADE_TIME,
        source_event_id=event_id,
        provenance="wss://data-stream.binance.vision:443/ws/bnbusdt@aggTrade",
    )


def hype_tick(*, event_id: str = "1") -> BenchmarkTick:
    return BenchmarkTick(
        asset=Asset.HYPE,
        provider=LowLatencyProvider.HYPERLIQUID_PERP,
        symbol="HYPE/USDC perpetual BBO",
        instrument_id="HYPE",
        price=Decimal("44.125"),
        bid=Decimal("44.12"),
        ask=Decimal("44.13"),
        source_timestamp=NOW,
        socket_received_timestamp=NOW + timedelta(milliseconds=12),
        parse_completed_timestamp=NOW + timedelta(milliseconds=12),
        timestamp_semantics=SourceTimestampSemantics.BBO_TIME,
        source_event_id=event_id,
        provenance="wss://api.hyperliquid.xyz/ws",
    )


def test_secondary_mapping_preserves_exact_semantics_and_decimal() -> None:
    bnb = secondary_from_benchmark_tick(bnb_tick(), max_source_age_seconds=1)
    hype = secondary_from_benchmark_tick(hype_tick(), max_source_age_seconds=1)
    assert bnb.provider is UnderlyingProvider.BINANCE_SPOT
    assert bnb.price == Decimal("871.123456789")
    assert bnb.price_semantics is SecondaryPriceSemantics.AGGREGATE_TRADE
    assert bnb.bid is None and bnb.ask is None
    assert hype.provider is UnderlyingProvider.HYPERLIQUID_PERP
    assert hype.price_semantics is SecondaryPriceSemantics.BBO_MIDPOINT
    assert (hype.bid, hype.ask) == (Decimal("44.12"), Decimal("44.13"))


def test_secondary_storage_restart_duplicate_out_of_order_and_conflict(tmp_path) -> None:
    path = tmp_path / "secondary.sqlite3"
    observation = secondary_from_benchmark_tick(bnb_tick(), max_source_age_seconds=1)
    with RecorderStore(path) as store:
        assert store.append_secondary_underlying(observation) is SecondaryAppendStatus.INSERTED
        assert store.append_secondary_underlying(observation) is SecondaryAppendStatus.DUPLICATE
    with RecorderStore(path) as store:
        assert store.append_secondary_underlying(observation) is SecondaryAppendStatus.DUPLICATE
        older = secondary_from_benchmark_tick(
            bnb_tick(
                event_id="122",
                source=NOW - timedelta(seconds=1),
                received=NOW + timedelta(seconds=1),
            ),
            max_source_age_seconds=5,
        )
        assert store.append_secondary_underlying(older) is SecondaryAppendStatus.OUT_OF_ORDER
        conflicting = replace(observation, price=Decimal("999"))
        with pytest.raises(RecorderStorageError, match="conflicting secondary"):
            store.append_secondary_underlying(conflicting)
        record = store.latest_secondary_underlying(Asset.BNB, UnderlyingProvider.BINANCE_SPOT)
        assert record is not None
        assert record.price == Decimal("871.123456789")
        assert record.persisted_timestamp is not None
        assert record.receive_persist_latency_ms is not None
        assert store.count("secondary_underlying_observations") == 1


def test_v6_to_v7_migration_only_adds_secondary_table(tmp_path) -> None:
    path = tmp_path / "v6.sqlite3"
    with RecorderStore(path) as store:
        primary = UnderlyingObservation(
            asset=Asset.BNB,
            provider=UnderlyingProvider.PYTH_HERMES,
            symbol="Crypto.BNB/USD",
            feed_id="feed",
            price=Decimal("870"),
            source_timestamp=NOW,
            received_timestamp=NOW,
            confidence=None,
            provenance="official-pyth",
            freshness=FreshnessState.FRESH,
        )
        store.append_underlying(primary)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE secondary_underlying_observations")
    connection.execute("UPDATE recorder_metadata SET value='6'")
    connection.execute("UPDATE underlying_observations SET schema_version=6")
    connection.commit()
    connection.close()
    with RecorderStore(path) as store:
        assert store.count("underlying_observations") == 1
        assert store.count("secondary_underlying_observations") == 0
        replayed = next(
            store.replay_underlying_range(
                Asset.BNB,
                UnderlyingProvider.PYTH_HERMES,
                start=NOW - timedelta(seconds=1),
                end=NOW + timedelta(seconds=1),
            )
        )
        assert replayed.schema_version == 6
        assert store.integrity_check() == "ok"
        assert store._connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_secondary_latest_source_lookup_is_indexed(tmp_path) -> None:
    with RecorderStore(tmp_path / "secondary-index.sqlite3") as store:
        plan = store._connection.execute(
            """EXPLAIN QUERY PLAN
            SELECT source_timestamp FROM secondary_underlying_observations
            WHERE provider=? AND instrument=?
            ORDER BY source_timestamp DESC,id DESC LIMIT 1""",
            (UnderlyingProvider.BINANCE_SPOT.value, "BNBUSDT"),
        ).fetchall()
    assert any("idx_secondary_underlying_latest_source" in str(row[3]) for row in plan)


def test_secondary_feature_boundary_never_uses_future_receive_time(tmp_path) -> None:
    path = tmp_path / "boundary.sqlite3"
    with RecorderStore(path) as store:
        primary = UnderlyingObservation(
            asset=Asset.BNB,
            provider=UnderlyingProvider.PYTH_HERMES,
            symbol="Crypto.BNB/USD",
            feed_id="feed",
            price=Decimal("870"),
            source_timestamp=NOW - timedelta(seconds=2),
            received_timestamp=NOW - timedelta(seconds=1),
            confidence=None,
            provenance="official-pyth",
            freshness=FreshnessState.FRESH,
        )
        store.append_underlying(primary)
        store.append_secondary_underlying(
            secondary_from_benchmark_tick(
                bnb_tick(received=NOW + timedelta(seconds=1)), max_source_age_seconds=5
            )
        )
        primary_record = next(
            store.replay_underlying_range(
                Asset.BNB,
                UnderlyingProvider.PYTH_HERMES,
                start=NOW - timedelta(minutes=1),
                end=NOW,
            )
        )
        secondary_record = store.latest_secondary_underlying(
            Asset.BNB, UnderlyingProvider.BINANCE_SPOT
        )
        boundary = build_secondary_feature_boundary(
            primary_record, secondary_record, asset=Asset.BNB, decision_timestamp=NOW
        )
        assert boundary.secondary_price is None
        assert boundary.missing_reason == "future_observation_unavailable"


def test_secondary_diagnostics_are_bounded_read_only_and_semantics_explicit(tmp_path) -> None:
    path = tmp_path / "diagnostics.sqlite3"
    with RecorderStore(path) as store:
        for offset in (2, 1):
            observed = NOW - timedelta(seconds=offset)
            store.append_underlying(
                UnderlyingObservation(
                    asset=Asset.BNB,
                    provider=UnderlyingProvider.PYTH_HERMES,
                    symbol="Crypto.BNB/USD",
                    feed_id="feed",
                    price=Decimal("870"),
                    source_timestamp=observed,
                    received_timestamp=observed,
                    confidence=None,
                    provenance="official-pyth",
                    freshness=FreshnessState.FRESH,
                )
            )
        store.append_secondary_underlying(
            secondary_from_benchmark_tick(
                bnb_tick(source=NOW - timedelta(milliseconds=500), received=NOW),
                max_source_age_seconds=2,
            )
        )
        before = store.count("secondary_underlying_observations")
    report = build_secondary_diagnostics(path, now=NOW + timedelta(seconds=1))
    bnb = next(item for item in report if item.asset is Asset.BNB)
    assert bnb.latest_secondary_minus_primary == Decimal("1.123456789")
    assert "aggregate trade" in bnb.semantics_note
    assert bnb.secondary.source_clock_skew_detected is False
    assert bnb.secondary.source_clock_skew_observations == 0
    with RecorderStore(path) as store:
        assert store.count("secondary_underlying_observations") == before


def test_secondary_diagnostics_report_cross_clock_skew_without_repair(tmp_path) -> None:
    path = tmp_path / "clock-skew.sqlite3"
    with RecorderStore(path) as store:
        store.append_secondary_underlying(
            secondary_from_benchmark_tick(
                bnb_tick(source=NOW, received=NOW - timedelta(milliseconds=75)),
                max_source_age_seconds=2,
            )
        )
    report = build_secondary_diagnostics(path, now=NOW)
    bnb = next(item for item in report if item.asset is Asset.BNB)
    assert bnb.secondary.median_source_receive_latency_ms == Decimal("-75")
    assert bnb.secondary.source_clock_skew_observations == 1
    assert bnb.secondary.source_clock_skew_detected is True


def test_secondary_diagnostics_exclude_rows_received_after_observed_at(tmp_path) -> None:
    path = tmp_path / "diagnostic-boundary.sqlite3"
    with RecorderStore(path) as store:
        store.append_secondary_underlying(
            secondary_from_benchmark_tick(
                bnb_tick(event_id="current", source=NOW, received=NOW),
                max_source_age_seconds=2,
            )
        )
        store.append_secondary_underlying(
            secondary_from_benchmark_tick(
                bnb_tick(
                    event_id="future",
                    source=NOW + timedelta(seconds=1),
                    received=NOW + timedelta(seconds=1),
                ),
                max_source_age_seconds=2,
            )
        )
    report = build_secondary_diagnostics(path, now=NOW)
    bnb = next(item for item in report if item.asset is Asset.BNB)
    assert bnb.secondary.observations == 1
    assert bnb.secondary.latest_age_seconds == Decimal(0)


class _GoodSecondary:
    diagnostics = SourceDiagnostics()

    def __init__(self, emitted: asyncio.Event) -> None:
        self.emitted = emitted

    async def ticks(self):
        yield hype_tick()
        self.emitted.set()
        await asyncio.Event().wait()

    async def close(self) -> None:
        return None


class _FailingSecondary:
    diagnostics = SourceDiagnostics()

    def __init__(self, attempted: asyncio.Event) -> None:
        self.attempted = attempted

    async def ticks(self):
        if False:
            yield bnb_tick()
        self.attempted.set()
        raise ConnectionError("public source unavailable")

    async def close(self) -> None:
        return None


class _BufferedSecondary:
    """A source whose next messages are immediately available without socket waits."""

    diagnostics = SourceDiagnostics()

    def __init__(self) -> None:
        self.drained = asyncio.Event()

    async def ticks(self):
        for offset in range(100):
            yield bnb_tick(
                event_id=str(offset),
                source=NOW + timedelta(microseconds=offset),
                received=NOW + timedelta(milliseconds=1, microseconds=offset),
            )
        self.drained.set()
        await asyncio.Event().wait()

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_one_secondary_outage_does_not_stop_other_recorder_sources(tmp_path) -> None:
    hype_emitted = asyncio.Event()
    bnb_attempted = asyncio.Event()
    with RecorderStore(tmp_path / "native.sqlite3") as store:
        recorder = KalshiNativeRecorder(
            Settings(
                products=("BTC-USD",),
                enable_secondary_underlying=True,
                reconnect_delay_seconds=0.01,
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            secondary_factories={
                Asset.BNB: lambda: _FailingSecondary(bnb_attempted),
                Asset.HYPE: lambda: _GoodSecondary(hype_emitted),
            },
            now=lambda: NOW,
        )
        task = asyncio.create_task(recorder.run())
        await asyncio.wait_for(bnb_attempted.wait(), 1)
        await asyncio.wait_for(hype_emitted.wait(), 1)
        recorder.request_stop()
        await asyncio.wait_for(task, 1)
        assert store.count("secondary_underlying_observations") == 1
        assert recorder.health().fatal_task is None
        assert recorder.health().source_failures["secondary:BNB"] == "ConnectionError"


@pytest.mark.asyncio
async def test_buffered_secondary_yields_to_other_recorder_workers(tmp_path) -> None:
    """A backlogged public stream must not monopolize the shared event loop."""

    with RecorderStore(tmp_path / "fairness.sqlite3") as store:
        source = _BufferedSecondary()
        recorder = KalshiNativeRecorder(
            Settings(
                products=("BTC-USD",),
                enable_secondary_underlying=True,
                recorder_health_path=tmp_path / "health.json",
            ),
            store,
            discovery=FakeDiscovery(()),
            quotes=FakeQuotes(),
            coinbase_factory=OneTickStream,
            secondary_factories={Asset.BNB: lambda: source},
            now=lambda: NOW,
        )
        task = asyncio.create_task(recorder._record_secondary(Asset.BNB))
        await asyncio.sleep(0)
        # The first observation may run, but the buffered remaining 99 must yield
        # so health/lifecycle tasks get a deterministic scheduling opportunity.
        assert store.count("secondary_underlying_observations") == 1
        await asyncio.wait_for(source.drained.wait(), 1)
        assert store.count("secondary_underlying_observations") == 100
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
