from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from live15_quant.config import KALSHI_DEMO_API_BASE_URL, KALSHI_PUBLIC_API_BASE_URL, Settings
from live15_quant.providers.kalshi_demo import KalshiDemoCredentials
from live15_quant.providers.kalshi_demo_execution import (
    DemoBookSide,
    DemoOrderRequest,
    KalshiDemoAmbiguousWriteError,
    KalshiDemoExecutionClient,
    KalshiDemoExecutionError,
    KalshiDemoWriteRejectedError,
    authenticated_signature_message,
)


class FakeSigner:
    def __init__(self) -> None:
        self.messages: list[bytes] = []

    def sign(self, message: bytes) -> str:
        self.messages.append(message)
        return "redacted-signature"


class FakeResponse:
    def __init__(self, payload: object, url: str, status_code: int = 200) -> None:
        self.text = json.dumps(payload)
        self.url = url
        self.status_code = status_code


class FakeSession:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, Mapping[str, object] | None]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | None,
        json: Mapping[str, object] | None,
        timeout: float,
        headers: Mapping[str, str],
        allow_redirects: bool,
    ) -> FakeResponse:
        del params, timeout
        assert allow_redirects is False
        assert headers["KALSHI-ACCESS-KEY"] == "demo-key"
        assert "redacted-signature" == headers["KALSHI-ACCESS-SIGNATURE"]
        self.calls.append((method, url, json))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, FakeResponse)
        return response


def _client(tmp_path: Path, responses: list[object]):
    key = tmp_path / "demo.key"
    key.touch()
    session = FakeSession(responses)
    signer = FakeSigner()
    client = KalshiDemoExecutionClient(
        Settings(),
        KalshiDemoCredentials("demo-key", key),
        session=session,
        signer=signer,
        clock_ms=lambda: 1_700_000_000_123,
        utc_now=lambda: datetime(2026, 8, 23, tzinfo=UTC),
        repository_root=Path.cwd(),
    )
    return client, session, signer


def _order(*, status: str = "resting", fill: str = "0", remaining: str = "1"):
    return {
        "order_id": "order-1",
        "client_order_id": "client-1",
        "ticker": "KXBTC15M-TEST",
        "side": "yes",
        "status": status,
        "yes_price_dollars": "0.5100",
        "fill_count_fp": fill,
        "remaining_count_fp": remaining,
        "initial_count_fp": "1",
        "taker_fees_dollars": "0.0100",
        "maker_fees_dollars": "0",
    }


def test_demo_signature_supports_only_minimal_documented_methods() -> None:
    assert (
        authenticated_signature_message(
            "1700000000123", "POST", "/trade-api/v2/portfolio/events/orders?x=1"
        )
        == b"1700000000123POST/trade-api/v2/portfolio/events/orders"
    )
    with pytest.raises(KalshiDemoExecutionError, match="method"):
        authenticated_signature_message("1700000000123", "PATCH", "/trade-api/v2/x")


def test_demo_client_is_fixed_to_demo_and_builds_documented_v2_order(tmp_path: Path) -> None:
    response_url = f"{KALSHI_DEMO_API_BASE_URL}/portfolio/events/orders"
    client, session, signer = _client(
        tmp_path,
        [FakeResponse({"order": _order()}, response_url)],
    )

    result = client.create_order(
        DemoOrderRequest(
            "KXBTC15M-TEST", "client-1", DemoBookSide.BID, Decimal("1"), Decimal("0.51")
        )
    )

    assert result.order_id == "order-1"
    method, url, body = session.calls[0]
    assert method == "POST"
    assert url.startswith(KALSHI_DEMO_API_BASE_URL)
    assert KALSHI_PUBLIC_API_BASE_URL not in url
    assert body == {
        "ticker": "KXBTC15M-TEST",
        "client_order_id": "client-1",
        "side": "bid",
        "count": "1",
        "price": "0.51",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
    }
    assert signer.messages == [b"1700000000123POST/trade-api/v2/portfolio/events/orders"]


def test_compact_v2_ioc_create_ack_is_official_final_truth(tmp_path: Path) -> None:
    create_url = f"{KALSHI_DEMO_API_BASE_URL}/portfolio/events/orders"
    client, session, _ = _client(
        tmp_path,
        [
            FakeResponse(
                {
                    "order_id": "order-1",
                    "client_order_id": "client-1",
                    "fill_count": "0",
                    "remaining_count": "1",
                    "ts_ms": 1_700_000_000_124,
                },
                create_url,
                201,
            ),
        ],
    )
    result = client.create_order(
        DemoOrderRequest(
            "KXBTC15M-TEST", "client-1", DemoBookSide.BID, Decimal("1"), Decimal("0.51")
        )
    )
    assert result.state.value == "canceled"
    assert result.filled_count == 0
    assert result.remaining_count == 1
    assert [(method, url) for method, url, _ in session.calls] == [("POST", create_url)]


