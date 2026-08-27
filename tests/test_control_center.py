from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import fastapi.routing
import httpx
import pytest

import live15_quant.control_center as control_center
import live15_quant.control_center_store as control_center_store
from live15_quant.config import Settings
from live15_quant.control_center import LOCAL_HOST, create_app
from live15_quant.control_center_models import RecorderState
from live15_quant.control_center_service import ControlCenterService
from live15_quant.dataset import FeatureStore
from live15_quant.models import (
    Asset,
    FreshnessState,
    MarketTick,
    RecorderEventSeverity,
    RecorderEventType,
    UnderlyingObservation,
    UnderlyingProvider,
)
from live15_quant.recorder_control import ManagedRecorderState, RecorderControlStatus
from live15_quant.secondary import secondary_from_benchmark_tick
from live15_quant.storage import RecorderStore
from tests.test_kalshi_lifecycle import NOW, provider, quote, raw_market
from tests.test_secondary_underlying import bnb_tick


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values = {
        "recorder_data_path": tmp_path / "raw.sqlite3",
        "feature_store_path": tmp_path / "features.sqlite3",
        "paper_data_path": tmp_path / "paper.sqlite3",
        "recorder_health_path": tmp_path / "health.json",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def write_health(path: Path, observed: datetime = NOW, **extra: object) -> None:
    payload: dict[str, object] = {
        "status": "healthy",
        "started_at": (observed - timedelta(minutes=5)).isoformat(),
        "observed_at": observed.isoformat(),
        "uptime_seconds": 300,
        "current_markets": {},
        "active_settlement_followups": 0,
        "database_bytes": 4096,
        "wal_bytes": 0,
        "retry_counts": {},
        "source_failures": {},
        "stale_sources": [],
        "written_records": 0,
    }
    payload.update(extra)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_localhost_only_binding(monkeypatch, tmp_path: Path) -> None:
    configured = settings(tmp_path, ui_port=8123)
    called: dict[str, object] = {}
    monkeypatch.setattr(control_center, "load_settings", lambda: configured)
    monkeypatch.setattr(control_center, "configure_logging", lambda _level: None)
    monkeypatch.setattr(
        control_center.uvicorn,
        "run",
        lambda app, **kwargs: called.update({"app": app, **kwargs}),
    )
    monkeypatch.setattr(sys, "argv", ["live15-ui"])

    control_center.main()

    assert LOCAL_HOST == "127.0.0.1"
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 8123


@pytest.mark.asyncio
async def test_health_and_system_when_recorder_stopped(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    service = ControlCenterService(configured, clock=lambda: NOW)
    transport = httpx.ASGITransport(app=create_app(configured, service))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        health = await client.get("/api/health")
        system = await client.get("/api/system")
        page = await client.get("/")

    assert health.status_code == 200
    assert health.json()["recorder_state"] == "stopped"
    assert health.json()["heartbeat_status"] == "unavailable"
    assert system.json()["api_mode"] == "read_only_data_with_bounded_recorder_control"
    assert system.json()["bind_host"] == "127.0.0.1"
    assert page.status_code == 200
    assert "LIVE15 Control Center" in page.text


@pytest.mark.asyncio
async def test_stale_health_is_explicit_and_secrets_are_whitelisted(tmp_path: Path) -> None:
    configured = settings(
        tmp_path,
        ui_heartbeat_stale_seconds=30,
        kalshi_demo_api_key_id="never-return-id",
        kalshi_demo_private_key_path=Path("never-return.key"),
    )
    write_health(
        configured.recorder_health_path,
        NOW - timedelta(seconds=31),
        kalshi_demo_api_key_id="heartbeat-secret",
        private_key="heartbeat-private",
        signature="heartbeat-signature",
        fatal_task="kalshi-quotes-Silver",
        fatal_error_type="KalshiPublicApiError",
    )
    service = ControlCenterService(configured, clock=lambda: NOW)

    transport = httpx.ASGITransport(app=create_app(configured, service))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/api/health")

    assert response.json()["recorder_state"] == RecorderState.STALE
    assert response.json()["heartbeat_status"] == "stale"
    assert response.json()["heartbeat_age_seconds"] == 31
    assert response.json()["fatal_task"] == "kalshi-quotes-Silver"
    assert response.json()["fatal_error_type"] == "KalshiPublicApiError"
    serialized = response.text.lower()
    assert "never-return" not in serialized
    assert "heartbeat-secret" not in serialized
    assert "heartbeat-private" not in serialized
    assert "heartbeat-signature" not in serialized


def test_health_reclassifies_legacy_weekend_stale_as_market_closed(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    saturday = datetime(2026, 8, 22, 4, 0, tzinfo=UTC)
    friday = datetime(2026, 8, 21, 20, 59, tzinfo=UTC)
    write_health(
        configured.recorder_health_path,
        observed=saturday,
        status="degraded",
        stale_sources=["pyth:Gold", "pyth:Silver", "pyth:WTI Oil"],
        stale_workers=[],
        last_additional_underlying={
            "Gold": friday.isoformat(),
            "Silver": friday.isoformat(),
            "WTI Oil": friday.isoformat(),
        },
    )
    health = ControlCenterService(configured, clock=lambda: saturday).health()

    assert health.status == "healthy"
    assert health.stale_sources == []
    assert set(health.market_closed_sources) == {
        "pyth:Gold",
        "pyth:Silver",
        "pyth:WTI Oil",
    }


@pytest.mark.asyncio
async def test_health_exposes_bounded_worker_progress_and_event_loop_lag(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    write_health(
        configured.recorder_health_path,
        NOW,
        worker_progress={"coinbase": NOW.isoformat()},
        worker_progress_age_seconds={"coinbase": 0.25},
        stale_workers=["kalshi_quote:BTC"],
        event_loop_lag_seconds=0.015,
    )
    service = ControlCenterService(configured, clock=lambda: NOW)
    transport = httpx.ASGITransport(app=create_app(configured, service))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        payload = (await client.get("/api/health")).json()
    assert payload["worker_progress"]["coinbase"] == NOW.isoformat().replace("+00:00", "Z")
    assert payload["worker_progress_age_seconds"]["coinbase"] == 0.25
    assert payload["stale_workers"] == ["kalshi_quote:BTC"]
    assert payload["event_loop_lag_seconds"] == 0.015


def test_health_ignores_new_internal_archive_fields_without_hiding_known_truth(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    write_health(
        configured.recorder_health_path,
        NOW,
        status="healthy",
        database_bytes=8192,
        wal_bytes=4096,
        written_records=123,
        ws_archive={
            "enabled": True,
            "verified": 7,
            "adaptive_retention": {"controller_mode": "INSUFFICIENT_EVIDENCE"},
            "raw_ws_growth_bytes_per_day": 1234.5,
        },
    )

    health = ControlCenterService(configured, clock=lambda: NOW).health()

    assert health.status == "healthy"
    assert health.heartbeat_status == "available"
    assert health.database_bytes == 8192
    assert health.wal_bytes == 4096
    assert health.written_records == 123
    assert health.ws_archive.enabled is True
    assert health.ws_archive.verified == 7


def test_system_exposes_live_supervisor_component_truth(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "runtime-supervisor-status.json").write_text(
        json.dumps(
            {
                "components": {
                    "paper_forward": {
                        "status": "RUNNING",
                        "pid": os.getpid(),
                        "started_at": NOW.isoformat(),
                        "last_heartbeat": NOW.isoformat(),
                        "last_error": None,
                        "process_alive": True,
                        "expected_mode": "PAPER_SHADOW_LOCAL_ONLY",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    system = ControlCenterService(configured, clock=lambda: NOW).system()

    component = system.runtime_components["paper_forward"]
    assert component.status == "RUNNING"
    assert component.pid == os.getpid()
    assert component.process_alive is True
    assert component.heartbeat_age_seconds == 0


@pytest.mark.asyncio
async def test_health_exposes_bounded_ws_sync_state_without_credentials(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    write_health(
        configured.recorder_health_path,
        NOW,
        kalshi_ws_connection_state="synchronized",
        kalshi_ws_synchronized_markets={"BTC": "KXBTC15M-EXACT"},
        kalshi_ws_synchronized_count=1,
        kalshi_ws_book_age_seconds={"BTC": 0.25},
        kalshi_ws_seq_gaps=2,
        kalshi_ws_resync_count=2,
        kalshi_ws_reconnect_count=1,
        kalshi_ws_queue_high_watermark=17,
        kalshi_ws_queue_capacity=32,
        kalshi_ws_queue_depth=3,
        kalshi_ws_queue_enqueued=100,
        kalshi_ws_queue_dequeued=97,
        kalshi_ws_queue_full_waits=4,
        kalshi_ws_queue_dropped=0,
        kalshi_ws_queue_max_backlog_seconds=0.75,
        kalshi_ws_queue_above_50_seconds=0.5,
        kalshi_ws_queue_above_75_seconds=0.25,
        kalshi_ws_queue_above_90_seconds=0.125,
        kalshi_ws_receive_persist_latency_ms="0.125",
        kalshi_rest_fallback_status="healthy",
        private_key="must-not-escape",
        signature="must-not-escape",
    )
    service = ControlCenterService(configured, clock=lambda: NOW)
    transport = httpx.ASGITransport(app=create_app(configured, service))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/api/health")
    payload = response.json()
    assert payload["kalshi_ws_connection_state"] == "synchronized"
    assert payload["kalshi_ws_synchronized_count"] == 1
    assert payload["kalshi_ws_book_age_seconds"] == {"BTC": 0.25}
    assert payload["kalshi_ws_queue_capacity"] == 32
    assert payload["kalshi_ws_queue_depth"] == 3
    assert payload["kalshi_ws_queue_enqueued"] == 100
    assert payload["kalshi_ws_queue_dequeued"] == 97
    assert payload["kalshi_ws_queue_dropped"] == 0
    assert payload["kalshi_ws_queue_above_90_seconds"] == 0.125
    assert payload["kalshi_rest_fallback_status"] == "healthy"
    assert "must-not-escape" not in response.text


def test_naive_health_timestamp_fails_closed(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    write_health(configured.recorder_health_path, NOW.replace(tzinfo=None))

    health = ControlCenterService(configured, clock=lambda: NOW).health()

    assert health.recorder_state == RecorderState.ERROR
    assert health.heartbeat_status == "error"
    assert health.source_failures == {"health": "malformed_heartbeat"}


@pytest.mark.asyncio
async def test_markets_api_returns_all_ten_with_missing_not_zero(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    service = ControlCenterService(configured, clock=lambda: NOW)

    transport = httpx.ASGITransport(app=create_app(configured, service))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        markets = (await client.get("/api/markets")).json()
        btc = (await client.get("/api/markets/BTC")).json()

    assert len(markets) == 10
    assert {item["asset"] for item in markets} == {asset.value for asset in Asset}
    assert btc["ticker"] is None
    assert btc["yes_bid"] is None
    assert btc["quote_status"] == "missing"
    assert btc["underlying_status"] == "missing"


@pytest.mark.asyncio
async def test_market_detail_reuses_native_storage_and_feature_engine(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    market = provider().parse_market(Asset.BTC, raw_market(), NOW)
    with RecorderStore(configured.recorder_data_path) as store:
        store.append_kalshi_market(market)
        store.append_kalshi_quote(quote(market.ticker, market.event_ticker, NOW))
        store.append_coinbase(
            MarketTick(
                symbol="BTC-USD",
                price=market.target + Decimal("1"),
                bid=market.target,
                ask=market.target + Decimal("2"),
                received_at=NOW,
                exchange_time=NOW,
            )
        )
    write_health(configured.recorder_health_path, current_markets={"BTC": market.ticker})
    service = ControlCenterService(configured, clock=lambda: NOW)

    transport = httpx.ASGITransport(app=create_app(configured, service))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/api/markets/BTC")

    payload = response.json()
    assert response.status_code == 200
    assert payload["ticker"] == market.ticker
    assert payload["target"] == str(market.target)
    assert payload["yes_bid"] == "0.5000"
    assert payload["yes_ask"] == "0.5100"
    assert payload["underlying_price"] == str(market.target + 1)
    assert payload["features"]["signed_distance_to_target"]["value"] == "1.00000000"


@pytest.mark.asyncio
async def test_pyth_underlying_status_honors_provider_freshness(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    market = provider().parse_market(Asset.GOLD, raw_market(Asset.GOLD), NOW)
    with RecorderStore(configured.recorder_data_path) as store:
        store.append_kalshi_market(market)
        store.append_underlying(
            UnderlyingObservation(
                asset=Asset.GOLD,
                provider=UnderlyingProvider.PYTH_HERMES,
                symbol="Metal.XAU/USD",
                feed_id="a" * 64,
                price=market.target + Decimal("1"),
                source_timestamp=NOW,
                received_timestamp=NOW,
                confidence=Decimal("0.01"),
                provenance="official-test",
                freshness=FreshnessState.STALE,
            )
        )
    write_health(configured.recorder_health_path, current_markets={"Gold": market.ticker})
    service = ControlCenterService(configured, clock=lambda: NOW)
    transport = httpx.ASGITransport(app=create_app(configured, service))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        payload = (await client.get("/api/markets/Gold")).json()

    assert payload["underlying_product"] == "Metal.XAU/USD"
    assert payload["underlying_provider"] == "pyth_hermes"
    assert payload["underlying_price"] == str(market.target + 1)
    assert payload["underlying_status"] == "stale"


@pytest.mark.asyncio
async def test_pyth_underlying_uses_configured_stale_threshold(tmp_path: Path) -> None:
    configured = settings(tmp_path, recorder_pyth_stale_seconds=15)
    market = provider().parse_market(Asset.GOLD, raw_market(Asset.GOLD), NOW)
    with RecorderStore(configured.recorder_data_path) as store:
        store.append_kalshi_market(market)
        store.append_underlying(
            UnderlyingObservation(
                asset=Asset.GOLD,
                provider=UnderlyingProvider.PYTH_HERMES,
                symbol="Metal.XAU/USD",
                feed_id="a" * 64,
                price=market.target,
                source_timestamp=NOW,
                received_timestamp=NOW,
                confidence=None,
                provenance="official-test",
                freshness=FreshnessState.FRESH,
            )
        )
    write_health(configured.recorder_health_path, current_markets={"Gold": market.ticker})
    service = ControlCenterService(configured, clock=lambda: NOW + timedelta(seconds=16))
    transport = httpx.ASGITransport(app=create_app(configured, service))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        payload = (await client.get("/api/markets/Gold")).json()

    assert payload["underlying_provider"] == "pyth_hermes"
    assert payload["underlying_status"] == "stale"


@pytest.mark.asyncio
async def test_closed_market_retains_last_price_with_non_live_status(tmp_path: Path) -> None:
    configured = settings(tmp_path, recorder_pyth_stale_seconds=15)
    market = provider().parse_market(Asset.GOLD, raw_market(Asset.GOLD), NOW)
    received = datetime(2026, 8, 21, 20, 59, tzinfo=UTC)
    saturday = datetime(2026, 8, 22, 4, 0, tzinfo=UTC)
    with RecorderStore(configured.recorder_data_path) as store:
        store.append_kalshi_market(market)
        store.append_underlying(
            UnderlyingObservation(
                asset=Asset.GOLD,
                provider=UnderlyingProvider.PYTH_HERMES,
                symbol="Metal.XAU/USD",
                feed_id="a" * 64,
                price=market.target,
                source_timestamp=received,
                received_timestamp=received,
                confidence=None,
                provenance="official-test",
                freshness=FreshnessState.FRESH,
            )
        )
    write_health(configured.recorder_health_path, current_markets={"Gold": market.ticker})
    service = ControlCenterService(configured, clock=lambda: saturday)
    transport = httpx.ASGITransport(app=create_app(configured, service))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        payload = (await client.get("/api/markets/Gold")).json()

    assert payload["underlying_price"] == str(market.target)
    assert payload["underlying_status"] == "market_closed"


@pytest.mark.asyncio
async def test_bnb_detail_keeps_pyth_primary_and_exposes_binance_secondary(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    market = provider().parse_market(Asset.BNB, raw_market(Asset.BNB), NOW)
    with RecorderStore(configured.recorder_data_path) as store:
        store.append_kalshi_market(market)
        store.append_underlying(
            UnderlyingObservation(
                asset=Asset.BNB,
                provider=UnderlyingProvider.PYTH_HERMES,
                symbol="Crypto.BNB/USD",
                feed_id="bnb-feed",
                price=Decimal("870"),
                source_timestamp=NOW,
                received_timestamp=NOW,
                confidence=None,
                provenance="official-pyth",
                freshness=FreshnessState.FRESH,
            )
        )
        store.append_secondary_underlying(
            secondary_from_benchmark_tick(
                bnb_tick(source=NOW, received=NOW), max_source_age_seconds=10
            )
        )
    write_health(configured.recorder_health_path, current_markets={"BNB": market.ticker})
    service = ControlCenterService(configured, clock=lambda: NOW)
    transport = httpx.ASGITransport(app=create_app(configured, service))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        payload = (await client.get("/api/markets/BNB")).json()

    assert payload["primary_provider"] == "pyth_hermes"
    assert payload["underlying_price"] == "870"
    assert payload["secondary_provider"] == "binance_spot"
    assert payload["secondary_price"] == "871.123456789"
    assert payload["primary_secondary_price_diff"] == "1.123456789"
    assert payload["secondary_status"] == "healthy"
    assert payload["secondary_clock_skew"] is False


@pytest.mark.asyncio
async def test_secondary_clock_skew_does_not_make_receive_fresh_data_stale(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    market = provider().parse_market(Asset.BNB, raw_market(Asset.BNB), NOW)
    with RecorderStore(configured.recorder_data_path) as store:
        store.append_kalshi_market(market)
        store.append_secondary_underlying(
            secondary_from_benchmark_tick(
                bnb_tick(source=NOW, received=NOW - timedelta(milliseconds=75)),
                max_source_age_seconds=10,
            )
        )
    write_health(configured.recorder_health_path, current_markets={"BNB": market.ticker})
    service = ControlCenterService(configured, clock=lambda: NOW)
    transport = httpx.ASGITransport(app=create_app(configured, service))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        payload = (await client.get("/api/markets/BNB")).json()

    assert payload["secondary_status"] == "healthy"
    assert payload["secondary_clock_skew"] is True


def test_health_ticker_cannot_cross_asset_boundary(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    market = provider().parse_market(Asset.BTC, raw_market(), NOW)
    with RecorderStore(configured.recorder_data_path) as store:
        store.append_kalshi_market(market)
    write_health(configured.recorder_health_path, current_markets={"ETH": market.ticker})

    payload = ControlCenterService(configured, clock=lambda: NOW).market(Asset.ETH)

    assert payload.asset == "ETH"
    assert payload.ticker is None
    assert payload.availability == "market_missing"


def test_unopenable_store_is_typed_unavailable_without_error_detail(
    tmp_path: Path, monkeypatch
) -> None:
    configured = settings(tmp_path)
    configured.recorder_data_path.touch()
    monkeypatch.setattr(
        control_center_store.sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            control_center_store.sqlite3.OperationalError("sensitive database detail")
        ),
    )

    payload = ControlCenterService(configured, clock=lambda: NOW).market(Asset.BTC)

    assert payload.availability == "raw_store_unavailable"
    assert "secret" not in payload.model_dump_json().lower()


@pytest.mark.asyncio
async def test_coverage_empty_state_is_typed_and_does_not_fabricate_rates(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    service = ControlCenterService(configured, clock=lambda: NOW)

    transport = httpx.ASGITransport(app=create_app(configured, service))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/api/coverage")

    payload = response.json()
    assert payload["status"] == "not_enough_training_data"
    assert payload["finalized_events"] == 0
    assert payload["training_rows"] == 0
    assert payload["label_balance"] is None
    assert payload["missing_feature_rates"] is None
    assert payload["snapshot_status"] == "not_built"
    assert payload["unevaluated_finalized_events"] == 0
    assert len(payload["per_asset"]) == 10


def test_coverage_distinguishes_unevaluated_events_from_rejections(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    with RecorderStore(configured.recorder_data_path) as raw:
        empty_snapshot = raw.training_source_snapshot()
    with FeatureStore(configured.feature_store_path) as feature:
        feature.begin_build("empty-build", {"mode": "pooled"}, empty_snapshot)
        feature.complete_build("empty-build", {})

    finalized = provider().parse_market(
        Asset.BTC,
        raw_market(Asset.BTC, status="finalized", result="yes"),
        NOW + timedelta(minutes=16),
    )
    assert finalized.settlement is not None
    with RecorderStore(configured.recorder_data_path) as raw:
        raw.append_kalshi_settlement(finalized.settlement)

    coverage = ControlCenterService(configured, clock=lambda: NOW).coverage()

    assert coverage.status == "available"
    assert coverage.snapshot_status == "outdated"
    assert coverage.finalized_events == 1
    assert coverage.snapshot_finalized_events == 0
    assert coverage.unevaluated_finalized_events == 1
    assert coverage.trainability_rejections is None
    assert coverage.per_asset["BTC"].evaluated_finalized_events == 0
    assert coverage.per_asset["BTC"].unevaluated_finalized_events == 1


def _write_current_trainable_fixture(path: Path, *, rows: int = 2) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE current_trainable_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE current_trainable_checkpoint (
            singleton INTEGER PRIMARY KEY,
            last_settlement_row_id INTEGER NOT NULL,
            last_evaluated_timestamp TEXT NOT NULL,
            materializer_schema_version INTEGER NOT NULL,
            evaluator_version TEXT NOT NULL,
            recorder_schema_version INTEGER NOT NULL,
            source_identity TEXT NOT NULL,
            source_limits_json TEXT NOT NULL
        );
        CREATE TABLE current_trainable_events (
            settlement_row_id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            asset TEXT NOT NULL,
            series TEXT NOT NULL,
            event_ticker TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            settlement_timestamp TEXT NOT NULL,
            eligibility_status TEXT NOT NULL,
            exclusion_reasons_json TEXT NOT NULL,
            source_limits_json TEXT NOT NULL,
            materialized_timestamp TEXT NOT NULL,
            content_hash TEXT NOT NULL
        );
        CREATE TABLE current_trainable_rows (
            id INTEGER PRIMARY KEY,
            settlement_row_id INTEGER NOT NULL,
            asset TEXT NOT NULL,
            series TEXT NOT NULL,
            ticker TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            decision_timestamp TEXT NOT NULL,
            time_remaining_seconds TEXT NOT NULL,
            target TEXT NOT NULL,
            label TEXT NOT NULL,
            features_json TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            materialized_timestamp TEXT NOT NULL,
            content_hash TEXT NOT NULL
        );
        """
    )
    connection.executemany(
        "INSERT INTO current_trainable_metadata VALUES (?, ?)",
        [
            ("schema_version", "1"),
            ("dataset_version", "1.2.0"),
            ("feature_schema_version", "1.0.0"),
        ],
    )
    connection.execute(
        "INSERT INTO current_trainable_checkpoint VALUES "
        "(1, 10, ?, 1, 'evaluator-v1', 10, 'source-hash', '{}')",
        (NOW.isoformat(),),
    )
    connection.execute(
        "INSERT INTO current_trainable_events VALUES "
        "(10, 'KXBTC15M-E', 'BTC', 'KXBTC15M', 'KXBTC15M-E', ?, ?, ?, "
        "'eligible', '{}', '{}', ?, 'event-hash')",
        (
            NOW.isoformat(),
            (NOW + timedelta(minutes=15)).isoformat(),
            (NOW + timedelta(minutes=15)).isoformat(),
            NOW.isoformat(),
        ),
    )
    for row_id in range(1, rows + 1):
        connection.execute(
            "INSERT INTO current_trainable_rows VALUES "
            "(?, 10, 'BTC', 'KXBTC15M', 'KXBTC15M-E', ?, ?, ?, '60', "
            "'100', 'yes', '{}', '{}', ?, ?)",
            (
                row_id,
                NOW.isoformat(),
                (NOW + timedelta(minutes=15)).isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                f"hash-{row_id}",
            ),
        )
    connection.commit()
    connection.close()


def test_training_projection_separates_raw_current_snapshot_and_frozen_facts(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path, current_trainable_path=tmp_path / "current.sqlite3")
    _write_current_trainable_fixture(configured.current_trainable_path)
    finalized = provider().parse_market(
        Asset.BTC,
        raw_market(Asset.BTC, status="finalized", result="yes"),
        NOW + timedelta(minutes=16),
    )
    assert finalized.settlement is not None
    with RecorderStore(configured.recorder_data_path) as raw:
        raw.append_kalshi_settlement(finalized.settlement)
        source_snapshot = raw.training_source_snapshot()
    with FeatureStore(configured.feature_store_path) as feature:
        feature.begin_build("frozen-build", {"mode": "pooled"}, source_snapshot)
        feature.complete_build("frozen-build", {"rows_count": 7, "events_count": 3})

    projection = ControlCenterService(configured, clock=lambda: NOW).training()

    assert projection.raw_finalized_pool.status == "available"
    assert projection.raw_finalized_pool.events == 1
    assert projection.current_trainable.status == "available"
    assert projection.current_trainable.rows == 2
    assert projection.latest_completed_dataset.build_id == "frozen-build"
    assert projection.latest_completed_dataset.status == "available"
    assert projection.frozen_experiment_facts == []


def test_training_missing_projection_is_unknown_not_zero(tmp_path: Path) -> None:
    configured = settings(tmp_path, current_trainable_path=tmp_path / "missing.sqlite3")
    projection = ControlCenterService(configured, clock=lambda: NOW).training()

    assert projection.current_trainable.status == "unknown"
    assert projection.current_trainable.reason_code == "CURRENT_TRAINABLE_UNAVAILABLE"
    assert projection.current_trainable.rows is None
    assert projection.current_trainable.events is None


def test_training_api_exposes_dedicated_read_only_projection(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    transport = httpx.ASGITransport(app=create_app(configured))

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.get("/api/training")

    response = asyncio.run(request())
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) >= {
        "raw_finalized_pool",
        "current_trainable",
        "latest_completed_dataset",
        "frozen_experiment_facts",
    }
    assert payload["current_trainable"]["rows"] is None


@pytest.mark.asyncio
async def test_dedicated_admin_projections_are_read_only_and_typed(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    transport = httpx.ASGITransport(app=create_app(configured))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        responses = await asyncio.gather(
            client.get("/api/data"),
            client.get("/api/archive"),
            client.get("/api/storage"),
            client.get("/api/operations"),
        )

    assert all(response.status_code == 200 for response in responses)
    assert set(responses[0].json()) >= {"raw_store", "finalized_events", "freshness"}
    assert set(responses[1].json()) >= {"state", "purge_is_dry_run", "quarantined_chunks"}
    assert responses[1].json()["purge_is_dry_run"] is True
    assert set(responses[2].json()) >= {"disk_free_bytes", "purge_is_dry_run"}
    assert responses[3].json()["recorder_heartbeat"] in {
        "available",
        "unavailable",
        "stale",
        "error",
    }


def test_archive_projection_exposes_operational_and_compression_facts(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    write_health(
        configured.recorder_health_path,
        NOW,
        ws_archive={
            "enabled": True,
            "archive_poll_mode": "CATCH_UP",
            "archive_next_poll_seconds": 2.0,
            "archive_backlog_events": 10_000,
            "archive_throughput_events_per_second": 4_500.0,
            "verified": 36_051,
            "failed": 0,
            "waiting_for_replay_baseline": 0,
            "quarantined": 8,
            "eligible": 1_224,
            "purged": 265_586_658,
            "compressed": 8_000,
            "uncompressed": 80_000,
            "compression_ratio": 10.0,
            "last_purge_deleted_events": 10_000,
            "last_purge_transaction_seconds": 0.37,
            "last_purge_reusable_bytes": 56,
        },
    )

    archive = ControlCenterService(configured, clock=lambda: NOW).archive()

    assert archive.poll_mode == "CATCH_UP"
    assert archive.next_poll_seconds == 2.0
    assert archive.waiting_chunks == 0
    assert archive.uncompressed_archive_bytes == 80_000
    assert archive.compressed_archive_bytes == 8_000
    assert archive.compressed_bytes_saved == 72_000
    assert archive.compression_saving_percent == 90.0
    assert archive.purge_eligible_chunks == 1_224
    assert archive.total_purged_events == 265_586_658
    assert archive.last_purge_deleted_events == 10_000


def test_archive_and_storage_missing_facts_remain_na_and_reusable_is_not_reclaimed(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    write_health(
        configured.recorder_health_path,
        NOW,
        ws_archive={
            "enabled": True,
            "freelist_reusable_bytes": 56_800_000,
            "physical_database_bytes": 72_500_000_000,
            "disk_free_bytes": 173_800_000_000,
            "hot_sqlite_used_bytes": 72_450_000_000,
            "wal_bytes": 8_400_000,
            "cold_archive_bytes": 8_100_000_000,
            "disk_threshold_state": "normal",
        },
    )

    service = ControlCenterService(configured, clock=lambda: NOW)
    archive = service.archive()
    storage = service.storage()

    assert archive.compressed_bytes_saved is None
    assert archive.compression_saving_percent is None
    assert storage.sqlite_reusable_bytes == 56_800_000
    assert storage.physical_reclaimed_bytes is None
    assert storage.compaction_minimum_required_bytes == configured.ws_compaction_min_reclaim_bytes
    assert storage.compaction_minimum_required_percent == 25.0
    assert storage.compaction_status == "NOT_ELIGIBLE"
    assert storage.raw_ws_growth_bytes_per_day is None
    assert storage.cold_archive_growth_bytes_per_day is None
    assert storage.net_disk_growth_bytes_per_day is None


def test_coverage_is_bounded_by_short_thread_safe_cache(tmp_path: Path, monkeypatch) -> None:
    configured = settings(tmp_path)
    monotonic_values = iter((10.0, 20.0, 41.0))
    service = ControlCenterService(configured, monotonic=lambda: next(monotonic_values))
    calls = 0
    original = service.store.coverage

    def counted_coverage() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(service.store, "coverage", counted_coverage)

    assert service.coverage().status == "not_enough_training_data"
    assert service.coverage().status == "not_enough_training_data"
    assert calls == 1
    assert service.coverage().status == "not_enough_training_data"
    assert calls == 2


def test_routes_are_read_only_and_have_no_sensitive_capabilities(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))
    routes = {(method, route.path) for route in app.routes for method in route.methods or set()}

    assert {(method, path) for method, path in routes if method == "POST"} == {
        ("POST", "/api/recorder/start"),
        ("POST", "/api/recorder/pause"),
        ("POST", "/api/recorder/resume"),
    }
    assert all(method in {"GET", "HEAD", "POST"} for method, _path in routes)
    assert {path for _method, path in routes} == {
        "/",
        "/assets/app.css",
        "/assets/app.js",
        "/api/health",
        "/api/markets",
        "/api/markets/{asset}",
        "/api/coverage",
        "/api/data",
        "/api/training",
        "/api/archive",
        "/api/storage",
        "/api/operations",
        "/api/events",
        "/api/recorder/start",
        "/api/recorder/pause",
        "/api/recorder/resume",
        "/api/system",
    }
    forbidden_segments = {
        "order",
        "orders",
        "trade",
        "trading",
        "credential",
        "credentials",
        "key",
        "keys",
        "shell",
        "file",
        "files",
    }
    assert not any(
        forbidden_segments.intersection(segment.lower() for segment in path.split("/") if segment)
        for _method, path in routes
    )


@pytest.mark.asyncio
async def test_dashboard_static_assets_and_security_headers(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=create_app(settings(tmp_path)))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        page, stylesheet, script = await asyncio.gather(
            client.get("/"), client.get("/assets/app.css"), client.get("/assets/app.js")
        )

    assert page.status_code == stylesheet.status_code == script.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert script.headers["content-type"].startswith("text/javascript")
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert 'href="/assets/app.css"' in page.text
    assert 'src="/assets/app.js"' in page.text


@pytest.mark.asyncio
async def test_operational_events_api_is_bounded_typed_and_filterable(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    with RecorderStore(configured.recorder_data_path) as store:
        store.append_recorder_event(
            observed_timestamp=NOW,
            severity=RecorderEventSeverity.WARNING,
            event_type=RecorderEventType.LIFECYCLE_REGRESSION,
            asset=Asset.BTC,
            source="kalshi_settlement:BTC",
            error_type="StaleLifecycleRegression",
            message="Ignored stale closed; retained settlement_pending",
        )
    transport = httpx.ASGITransport(app=create_app(configured))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get(
            "/api/events",
            params={"severity": "warning", "asset": "BTC", "limit": 20},
        )
        invalid = await client.get("/api/events", params={"limit": 201})
        naive_time = await client.get("/api/events", params={"since": "2026-08-20T12:00:00"})

    assert response.status_code == 200
    assert response.json() == [
        {
            "timestamp": NOW.isoformat().replace("+00:00", "Z"),
            "severity": "warning",
            "event_type": "lifecycle_regression",
            "asset": "BTC",
            "source": "kalshi_settlement:BTC",
            "error_type": "StaleLifecycleRegression",
            "message": "Ignored stale closed; retained settlement_pending",
        }
    ]
    assert invalid.status_code == 422
    assert naive_time.status_code == 422


def test_dashboard_assets_are_declared_for_clean_install() -> None:
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert set(project["tool"]["setuptools"]["package-data"]["live15_quant"]) == {
        "web/*.html",
        "web/*.css",
        "web/*.js",
    }


@pytest.mark.asyncio
async def test_frontend_contains_all_read_only_views_and_ten_asset_contract(
    tmp_path: Path,
) -> None:
    transport = httpx.ASGITransport(app=create_app(settings(tmp_path)))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        page = (await client.get("/")).text
        script = (await client.get("/assets/app.js")).text

    for route in (
        "#/",
        "#/dashboard",
        "#/markets",
        "#/data",
        "#/training",
        "#/archive",
        "#/storage",
        "#/operations",
        "#/system",
    ):
        assert f'href="{route}"' in page
    for asset in ("BTC", "ETH", "Gold", "Silver", "XRP", "WTI Oil", "SOL", "HYPE", "DOGE", "BNB"):
        assert asset in script
    assert "Not enough training data yet" in script
    assert "Missing or insufficient source lookback is not filled with zero." in script
    assert "recorder_state" in script
    assert "quote_status" in script
    assert "underlying_status" in script
    assert "Secondary \N{MINUS SIGN} primary" in script
    assert "secondary_source_receive_latency_ms" in script
    assert "Exact source" in script
    assert "eventFilters" in script
    assert "await pending.catch" in script
    for endpoint in (
        "/api/data",
        "/api/training",
        "/api/archive",
        "/api/storage",
        "/api/operations",
    ):
        assert endpoint in script
    assert "purge" in script.lower()
    assert "destructive" in script.lower()
    for label in (
        "Adaptive cadence",
        "Poll Mode",
        "Next poll",
        "Compression savings",
        "SQLite reusable",
        "Physical disk reclaimed",
        "Compaction gate",
        "Raw WS growth",
        "Cold archive growth",
        "Net disk growth",
    ):
        assert label in script
    assert "N/A" in script


@pytest.mark.asyncio
async def test_frontend_polling_is_bounded_and_exposes_only_recorder_controls(
    tmp_path: Path,
) -> None:
    transport = httpx.ASGITransport(app=create_app(settings(tmp_path)))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        page = (await client.get("/")).text.lower()
        script = (await client.get("/assets/app.js")).text.lower()
        stylesheet = (await client.get("/assets/app.css")).text

    assert "health: 2500" in script
    assert "markets: 2500" in script
    assert "detail: 2500" in script
    assert "system: 30000" in script
    assert "coverage: 60000" in script
    assert "setinterval(updatecountdowns, 1000)" in script
    assert "setinterval(() => refresh(false), 500)" in script
    assert 'queryselectorall("[data-window-end]")' in script
    assert "windowend" in script
    assert "document.hidden" in script
    assert "inflight" in script
    assert "progress.value = numeric === null" not in script
    assert "if (numeric !== null && number.isfinite(numeric))" in script
    assert "check status before retrying" in script
    assert 'credentials: "omit"' in script
    assert 'method: "post"' in script
    assert "start collection" in script
    assert "pause collection" in script
    assert "resume collection" in script
    assert '--font-ui: "Segoe UI", "Microsoft YaHei", Arial, sans-serif' in stylesheet
    assert '--font-cjk: "Microsoft YaHei", "SimSun", sans-serif' in stylesheet
    assert "font-variant-numeric: tabular-nums" in stylesheet
    quote_rule = stylesheet.split(".quote-prices", 1)[1].split("}", 1)[0]
    assert "var(--font-ui)" in quote_rule
    assert "monospace" not in quote_rule
    assert "<form" not in page
    assert not any(
        endpoint in script
        for endpoint in (
            "/api/trade",
            "/api/order",
            "/api/credential",
            "/api/shell",
        )
    )


class FakeRecorderController:
    def __init__(self) -> None:
        self.current = ManagedRecorderState.STOPPED
        self.calls: list[str] = []

    def status(self) -> RecorderControlStatus:
        return RecorderControlStatus(self.current, None, self.current.value)

    def start(self) -> RecorderControlStatus:
        self.calls.append("start")
        self.current = ManagedRecorderState.RUNNING
        return self.status()

    def resume(self) -> RecorderControlStatus:
        self.calls.append("resume")
        self.current = ManagedRecorderState.RUNNING
        return self.status()

    def pause(self) -> RecorderControlStatus:
        self.calls.append("pause")
        self.current = ManagedRecorderState.PAUSED
        return self.status()


@pytest.mark.asyncio
async def test_recorder_control_routes_are_explicit_and_localhost_only(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    controller = FakeRecorderController()
    service = ControlCenterService(configured, clock=lambda: NOW, controller=controller)  # type: ignore[arg-type]
    app = create_app(configured, service)
    local = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=local, base_url="http://127.0.0.1") as client:
        started = (await client.post("/api/recorder/start")).json()
        paused = (await client.post("/api/recorder/pause")).json()
        resumed = (await client.post("/api/recorder/resume")).json()
        assert started == {
            "action": "start",
            "action_succeeded": True,
            "outcome": "applied",
            "state": "running",
            "pid": None,
            "message": "running",
        }
        assert paused["action"] == "pause"
        assert paused["action_succeeded"] is True
        assert paused["outcome"] == "applied"
        assert paused["state"] == "paused"
        assert resumed["action"] == "resume"
        assert resumed["state"] == "running"
    assert controller.calls == ["start", "pause", "resume"]
    remote = httpx.ASGITransport(app=app, client=("192.0.2.10", 1234))
    async with httpx.AsyncClient(transport=remote, base_url="http://127.0.0.1") as client:
        assert (await client.post("/api/recorder/pause")).status_code == 403
    assert controller.calls == ["start", "pause", "resume"]


@pytest.mark.asyncio
async def test_repeated_pause_returns_typed_idempotent_success(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    controller = FakeRecorderController()
    controller.current = ManagedRecorderState.PAUSED
    service = ControlCenterService(configured, clock=lambda: NOW, controller=controller)  # type: ignore[arg-type]
    app = create_app(configured, service)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        first = await client.post("/api/recorder/pause")
        second = await client.post("/api/recorder/pause")

    assert first.status_code == second.status_code == 200
    assert first.json()["outcome"] == "already_in_state"
    assert second.json()["outcome"] == "already_in_state"
    assert first.json()["state"] == second.json()["state"] == "paused"

    assert controller.calls == ["pause", "pause"]


@pytest.mark.asyncio
async def test_successful_pause_is_not_revalidated_after_controller_action(
    tmp_path: Path, monkeypatch
) -> None:
    configured = settings(tmp_path)
    controller = FakeRecorderController()
    controller.current = ManagedRecorderState.RUNNING
    service = ControlCenterService(configured, clock=lambda: NOW, controller=controller)  # type: ignore[arg-type]
    app = create_app(configured, service)

    async def forbidden_post_action_serialization(*_args, **_kwargs):
        raise AssertionError("FastAPI must not re-serialize a completed recorder action")

    monkeypatch.setattr(fastapi.routing, "serialize_response", forbidden_post_action_serialization)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post("/api/recorder/pause")

    assert response.status_code == 200
    assert response.json()["action_succeeded"] is True
    assert response.json()["state"] == "paused"
    assert controller.calls == ["pause"]


@pytest.mark.asyncio
async def test_clean_startup_and_shutdown_releases_test_client(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            assert (await client.get("/api/system")).status_code == 200
    assert app.state._state == {}
