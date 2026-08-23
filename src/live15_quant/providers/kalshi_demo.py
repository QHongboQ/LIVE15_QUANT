"""Authenticated GET-only connectivity audit for the official Kalshi Demo API."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from live15_quant.config import KALSHI_DEMO_API_BASE_URL, KALSHI_DEMO_WEBSOCKET_URL, Settings

_GET_PATHS = frozenset(
    {
        "/portfolio/balance",
        "/markets",
        "/portfolio/positions",
        "/portfolio/orders",
        "/portfolio/fills",
    }
)


class KalshiDemoAuditError(RuntimeError):
    """Raised when the safe Demo connectivity audit cannot be completed."""


class HttpResponse(Protocol):
    text: str
    url: str
    status_code: int


class GetOnlySession(Protocol):
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, object] | None,
        timeout: float,
        headers: Mapping[str, str],
        allow_redirects: bool,
    ) -> HttpResponse: ...


class RequestSigner(Protocol):
    def sign(self, message: bytes) -> str: ...


@dataclass(frozen=True, slots=True, repr=False)
class KalshiDemoCredentials:
    """References to Demo-only credentials; secret material is never represented here."""

    api_key_id: str
    private_key_path: Path

    def validate(self, repository_root: Path | None = None) -> None:
        if not self.api_key_id.strip():
            raise KalshiDemoAuditError("Kalshi Demo API key ID is empty")
        if not self.private_key_path.is_absolute():
            raise KalshiDemoAuditError("Kalshi Demo private key path must be absolute")
        key_path = self.private_key_path.resolve()
        if repository_root is not None and key_path.is_relative_to(repository_root.resolve()):
            raise KalshiDemoAuditError(
                "Kalshi Demo private key must be stored outside the repository"
            )
        if key_path.suffix.lower() not in {".key", ".pem"}:
            raise KalshiDemoAuditError("Kalshi Demo private key must be a .key or .pem file")
        if not key_path.is_file():
            raise KalshiDemoAuditError("Kalshi Demo private key file does not exist")


@dataclass(frozen=True, slots=True)
class KalshiApiCapabilities:
    """Officially documented API boundary; this client exposes none of the write methods."""

    authenticated_balance: bool = True
    market_discovery: bool = True
    positions: bool = True
    orders_and_status: bool = True
    fills: bool = True
    documented_create_order_v2: bool = True
    documented_cancel_order_v2: bool = True
    websocket_market_data: bool = True
    websocket_private_updates: bool = True
    client_write_operations: bool = False


@dataclass(frozen=True, slots=True)
class KalshiDemoAuditResult:
    environment: str
    rest_endpoint: str
    websocket_endpoint: str
    authenticated: bool
    balance_dollars: Decimal | None
    portfolio_value_cents: int | None
    market_count: int
    sample_market_tickers: tuple[str, ...]
    positions_readable: bool
    orders_readable: bool
    fills_readable: bool
    audited_at: datetime
    capabilities: KalshiApiCapabilities


def canonical_signature_message(timestamp_ms: str, method: str, path: str) -> bytes:
    """Build Kalshi's documented signature message and reject every write method."""

    normalized_method = method.upper()
    if normalized_method != "GET":
        raise KalshiDemoAuditError("connectivity audit permits GET only")
    path_without_query = path.split("?", maxsplit=1)[0]
    if not path_without_query.startswith("/trade-api/v2/"):
        raise KalshiDemoAuditError("signature path is outside the Kalshi Trade API v2")
    if not timestamp_ms.isdecimal():
        raise KalshiDemoAuditError("signature timestamp must be milliseconds")
    return f"{timestamp_ms}{normalized_method}{path_without_query}".encode()


class FileRsaPssSigner:
    """Load a local Kalshi key into memory and produce documented RSA-PSS signatures."""

    def __init__(self, private_key_path: Path) -> None:
        try:
            key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
        except (OSError, TypeError, ValueError):
            # Do not chain filesystem/parser exceptions: they may disclose the private path.
            raise KalshiDemoAuditError("Kalshi Demo private key could not be loaded") from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise KalshiDemoAuditError("Kalshi Demo private key is not an RSA private key")
        self._key = key

    def sign(self, message: bytes) -> str:
        signature = self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")


def _retrying_get_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _object_payload(response: HttpResponse) -> Mapping[str, Any]:
    try:
        payload = json.loads(response.text, parse_float=Decimal, parse_int=Decimal)
    except (json.JSONDecodeError, TypeError):
        # Authenticated response bodies must never become exception-chain state.
        raise KalshiDemoAuditError("malformed Kalshi Demo JSON payload") from None
    if not isinstance(payload, Mapping):
        raise KalshiDemoAuditError("Kalshi Demo payload must be an object")
    return payload