def test_compact_v2_ioc_partial_fill_preserves_official_fee_truth(tmp_path: Path) -> None:
    create_url = f"{KALSHI_DEMO_API_BASE_URL}/portfolio/events/orders"
    client, _, _ = _client(
        tmp_path,
        [
            FakeResponse(
                {
                    "order_id": "order-1",
                    "client_order_id": "client-1",
                    "fill_count": "0.25",
                    "remaining_count": "0.75",
                    "average_fill_price": "0.50",
                    "average_fee_paid": "0.02",
                    "ts_ms": 1_700_000_000_124,
                },
                create_url,
                201,
            )
        ],
    )

    result = client.create_order(
        DemoOrderRequest(
            "KXBTC15M-TEST", "client-1", DemoBookSide.BID, Decimal("1"), Decimal("0.51")
        )
    )

    assert result.state.value == "canceled"
    assert result.filled_count == Decimal("0.25")
    assert result.remaining_count == Decimal("0.75")
    assert result.fees == Decimal("0.005")


def test_compact_v2_ask_ack_keeps_acquired_no_cost(tmp_path: Path) -> None:
    create_url = f"{KALSHI_DEMO_API_BASE_URL}/portfolio/events/orders"
    client, session, _ = _client(
        tmp_path,
        [
            FakeResponse(
                {
                    "order_id": "order-ask",
                    "client_order_id": "client-ask",
                    "fill_count": "1",
                    "remaining_count": "0",
                    "average_fill_price": "0.60",
                    "average_fee_paid": "0.01",
                    "ts_ms": 1_700_000_000_124,
                },
                create_url,
                201,
            )
        ],
    )

    result = client.create_order(
        DemoOrderRequest(
            "KXBTC15M-TEST", "client-ask", DemoBookSide.ASK, Decimal("1"), Decimal("0.60")
        )
    )

    assert result.price == Decimal("0.40")
    assert session.calls[0][2]["side"] == "ask"  # type: ignore[index]
    assert session.calls[0][2]["price"] == "0.60"  # type: ignore[index]


def test_write_transport_failure_is_ambiguous_and_never_retried(tmp_path: Path) -> None:
    client, session, _ = _client(tmp_path, [requests.Timeout("contains secret")])

    with pytest.raises(KalshiDemoAmbiguousWriteError, match="reconcile") as error:
        client.create_order(
            DemoOrderRequest(
                "KXBTC15M-TEST",
                "client-1",
                DemoBookSide.BID,
                Decimal("1"),
                Decimal("0.51"),
            )
        )

    assert len(session.calls) == 1
    assert "secret" not in str(error.value)
    assert error.value.reason_code == "transport_failure"


@pytest.mark.parametrize("status_code", (409, 429, 500))
def test_inconclusive_write_http_status_requires_reconciliation(
    tmp_path: Path, status_code: int
) -> None:
    client, _, _ = _client(
        tmp_path,
        [
            FakeResponse(
                {"error": "transient"},
                f"{KALSHI_DEMO_API_BASE_URL}/portfolio/events/orders",
                status_code,
            )
        ],
    )
    with pytest.raises(KalshiDemoAmbiguousWriteError, match="reconcile") as error:
        client.create_order(
            DemoOrderRequest(
                "KXBTC15M-TEST",
                "client-1",
                DemoBookSide.BID,
                Decimal("1"),
                Decimal("0.51"),
            )
        )
    assert error.value.reason_code == f"http_{status_code}"


@pytest.mark.parametrize("status_code", (400, 401, 403, 422))
def test_conclusive_write_4xx_is_typed_and_never_retried(tmp_path: Path, status_code: int) -> None:
    client, session, _ = _client(
        tmp_path,
        [
            FakeResponse(
                {"error": "safe rejection"},
                f"{KALSHI_DEMO_API_BASE_URL}/portfolio/events/orders",
                status_code,
            )
        ],
    )
    with pytest.raises(KalshiDemoWriteRejectedError) as error:
        client.create_order(
            DemoOrderRequest(
                "KXBTC15M-TEST",
                "client-1",
                DemoBookSide.BID,
                Decimal("1"),
                Decimal("0.51"),
            )
        )
    assert error.value.reason_code == "http_4xx"
    assert len(session.calls) == 1


def test_malformed_successful_write_response_requires_reconciliation(tmp_path: Path) -> None:
    client, _, _ = _client(
        tmp_path,
        [
            FakeResponse(
                ["not", "an", "object"],
                f"{KALSHI_DEMO_API_BASE_URL}/portfolio/events/orders",
                201,
            )
        ],
    )
    with pytest.raises(KalshiDemoAmbiguousWriteError, match="reconcile"):
        client.create_order(
            DemoOrderRequest(
                "KXBTC15M-TEST",
                "client-1",
                DemoBookSide.BID,
                Decimal("1"),
                Decimal("0.51"),
            )
        )


