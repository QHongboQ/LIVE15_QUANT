from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import ValidationError

from live15_quant.config import (
    KALSHI_DEMO_API_BASE_URL,
    KALSHI_DEMO_WEBSOCKET_URL,
    KALSHI_PRODUCTION_WEBSOCKET_URL,
    KALSHI_PUBLIC_API_BASE_URL,
)
from live15_quant.kalshi_gateway.client import (
    GatewayCredentials,
    KalshiEnvironment,
    KalshiGatewayConfig,
    KalshiGatewayError,
    build_sdk_client,
    production_credentials,
    production_runtime_environment,
)
from live15_quant.kalshi_gateway.execution import (
    GatewayOrderIntent,
    KalshiExecutionGateway,
    KalshiWriteDisabledError,
)
from live15_quant.kalshi_gateway.market_data import KalshiMarketDataGateway
from live15_quant.kalshi_gateway.portfolio import KalshiPortfolioGateway
from live15_quant.kalshi_gateway.websocket import (
    KalshiWebSocketGateway,
    _ImmutableOrderbookFeed,
    _load_ws_json_with_sparse_snapshot_compat,
)


class Resource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def status(self) -> str:
        return "status"

    def list_all(self, **kwargs: object):
        self.calls.append(("list_all", kwargs))
        return iter(())

    def list_all_trades(self, **kwargs: object):
        self.calls.append(("list_all_trades", kwargs))
        return iter(())

    def get(self, identity: str) -> object:
        self.calls.append(("get", identity))
        return SimpleNamespace(order_id=identity)

    def orderbook(self, ticker: str, *, depth: int) -> tuple[str, int]:
        return ticker, depth

    def balance(self, *, exchange_index: int | None) -> object:
        return SimpleNamespace(balance=Decimal("10"), exchange_index=exchange_index)

    def positions_all(self, **kwargs: object):
        self.calls.append(("positions_all", kwargs))
        return iter(())

    def fills_all(self, **kwargs: object):
        self.calls.append(("fills_all", kwargs))
        return iter(())


def client() -> SimpleNamespace:
    resource = Resource()
    return SimpleNamespace(
        exchange=resource,
        markets=resource,
        orders=resource,
        portfolio=resource,
    )


def test_environment_endpoints_are_explicit_and_not_sdk_defaults() -> None:
    demo = KalshiGatewayConfig.for_environment(KalshiEnvironment.DEMO)
    production = KalshiGatewayConfig.for_environment(KalshiEnvironment.PRODUCTION)
    assert (demo.rest_base_url, demo.websocket_url) == (
        KALSHI_DEMO_API_BASE_URL,
        KALSHI_DEMO_WEBSOCKET_URL,
    )
    assert (production.rest_base_url, production.websocket_url) == (
        KALSHI_PUBLIC_API_BASE_URL,
        KALSHI_PRODUCTION_WEBSOCKET_URL,
    )


def test_endpoint_override_fails_closed() -> None:
    config = KalshiGatewayConfig(
        KalshiEnvironment.DEMO,
        KALSHI_PUBLIC_API_BASE_URL,
        KALSHI_DEMO_WEBSOCKET_URL,
    )
    with pytest.raises(KalshiGatewayError, match="endpoint mismatch"):
        config.validate()


def test_real_sdk_client_uses_live15_external_hosts() -> None:
    config = KalshiGatewayConfig.for_environment(KalshiEnvironment.PRODUCTION)
    sdk_client = build_sdk_client(config)
    try:
        assert sdk_client._config.base_url == KALSHI_PUBLIC_API_BASE_URL
        assert sdk_client._config.ws_base_url == KALSHI_PRODUCTION_WEBSOCKET_URL
        assert sdk_client.is_authenticated is False
    finally:
        sdk_client.close()