def _list(payload: Mapping[str, Any], field: str) -> list[Any]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise KalshiDemoAuditError(f"Kalshi Demo {field} must be a list")
    return value


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise KalshiDemoAuditError(f"malformed Kalshi Demo {field}")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise KalshiDemoAuditError(f"malformed Kalshi Demo {field}") from error
    if not result.is_finite():
        raise KalshiDemoAuditError(f"malformed Kalshi Demo {field}")
    return result


def _integer(value: object, field: str) -> int:
    parsed = _decimal(value, field)
    if parsed != parsed.to_integral_value():
        raise KalshiDemoAuditError(f"malformed Kalshi Demo {field}")
    return int(parsed)


class KalshiDemoReadOnlyClient:
    """Fixed-host audit client with an explicit GET allowlist and no trading methods."""

    def __init__(
        self,
        settings: Settings,
        credentials: KalshiDemoCredentials,
        *,
        session: GetOnlySession | None = None,
        signer: RequestSigner | None = None,
        clock_ms: Callable[[], int] | None = None,
        repository_root: Path | None = None,
    ) -> None:
        credentials.validate(repository_root or Path.cwd())
        self._settings = settings
        self._credentials = credentials
        self._owned_session = _retrying_get_session() if session is None else None
        self._session = self._owned_session or session
        self._signer = signer or FileRsaPssSigner(credentials.private_key_path)
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def close(self) -> None:
        if self._owned_session is not None:
            self._owned_session.close()

    def __enter__(self) -> KalshiDemoReadOnlyClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _get(self, path: str, params: Mapping[str, object] | None = None) -> Mapping[str, Any]:
        if path not in _GET_PATHS:
            raise KalshiDemoAuditError("endpoint is not permitted by the Demo audit allowlist")
        url = f"{KALSHI_DEMO_API_BASE_URL}{path}"
        timestamp = str(self._clock_ms())
        full_path = urlsplit(url).path
        signature = self._signer.sign(canonical_signature_message(timestamp, "GET", full_path))
        try:
            response = self._session.get(
                url,
                params=params,
                timeout=self._settings.request_timeout_seconds,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "LIVE15_QUANT/0.5 demo-read-only-audit",
                    "KALSHI-ACCESS-KEY": self._credentials.api_key_id,
                    "KALSHI-ACCESS-TIMESTAMP": timestamp,
                    "KALSHI-ACCESS-SIGNATURE": signature,
                },
                allow_redirects=False,
            )
        except requests.RequestException:
            # Do not chain a requests exception: its request object contains auth headers.
            raise KalshiDemoAuditError("Kalshi Demo GET failed") from None
        if 300 <= response.status_code < 400:
            raise KalshiDemoAuditError("Kalshi Demo request attempted a redirect")
        if not 200 <= response.status_code < 300:
            raise KalshiDemoAuditError(f"Kalshi Demo GET returned HTTP {response.status_code}")
        if not response.url.startswith(f"{KALSHI_DEMO_API_BASE_URL}/"):
            raise KalshiDemoAuditError("Kalshi Demo response came from an unexpected endpoint")
        return _object_payload(response)

    def audit(self) -> KalshiDemoAuditResult:
        """Perform only documented authenticated reads; never create, amend, or cancel orders."""

        balance = self._get("/portfolio/balance")
        # Official V2 ``balance`` is available buying power in integer cents.
        # Retain the historical dollars field only as an explicit compatibility path.
        if "balance" in balance:
            balance_dollars = _decimal(balance.get("balance"), "balance") / Decimal(100)
        else:
            balance_dollars = _decimal(balance.get("balance_dollars"), "balance_dollars")
        portfolio_value_cents = _integer(balance.get("portfolio_value"), "portfolio_value")
        markets = _list(
            self._get("/markets", {"status": "open", "limit": 10}),
            "markets",
        )
        tickers: list[str] = []
        for market in markets:
            if not isinstance(market, Mapping) or not isinstance(market.get("ticker"), str):
                raise KalshiDemoAuditError("malformed Kalshi Demo market")
            tickers.append(market["ticker"])
        _list(self._get("/portfolio/positions", {"limit": 1}), "market_positions")
        _list(self._get("/portfolio/orders", {"limit": 1}), "orders")
        _list(self._get("/portfolio/fills", {"limit": 1}), "fills")
        return KalshiDemoAuditResult(
            environment="demo",
            rest_endpoint=KALSHI_DEMO_API_BASE_URL,
            websocket_endpoint=KALSHI_DEMO_WEBSOCKET_URL,
            authenticated=True,
            balance_dollars=balance_dollars,
            portfolio_value_cents=portfolio_value_cents,
            market_count=len(markets),
            sample_market_tickers=tuple(tickers),
            positions_readable=True,
            orders_readable=True,
            fills_readable=True,
            audited_at=datetime.now(UTC),
            capabilities=KalshiApiCapabilities(),
        )