def test_redirect_or_production_response_is_rejected(tmp_path: Path) -> None:
    client, _, _ = _client(
        tmp_path,
        [FakeResponse({"orders": []}, f"{KALSHI_PUBLIC_API_BASE_URL}/portfolio/orders")],
    )
    with pytest.raises(KalshiDemoExecutionError, match="unexpected endpoint"):
        client.orders()


def test_wrong_demo_credential_fails_loud_without_production_fallback(tmp_path: Path) -> None:
    client, session, _ = _client(
        tmp_path,
        [
            FakeResponse(
                {"error": "unauthorized"},
                f"{KALSHI_DEMO_API_BASE_URL}/portfolio/orders",
                401,
            )
        ],
    )
    with pytest.raises(KalshiDemoExecutionError, match="HTTP 401"):
        client.orders()
    assert len(session.calls) == 1
    assert session.calls[0][1].startswith(KALSHI_DEMO_API_BASE_URL)


def test_unknown_remote_order_state_fails_closed(tmp_path: Path) -> None:
    payload = _order(status="new_future_state")
    client, _, _ = _client(
        tmp_path,
        [FakeResponse({"orders": [payload]}, f"{KALSHI_DEMO_API_BASE_URL}/portfolio/orders")],
    )
    assert client.orders()[0].state.value == "reconciliation_required"


def test_paginated_remote_truth_is_rejected_until_complete(tmp_path: Path) -> None:
    client, _, _ = _client(
        tmp_path,
        [
            FakeResponse(
                {"orders": [_order()], "cursor": "next-page"},
                f"{KALSHI_DEMO_API_BASE_URL}/portfolio/orders",
            )
        ],
    )
    with pytest.raises(KalshiDemoExecutionError, match="incomplete"):
        client.orders()


def test_client_exposes_no_production_or_account_mutation_methods() -> None:
    names = set(dir(KalshiDemoExecutionClient))
    assert not names.intersection(
        {"withdraw", "deposit", "transfer", "amend_order", "create_api_key"}
    )


def test_demo_balance_and_positions_preserve_decimal_remote_truth(tmp_path: Path) -> None:
    client, _, _ = _client(
        tmp_path,
        [
            FakeResponse(
                {"balance": 1234, "portfolio_value": 1250, "updated_ts": 1_700_000_000},
                f"{KALSHI_DEMO_API_BASE_URL}/portfolio/balance",
            ),
            FakeResponse(
                {
                    "market_positions": [
                        {
                            "ticker": "KXBTC15M-TEST",
                            "position_fp": "1.00",
                            "market_exposure_dollars": "0.5100",
                            "realized_pnl_dollars": "0.123456",
                            "fees_paid_dollars": "0.0100",
                            "resting_orders_count": 1,
                            "last_updated_ts": "2026-08-23T00:00:00Z",
                        }
                    ]
                },
                f"{KALSHI_DEMO_API_BASE_URL}/portfolio/positions",
            ),
        ],
    )

    balance = client.balance()
    position = client.positions()[0]

    assert balance.buying_power == Decimal("12.34")
    assert balance.portfolio_value == Decimal("12.5")
    assert position.realized_pnl == Decimal("0.123456")
    assert position.quantity == Decimal("1.00")


def test_official_exchange_and_market_reads_are_typed_and_demo_only(tmp_path: Path) -> None:
    client, session, _ = _client(
        tmp_path,
        [
            FakeResponse(
                {
                    "exchange_active": True,
                    "trading_active": True,
                    "exchange_estimated_resume_time": None,
                },
                f"{KALSHI_DEMO_API_BASE_URL}/exchange/status",
            ),
            FakeResponse(
                {
                    "market": {
                        "ticker": "KXBTC15M-TEST",
                        "status": "active",
                        "result": None,
                        "close_time": "2026-08-23T00:15:00Z",
                    }
                },
                f"{KALSHI_DEMO_API_BASE_URL}/markets/KXBTC15M-TEST",
            ),
        ],
    )

    exchange = client.exchange_status()
    market = client.market("KXBTC15M-TEST")

    assert exchange.exchange_active is True
    assert exchange.trading_active is True
    assert exchange.received_at.isoformat() == "2026-08-23T00:00:00+00:00"
    assert market.ticker == "KXBTC15M-TEST"
    assert market.status == "active"
    assert market.close_time is not None
    assert market.close_time.isoformat() == "2026-08-23T00:15:00+00:00"
    assert all(call[1].startswith(KALSHI_DEMO_API_BASE_URL) for call in session.calls)


def test_market_read_blocks_path_traversal_before_network(tmp_path: Path) -> None:
    client, session, _ = _client(tmp_path, [])
    with pytest.raises(KalshiDemoExecutionError, match="ticker"):
        client.market("../../portfolio/balance")
    assert session.calls == []