def test_credentials_load_only_explicit_files(tmp_path: Path) -> None:
    key_id = tmp_path / "id.txt"
    private_key = tmp_path / "private.key"
    key_id.write_text("masked-id", encoding="utf-8")
    private_key.write_text("not logged", encoding="utf-8")
    credentials = GatewayCredentials.from_files(key_id, private_key)
    assert credentials.api_key_id == "masked-id"
    assert credentials.private_key_path == private_key.resolve()
    assert "masked-id" not in repr(credentials)


def test_production_runtime_environment_is_explicit_and_strips_demo(tmp_path: Path) -> None:
    key_id = tmp_path / "production-id.txt"
    private_key = tmp_path / "production.key"
    key_id.write_text("production-key-id", encoding="utf-8")
    private_key.write_text("private-key", encoding="utf-8")
    settings = SimpleNamespace(
        kalshi_production_api_key_id_path=key_id,
        kalshi_production_private_key_path=private_key,
    )

    environment = production_runtime_environment(
        settings,
        base={
            "LIVE15_KALSHI_DEMO_API_KEY_ID": "demo-id",
            "LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH": "C:/demo.key",
            "LIVE15_ENABLE_KALSHI_PRODUCTION_WEBSOCKET": "false",
        },
    )

    assert "LIVE15_KALSHI_DEMO_API_KEY_ID" not in environment
    assert "LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH" not in environment
    assert environment["LIVE15_KALSHI_RUNTIME_ENVIRONMENT"] == "PRODUCTION"
    assert environment["LIVE15_ENABLE_KALSHI_PRODUCTION_WEBSOCKET"] == "true"
    assert environment["LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH"] == str(key_id.resolve())
    assert environment["LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH"] == str(private_key.resolve())


def test_production_runtime_environment_rejects_explicit_demo_mode(tmp_path: Path) -> None:
    key_id = tmp_path / "production-id.txt"
    private_key = tmp_path / "production.key"
    key_id.write_text("production-key-id", encoding="utf-8")
    private_key.write_text("private-key", encoding="utf-8")
    settings = SimpleNamespace(
        kalshi_production_api_key_id_path=key_id,
        kalshi_production_private_key_path=private_key,
    )

    with pytest.raises(KalshiGatewayError, match="KALSHI_DEMO"):
        production_runtime_environment(settings, base={"KALSHI_DEMO": "true"})


def test_production_credentials_never_use_implicit_filesystem_fallback() -> None:
    with pytest.raises(KalshiGatewayError, match="not configured"):
        production_credentials(SimpleNamespace())


def test_market_and_portfolio_reads_use_sdk_resources() -> None:
    sdk_client = client()
    market = KalshiMarketDataGateway(sdk_client)
    portfolio = KalshiPortfolioGateway(sdk_client)
    assert market.exchange_status() == "status"
    assert market.orderbook("TICKER", depth=10) == ("TICKER", 10)
    assert portfolio.balance(exchange_index=2).exchange_index == 2
    assert portfolio.positions(exchange_index=2) == ()
    assert portfolio.orders(ticker="TICKER", exchange_index=2) == ()
    assert portfolio.fills(ticker="TICKER", exchange_index=2) == ()


def test_order_get_uses_list_fallback_only_for_404() -> None:
    sdk_client = client()

    def missing(_order_id: str) -> None:
        error = RuntimeError("not found")
        error.status_code = 404  # type: ignore[attr-defined]
        raise error

    sdk_client.orders.get = missing
    wanted = SimpleNamespace(order_id="order-1")
    sdk_client.orders.list_all = lambda **_kwargs: iter((wanted,))
    assert (
        KalshiPortfolioGateway(sdk_client).order("order-1", ticker="TICKER", exchange_index=0)
        is wanted
    )


def test_execution_is_disabled_before_sdk_request_construction() -> None:
    sdk_client = client()
    gateway = KalshiExecutionGateway(
        sdk_client,
        environment=KalshiEnvironment.DEMO,
    )
    intent = GatewayOrderIntent(
        ticker="TICKER",
        client_order_id="client-1",
        side="bid",
        count=Decimal("1"),
        price=Decimal("0.01"),
        time_in_force="good_till_canceled",
        exchange_index=0,
    )
    with pytest.raises(KalshiWriteDisabledError, match="disabled"):
        gateway.create(intent)


