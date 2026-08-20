from __future__ import annotations

import asyncio
import json
import sys
import tomllib
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
from live15_quant.dataset import FeatureStore
from live15_quant.models import (
    Asset,
    MarketTick,
    RecorderEventSeverity,
    RecorderEventType,
)
from live15_quant.recorder_control import ManagedRecorderState, RecorderControlStatus
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

    for route in ("#/", "#/markets", "#/training", "#/system"):
        assert f'href="{route}"' in page
    for asset in ("BTC", "ETH", "Gold", "Silver", "XRP", "WTI Oil", "SOL", "HYPE", "DOGE", "BNB"):
        assert asset in script
    assert "Not enough training data yet" in script
    assert "Missing or insufficient source lookback is not filled with zero." in script
    assert "recorder_state" in script
    assert "quote_status" in script
    assert "underlying_status" in script
    assert "Exact source" in script
    assert "eventFilters" in script
    assert "await pending.catch" in script


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
        assert (await client.post("/api/recorder/start")).json()["state"] == "running"
        assert (await client.post("/api/recorder/pause")).json()["state"] == "paused"
        assert (await client.post("/api/recorder/resume")).json()["state"] == "running"
    assert controller.calls == ["start", "pause", "resume"]

    remote = httpx.ASGITransport(app=app, client=("192.0.2.10", 1234))
    async with httpx.AsyncClient(transport=remote, base_url="http://127.0.0.1") as client:
        assert (await client.post("/api/recorder/pause")).status_code == 403
    assert controller.calls == ["start", "pause", "resume"]


@pytest.mark.asyncio
async def test_clean_startup_and_shutdown_releases_test_client(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            assert (await client.get("/api/system")).status_code == 200
    assert app.state._state == {}
