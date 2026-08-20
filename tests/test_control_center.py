from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

import live15_quant.control_center as control_center
import live15_quant.control_center_store as control_center_store
from live15_quant.config import Settings
from live15_quant.control_center import LOCAL_HOST, create_app
from live15_quant.control_center_models import RecorderState
from live15_quant.control_center_service import ControlCenterService
from live15_quant.models import Asset, MarketTick
from live15_quant.storage import RecorderStore
from tests.test_kalshi_lifecycle import NOW, provider, quote, raw_market


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
    assert system.json()["api_mode"] == "read_only"
    assert system.json()["bind_host"] == "127.0.0.1"
    assert page.status_code == 200
    assert "LOCAL BACKEND ONLINE" in page.text


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
    )
    service = ControlCenterService(configured, clock=lambda: NOW)

    transport = httpx.ASGITransport(app=create_app(configured, service))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/api/health")

    assert response.json()["recorder_state"] == RecorderState.STALE
    assert response.json()["heartbeat_status"] == "stale"
    assert response.json()["heartbeat_age_seconds"] == 31
    serialized = response.text.lower()
    assert "never-return" not in serialized
    assert "heartbeat-secret" not in serialized
    assert "heartbeat-private" not in serialized
    assert "heartbeat-signature" not in serialized


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
    assert len(payload["per_asset"]) == 10


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

    assert all(method in {"GET", "HEAD"} for method, _path in routes)
    assert {path for _method, path in routes} == {
        "/",
        "/api/health",
        "/api/markets",
        "/api/markets/{asset}",
        "/api/coverage",
        "/api/system",
    }
    assert not any(
        word in path.lower()
        for _method, path in routes
        for word in ("order", "trade", "credential", "key", "shell", "file", "recorder/start")
    )


@pytest.mark.asyncio
async def test_clean_startup_and_shutdown_releases_test_client(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            assert (await client.get("/api/system")).status_code == 200
    assert app.state._state == {}