def test_write_enabled_create_uses_current_v2_request_model() -> None:
    captured: dict[str, object] = {}

    class Orders(Resource):
        def create_v2(self, *, request: object) -> str:
            captured["request"] = request
            return "created"

    sdk_client = client()
    sdk_client.orders = Orders()
    gateway = KalshiExecutionGateway(
        sdk_client,
        environment=KalshiEnvironment.DEMO,
        write_enabled=True,
    )
    intent = GatewayOrderIntent(
        ticker="TICKER",
        client_order_id="client-1",
        side="bid",
        count=Decimal("1"),
        price=Decimal("0.01"),
        time_in_force="good_till_canceled",
        post_only=True,
        exchange_index=2,
    )
    assert gateway.create(intent) == "created"
    request = captured["request"]
    assert request.__class__.__name__ == "CreateOrderV2Request"
    assert request.model_dump(exclude_none=True, mode="json") == {
        "ticker": "TICKER",
        "client_order_id": "client-1",
        "side": "bid",
        "count": "1",
        "price": "0.01",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": True,
        "exchange_index": 2,
    }


def test_websocket_gateway_is_not_activated_for_recorder() -> None:
    assert KalshiWebSocketGateway.recorder_transport_activated is False


def test_sparse_production_snapshot_adds_only_the_unambiguous_empty_side() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "kalshi_ws"
        / "production_sparse_snapshot_sanitized.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))["snapshot"]
    normalized = _load_ws_json_with_sparse_snapshot_compat(json.dumps(fixture))
    assert normalized["msg"]["yes_dollars_fp"] == fixture["msg"]["yes_dollars_fp"]
    assert normalized["msg"]["no_dollars_fp"] == []

    from kalshi.ws.models.orderbook_delta import OrderbookSnapshotMessage

    parsed = OrderbookSnapshotMessage.model_validate(normalized)
    assert parsed.msg.no == {}
    assert parsed.msg.yes[Decimal("0.0010")] == Decimal("810.00")


def test_sparse_production_snapshot_adds_only_the_unambiguous_empty_yes_side() -> None:
    raw = json.dumps(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {
                "market_ticker": "TICKER",
                "market_id": "market",
                "no_dollars_fp": [["0.6000", "2.00"]],
            },
        }
    )
    normalized = _load_ws_json_with_sparse_snapshot_compat(raw)
    assert normalized["msg"]["yes_dollars_fp"] == []
    assert normalized["msg"]["no_dollars_fp"] == [["0.6000", "2.00"]]

    from kalshi.ws.models.orderbook_delta import OrderbookSnapshotMessage

    parsed = OrderbookSnapshotMessage.model_validate(normalized)
    assert parsed.msg.yes == {}
    assert parsed.msg.no[Decimal("0.6000")] == Decimal("2.00")


def test_sparse_snapshot_compat_remains_fail_closed_when_both_sides_are_absent() -> None:
    raw = json.dumps(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {"market_ticker": "TICKER", "market_id": "market"},
        }
    )
    normalized = _load_ws_json_with_sparse_snapshot_compat(raw)
    assert "yes_dollars_fp" not in normalized["msg"]
    assert "no_dollars_fp" not in normalized["msg"]

    from kalshi.ws.models.orderbook_delta import OrderbookSnapshotMessage

    with pytest.raises(ValidationError):
        OrderbookSnapshotMessage.model_validate(normalized)


