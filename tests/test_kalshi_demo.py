from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from live15_quant.config import KALSHI_DEMO_API_BASE_URL, Settings
from live15_quant.providers.kalshi_demo import (
    KalshiDemoAuditError,
    KalshiDemoCredentials,
    KalshiDemoReadOnlyClient,
    canonical_signature_message,
)


class FakeSigner:
    def __init__(self) -> None:
        self.messages: list[bytes] = []

    def sign(self, message: bytes) -> str:
        self.messages.append(message)
        return "test-signature"


class FakeResponse:
    def __init__(self, payload: object, url: str, status_code: int = 200) -> None:
        self.text = json.dumps(payload)
        self.url = url
        self.status_code = status_code


class FakeGetOnlySession:
    def __init__(self, payloads: Mapping[str, object]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, Mapping[str, object] | None, Mapping[str, str]]] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, object] | None,
        timeout: float,
        headers: Mapping[str, str],
        allow_redirects: bool,
    ) -> FakeResponse:
        del timeout
        assert allow_redirects is False
        path = url.removeprefix(KALSHI_DEMO_API_BASE_URL)
        self.calls.append((url, params, headers))
        return FakeResponse(self.payloads[path], url)


def _client(tmp_path: Path, payloads: Mapping[str, object]):
    key_path = tmp_path / "demo.key"
    key_path.touch()
    credentials = KalshiDemoCredentials("not-a-real-key", key_path)
    session = FakeGetOnlySession(payloads)
    signer = FakeSigner()
    client = KalshiDemoReadOnlyClient(
        Settings(),
        credentials,
        session=session,
        signer=signer,
        clock_ms=lambda: 1_700_000_000_123,
        repository_root=Path.cwd(),
    )
    return client, session, signer


def _valid_payloads() -> dict[str, object]:
    return {
        "/portfolio/balance": {
            "balance": 1234,
            "balance_dollars": "12.3400",
            "portfolio_value": 1250,
            "updated_ts": 1_700_000_000,
        },
        "/markets": {"markets": [{"ticker": "KXBTC15M-EXAMPLE"}], "cursor": ""},
        "/portfolio/positions": {"market_positions": [], "event_positions": [], "cursor": ""},
        "/portfolio/orders": {"orders": [], "cursor": ""},
        "/portfolio/fills": {"fills": [], "cursor": ""},
    }


def test_signature_message_excludes_query_and_rejects_write_methods() -> None:
    message = canonical_signature_message(
        "1700000000123", "get", "/trade-api/v2/portfolio/orders?limit=1"
    )

    assert message == b"1700000000123GET/trade-api/v2/portfolio/orders"
    with pytest.raises(KalshiDemoAuditError, match="GET only"):
        canonical_signature_message("1700000000123", "POST", "/trade-api/v2/portfolio/orders")


def test_credentials_hide_values_and_require_external_absolute_key(tmp_path: Path) -> None:
    credentials = KalshiDemoCredentials("sensitive-id", tmp_path / "demo.key")

    assert "sensitive-id" not in repr(credentials)
    with pytest.raises(KalshiDemoAuditError, match="does not exist"):
        credentials.validate(Path.cwd())
    with pytest.raises(KalshiDemoAuditError, match="absolute"):
        KalshiDemoCredentials("id", Path("relative.key")).validate()


def test_demo_audit_reads_only_allowlisted_resources(tmp_path: Path) -> None:
    client, session, signer = _client(tmp_path, _valid_payloads())

    result = client.audit()

    assert result.environment == "demo"
    assert result.authenticated is True
    assert str(result.balance_dollars) == "12.3400"
    assert result.portfolio_value_cents == 1250
    assert result.sample_market_tickers == ("KXBTC15M-EXAMPLE",)
    assert result.capabilities.documented_create_order_v2 is True
    assert result.capabilities.documented_cancel_order_v2 is True
    assert result.capabilities.client_write_operations is False
    assert [call[0].removeprefix(KALSHI_DEMO_API_BASE_URL) for call in session.calls] == [
        "/portfolio/balance",
        "/markets",
        "/portfolio/positions",
        "/portfolio/orders",
        "/portfolio/fills",
    ]
    assert all(call[2]["KALSHI-ACCESS-KEY"] == "not-a-real-key" for call in session.calls)
    assert all(message.startswith(b"1700000000123GET") for message in signer.messages)
    assert not hasattr(client, "post")
    assert not hasattr(client, "create_order")
    assert not hasattr(client, "cancel_order")


def test_demo_audit_preserves_decimal_and_rejects_malformed_payload(tmp_path: Path) -> None:
    payloads = _valid_payloads()
    payloads["/portfolio/balance"] = {
        "balance_dollars": "0.123456789012345678",
        "portfolio_value": 1,
    }
    client, _, _ = _client(tmp_path, payloads)
    assert str(client.audit().balance_dollars) == "0.123456789012345678"

    malformed = _valid_payloads()
    malformed["/portfolio/orders"] = {"orders": None}
    client, _, _ = _client(tmp_path, malformed)
    with pytest.raises(KalshiDemoAuditError, match="orders must be a list"):
        client.audit()

    malformed_value = _valid_payloads()
    malformed_value["/portfolio/balance"] = {
        "balance_dollars": "1.0000",
        "portfolio_value": "1.5",
    }
    client, _, _ = _client(tmp_path, malformed_value)
    with pytest.raises(KalshiDemoAuditError, match="portfolio_value"):
        client.audit()


def test_demo_audit_rejects_key_inside_repository(tmp_path: Path) -> None:
    del tmp_path
    key_path = Path.cwd() / "forbidden-demo.key"
    credentials = KalshiDemoCredentials("id", key_path)

    with pytest.raises(KalshiDemoAuditError, match="outside the repository"):
        credentials.validate(Path.cwd())