def test_sparse_snapshot_compat_rejects_a_malformed_present_side() -> None:
    raw = json.dumps(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {
                "market_ticker": "TICKER",
                "market_id": "market",
                "yes_dollars_fp": "not-a-level-list",
            },
        }
    )
    normalized = _load_ws_json_with_sparse_snapshot_compat(raw)
    assert "no_dollars_fp" not in normalized["msg"]

    from kalshi.ws.models.orderbook_delta import OrderbookSnapshotMessage

    with pytest.raises(ValidationError):
        OrderbookSnapshotMessage.model_validate(normalized)


def test_production_build_installs_wire_normalization_without_pre_dispatch_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeConfig:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class FakeAuth:
        @staticmethod
        def from_key_path(_api_key_id: str, _private_key_path: Path) -> object:
            return object()

    class FakeWebSocket:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    auth_module = ModuleType("kalshi.auth")
    auth_module.KalshiAuth = FakeAuth  # type: ignore[attr-defined]
    ws_module = ModuleType("kalshi.ws")
    ws_module.KalshiWebSocket = FakeWebSocket  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kalshi.auth", auth_module)
    monkeypatch.setitem(sys.modules, "kalshi.ws", ws_module)
    monkeypatch.setattr(
        "live15_quant.kalshi_gateway.websocket._sdk_types",
        lambda: (object, FakeConfig),
    )
    private_key = tmp_path / "private.key"
    private_key.write_text("not-a-real-key", encoding="utf-8")
    gateway = KalshiWebSocketGateway(
        KalshiGatewayConfig.for_environment(KalshiEnvironment.PRODUCTION),
        GatewayCredentials(api_key_id="masked-id", private_key_path=private_key.resolve()),
    )

    gateway.build(capture_pre_dispatch=False)

    config = captured["config"]
    assert isinstance(config, FakeConfig)
    loader = config.kwargs.get("ws_json_loads")
    assert loader is _load_ws_json_with_sparse_snapshot_compat
    assert gateway._orderbook_feed is None
    normalized = loader(
        json.dumps(
            {
                "type": "orderbook_snapshot",
                "sid": 1,
                "seq": 1,
                "msg": {
                    "market_ticker": "TICKER",
                    "market_id": "market",
                    "yes_dollars_fp": [],
                },
            }
        )
    )

    from kalshi.ws.models.orderbook_delta import OrderbookSnapshotMessage

    assert OrderbookSnapshotMessage.model_validate(normalized).msg.no == {}


def test_sparse_snapshot_compat_does_not_rewrite_non_snapshot_frames() -> None:
    value = {"type": "orderbook_delta", "msg": {"side": "yes"}}
    assert _load_ws_json_with_sparse_snapshot_compat(json.dumps(value)) == value


@pytest.mark.asyncio
async def test_immutable_feed_snapshot_does_not_follow_sdk_book_mutation() -> None:
    raw = json.dumps(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {
                "market_ticker": "KXBTC15M-TEST",
                "market_id": "market-id",
                "yes_dollars_fp": [["0.8400", "326.00"]],
                "no_dollars_fp": [],
            },
        }
    )
    feed = _ImmutableOrderbookFeed(maxsize=2)
    sdk_input = feed.load(raw)
    immutable = await feed.__anext__()

    from kalshi.ws.models.orderbook_delta import OrderbookSnapshotMessage

    sdk_owned = OrderbookSnapshotMessage.model_validate(sdk_input)
    sdk_owned.msg.yes[Decimal("0.8400")] = Decimal("26.00")
    assert immutable.message.msg.yes[Decimal("0.8400")] == Decimal("326.00")


def test_immutable_feed_overflow_fails_closed() -> None:
    raw = json.dumps(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 2,
            "msg": {
                "market_ticker": "KXBTC15M-TEST",
                "market_id": "market-id",
                "price_dollars": "0.5000",
                "delta_fp": "1.00",
                "side": "yes",
            },
        }
    )
    feed = _ImmutableOrderbookFeed(maxsize=1)
    feed.load(raw)
    with pytest.raises(RuntimeError, match="immutable orderbook feed overflow"):
        feed.load(raw)
