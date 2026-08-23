"""Official Kalshi Demo-only order transport.

This module is intentionally separate from the production read-only adapter.  It has a
fixed Demo host, never follows redirects, never retries a write request, and never places
credentials or authenticated response bodies in exceptions.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests

from live15_quant.config import KALSHI_DEMO_API_BASE_URL, Settings
from live15_quant.providers.kalshi_demo import (
    FileRsaPssSigner,
    KalshiDemoCredentials,
    RequestSigner,
)

_ALLOWED_READ_PATHS = frozenset(
    {
        "/exchange/status",
        "/portfolio/balance",
        "/portfolio/positions",
        "/portfolio/orders",
        "/portfolio/fills",
    }
)
_CREATE_ORDER_PATH = "/portfolio/events/orders"
_TICKER_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,199}")


class KalshiDemoExecutionError(RuntimeError):
    """Safe, typed Demo execution failure."""


class KalshiDemoAmbiguousWriteError(KalshiDemoExecutionError):
    """A write may have reached Demo and must be reconciled before any retry."""

    def __init__(self, message: str, *, reason_code: str = "write_outcome_ambiguous") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class KalshiDemoWriteRejectedError(KalshiDemoExecutionError):
    """A conclusive Demo 4xx rejection; the request must not be retried unchanged."""

    def __init__(self, message: str, *, reason_code: str = "http_4xx") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class DemoBookSide(StrEnum):
    BID = "bid"
    ASK = "ask"


class DemoTimeInForce(StrEnum):
    FILL_OR_KILL = "fill_or_kill"
    IMMEDIATE_OR_CANCEL = "immediate_or_cancel"
    GOOD_TILL_CANCELED = "good_till_canceled"


class DemoRemoteOrderState(StrEnum):
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class HttpResponse(Protocol):
    text: str
    url: str
    status_code: int


class DemoSession(Protocol):
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
    ) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class DemoOrderRequest:
    ticker: str
    client_order_id: str
    side: DemoBookSide
    count: Decimal
    price: Decimal
    time_in_force: DemoTimeInForce = DemoTimeInForce.IMMEDIATE_OR_CANCEL

    def __post_init__(self) -> None:
        if not self.ticker or not self.client_order_id:
            raise ValueError("Demo order identifiers must not be empty")
        if not self.count.is_finite() or self.count <= 0:
            raise ValueError("Demo order count must be positive")
        if not self.price.is_finite() or not Decimal(0) < self.price < Decimal(1):
            raise ValueError("Demo order price must be strictly between zero and one")


@dataclass(frozen=True, slots=True)
class DemoRemoteOrder:
    order_id: str
    client_order_id: str
    ticker: str
    state: DemoRemoteOrderState
    initial_count: Decimal
    filled_count: Decimal
    remaining_count: Decimal
    price: Decimal
    fees: Decimal
    raw_status: str

    def __post_init__(self) -> None:
        if not all((self.order_id, self.client_order_id, self.ticker, self.raw_status)):
            raise ValueError("Demo remote order identifiers must not be empty")
        values = (
            self.initial_count,
            self.filled_count,
            self.remaining_count,
            self.price,
            self.fees,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("Demo remote order values must be finite")
        if (
            self.initial_count <= 0
            or self.filled_count < 0
            or self.remaining_count < 0
            or self.filled_count + self.remaining_count > self.initial_count
            or not Decimal(0) <= self.price <= Decimal(1)
            or self.fees < 0
        ):
            raise ValueError("Demo remote order values are impossible")


@dataclass(frozen=True, slots=True)
class DemoRemoteFill:
    fill_id: str
    order_id: str
    ticker: str
    count: Decimal
    price: Decimal
    fee: Decimal
    created_time: str

    def __post_init__(self) -> None:
        if not all((self.fill_id, self.order_id, self.ticker, self.created_time)):
            raise ValueError("Demo fill identifiers must not be empty")
        if (
            any(not value.is_finite() for value in (self.count, self.price, self.fee))
            or self.count <= 0
            or not Decimal(0) <= self.price <= Decimal(1)
            or self.fee < 0
        ):
            raise ValueError("Demo fill values are impossible")


@dataclass(frozen=True, slots=True)
class DemoAccountSnapshot:
    buying_power: Decimal
    portfolio_value: Decimal
    updated_timestamp: int

    def __post_init__(self) -> None:
        if (
            any(not value.is_finite() for value in (self.buying_power, self.portfolio_value))
            or self.buying_power < 0
            or self.portfolio_value < 0
            or self.updated_timestamp < 0
        ):
            raise ValueError("Demo account values are impossible")


@dataclass(frozen=True, slots=True)
class DemoExchangeStatus:
    """Official Demo exchange availability observed immediately before execution."""

    exchange_active: bool
    trading_active: bool
    estimated_resume_time: str | None
    received_at: datetime

    def __post_init__(self) -> None:
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("Demo exchange status receive timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class DemoMarketTruth:
    """Official Demo market identity/lifecycle truth for an execution-time gate."""

    ticker: str
    status: str
    result: str | None
    close_time: datetime | None
    received_at: datetime

    def __post_init__(self) -> None:
        if not _TICKER_PATTERN.fullmatch(self.ticker):
            raise ValueError("Demo market ticker is malformed")
        if not self.status:
            raise ValueError("Demo market status must not be empty")
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("Demo market receive timestamp must be timezone-aware")
        if self.close_time is not None and (
            self.close_time.tzinfo is None or self.close_time.utcoffset() is None
        ):
            raise ValueError("Demo market close timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class DemoRemotePosition:
    ticker: str
    quantity: Decimal
    exposure: Decimal
    realized_pnl: Decimal
    fees: Decimal
    resting_orders: int
    updated_timestamp: str

    def __post_init__(self) -> None:
        if not self.ticker or not self.updated_timestamp:
            raise ValueError("Demo position identity/timestamp must not be empty")
        if (
            any(
                not value.is_finite()
                for value in (self.quantity, self.exposure, self.realized_pnl, self.fees)
            )
            or self.exposure < 0
            or self.fees < 0
            or self.resting_orders < 0
        ):
            raise ValueError("Demo position values are impossible")


def authenticated_signature_message(timestamp_ms: str, method: str, path: str) -> bytes:
    """Build the documented signature message for the minimal Demo method allowlist."""

    normalized_method = method.upper()
    if normalized_method not in {"GET", "POST", "DELETE"}:
        raise KalshiDemoExecutionError("HTTP method is outside the Demo execution allowlist")
    path_without_query = path.split("?", maxsplit=1)[0]
    if not path_without_query.startswith("/trade-api/v2/"):
        raise KalshiDemoExecutionError("signature path is outside the Kalshi Trade API v2")
    if not timestamp_ms.isdecimal():
        raise KalshiDemoExecutionError("signature timestamp must be milliseconds")
    return f"{timestamp_ms}{normalized_method}{path_without_query}".encode()


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise KalshiDemoExecutionError(f"malformed Demo {field}")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise KalshiDemoExecutionError(f"malformed Demo {field}") from None
    if not result.is_finite():
        raise KalshiDemoExecutionError(f"malformed Demo {field}")
    return result


def _object(response: HttpResponse) -> Mapping[str, Any]:
    try:
        payload = json.loads(response.text, parse_float=Decimal, parse_int=Decimal)
    except (json.JSONDecodeError, TypeError):
        raise KalshiDemoExecutionError("malformed Kalshi Demo JSON payload") from None
    if not isinstance(payload, Mapping):
        raise KalshiDemoExecutionError("Kalshi Demo payload must be an object")
    return payload


def _timestamp_or_none(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise KalshiDemoExecutionError(f"malformed Demo {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise KalshiDemoExecutionError(f"malformed Demo {field}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise KalshiDemoExecutionError(f"malformed Demo {field}")
    return parsed.astimezone(UTC)


def _complete_page(payload: Mapping[str, Any], field: str) -> list[Any]:
    values = payload.get(field)
    cursor = payload.get("cursor", "")
    if not isinstance(values, list) or cursor not in {None, ""}:
        raise KalshiDemoExecutionError(f"Demo {field} read is incomplete or malformed")
    return values


def _order_state(status: str, filled: Decimal, remaining: Decimal) -> DemoRemoteOrderState:
    if status in {"resting", "open", "pending"}:
        return DemoRemoteOrderState.PARTIALLY_FILLED if filled > 0 else DemoRemoteOrderState.OPEN
    if status in {"executed", "filled"}:
        return DemoRemoteOrderState.FILLED
    if status in {"canceled", "cancelled"}:
        return DemoRemoteOrderState.CANCELED
    if status == "rejected":
        return DemoRemoteOrderState.REJECTED
    if remaining == 0 and filled > 0:
        return DemoRemoteOrderState.FILLED
    return DemoRemoteOrderState.RECONCILIATION_REQUIRED


def _parse_order(value: object) -> DemoRemoteOrder:
    if not isinstance(value, Mapping):
        raise KalshiDemoExecutionError("malformed Demo order")
    required_strings = ("order_id", "client_order_id", "ticker")
    if any(not isinstance(value.get(field), str) or not value[field] for field in required_strings):
        raise KalshiDemoExecutionError("malformed Demo order identifiers")
    initial = _decimal(value.get("initial_count_fp", value.get("initial_count")), "initial_count")
    filled = _decimal(value.get("fill_count_fp", value.get("fill_count", 0)), "fill_count")
    remaining = _decimal(
        value.get("remaining_count_fp", value.get("remaining_count", initial - filled)),
        "remaining_count",
    )
    if initial <= 0 or filled < 0 or remaining < 0 or filled + remaining > initial:
        raise KalshiDemoExecutionError("impossible Demo order quantities")
    # V2 event orders are quoted on one YES book: bid buys YES, while ask sells
    # YES (economically buying NO). The portfolio read shape also carries the
    # legacy outcome-side field, so prefer the explicit V2 book side whenever it
    # is present. ``DemoRemoteOrder.price`` is the acquired contract's cost,
    # which is the value needed by exposure and reconciliation logic.
    book_side = str(value.get("book_side", ""))
    if book_side not in {"bid", "ask"}:
        action = str(value.get("action", ""))
        book_side = "ask" if action == "sell" else "bid"
    price_field = "yes_price_dollars" if book_side == "bid" else "no_price_dollars"
    price = _decimal(value.get(price_field, value.get("price", 0)), "order price")
    fees = _decimal(
        value.get(
            "fees_dollars",
            _decimal(value.get("taker_fees_dollars", 0), "taker fees")
            + _decimal(value.get("maker_fees_dollars", 0), "maker fees"),
        ),
        "fees",
    )
    status = value.get("status")
    if not isinstance(status, str):
        raise KalshiDemoExecutionError("malformed Demo order status")
    return DemoRemoteOrder(
        order_id=value["order_id"],
        client_order_id=value["client_order_id"],
        ticker=value["ticker"],
        state=_order_state(status, filled, remaining),
        initial_count=initial,
        filled_count=filled,
        remaining_count=remaining,
        price=price,
        fees=fees,
        raw_status=status,
    )


def _parse_compact_create_ack(
    value: Mapping[str, Any], request: DemoOrderRequest
) -> DemoRemoteOrder:
    """Parse documented V2 create truth without inventing a follow-up order state.

    For IOC/FOK, the V2 response's fill/remaining counts are the official final result;
    an entirely unfilled IOC may not remain queryable through the open-order collection.
    """

    order_id = value.get("order_id")
    client_order_id = value.get("client_order_id", request.client_order_id)
    if (
        not isinstance(order_id, str)
        or not order_id
        or not isinstance(client_order_id, str)
        or client_order_id != request.client_order_id
    ):
        raise KalshiDemoExecutionError("malformed Demo create-order response identity")
    filled = _decimal(value.get("fill_count"), "create fill_count")
    remaining = _decimal(value.get("remaining_count"), "create remaining_count")
    if filled < 0 or remaining < 0 or filled + remaining != request.count:
        raise KalshiDemoExecutionError("impossible Demo create-order quantities")
    average_fee = value.get("average_fee_paid")
    if filled > 0 and average_fee is None:
        raise KalshiDemoExecutionError("malformed Demo create-order fee")
    fee = Decimal(0) if average_fee is None else _decimal(average_fee, "average fee") * filled
    if request.time_in_force in {
        DemoTimeInForce.IMMEDIATE_OR_CANCEL,
        DemoTimeInForce.FILL_OR_KILL,
    }:
        state = DemoRemoteOrderState.FILLED if remaining == 0 else DemoRemoteOrderState.CANCELED
        raw_status = f"{request.time_in_force.value}_complete"
    else:
        state = DemoRemoteOrderState.PARTIALLY_FILLED if filled > 0 else DemoRemoteOrderState.OPEN
        raw_status = "create_ack_open"
    return DemoRemoteOrder(
        order_id=order_id,
        client_order_id=client_order_id,
        ticker=request.ticker,
        state=state,
        initial_count=request.count,
        filled_count=filled,
        remaining_count=remaining,
        price=(request.price if request.side is DemoBookSide.BID else Decimal(1) - request.price),
        fees=fee,
        raw_status=raw_status,
    )


class KalshiDemoExecutionClient:
    """Fixed-host official Demo client with no production fallback and no write retry."""

    def __init__(
        self,
        settings: Settings,
        credentials: KalshiDemoCredentials,
        *,
        session: DemoSession | None = None,
        signer: RequestSigner | None = None,
        clock_ms: Callable[[], int] | None = None,
        utc_now: Callable[[], datetime] | None = None,
        repository_root: Path | None = None,
    ) -> None:
        credentials.validate(repository_root or Path.cwd())
        self._settings = settings
        self._credentials = credentials
        self._owned_session = requests.Session() if session is None else None
        self._session = self._owned_session or session
        self._signer = signer or FileRsaPssSigner(credentials.private_key_path)
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._utc_now = utc_now or (lambda: datetime.now(UTC))

    def close(self) -> None:
        if self._owned_session is not None:
            self._owned_session.close()

    def __enter__(self) -> KalshiDemoExecutionClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        body: Mapping[str, object] | None = None,
    ) -> Mapping[str, Any]:
        method = method.upper()
        allowed = path in _ALLOWED_READ_PATHS and method == "GET"
        allowed = allowed or (
            path.startswith("/markets/")
            and path.count("/") == 2
            and bool(_TICKER_PATTERN.fullmatch(path.removeprefix("/markets/")))
            and method == "GET"
        )
        allowed = allowed or (path == _CREATE_ORDER_PATH and method == "POST")
        allowed = allowed or (
            path.startswith("/portfolio/events/orders/")
            and path.count("/") == 4
            and method == "DELETE"
        )
        allowed = allowed or (
            path.startswith("/portfolio/orders/") and path.count("/") == 3 and method == "GET"
        )
        if not allowed:
            raise KalshiDemoExecutionError("endpoint is outside the Demo execution allowlist")
        url = f"{KALSHI_DEMO_API_BASE_URL}{path}"
        timestamp = str(self._clock_ms())
        signature_path = urlsplit(url).path
        signature = self._signer.sign(
            authenticated_signature_message(timestamp, method, signature_path)
        )
        try:
            response = self._session.request(
                method,
                url,
                params=params,
                json=body,
                timeout=self._settings.request_timeout_seconds,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "LIVE15_QUANT/0.6 demo-execution",
                    "KALSHI-ACCESS-KEY": self._credentials.api_key_id,
                    "KALSHI-ACCESS-TIMESTAMP": timestamp,
                    "KALSHI-ACCESS-SIGNATURE": signature,
                },
                allow_redirects=False,
            )
        except requests.RequestException:
            if method in {"POST", "DELETE"}:
                raise KalshiDemoAmbiguousWriteError(
                    "Kalshi Demo write outcome is unknown; reconcile remote truth before retry",
                    reason_code="transport_failure",
                ) from None
            raise KalshiDemoExecutionError("Kalshi Demo read failed") from None
        if 300 <= response.status_code < 400:
            raise KalshiDemoExecutionError("Kalshi Demo request attempted a redirect")
        if not response.url.startswith(f"{KALSHI_DEMO_API_BASE_URL}/"):
            raise KalshiDemoExecutionError("Kalshi Demo response came from an unexpected endpoint")
        if not 200 <= response.status_code < 300:
            if method in {"POST", "DELETE"} and response.status_code not in {400, 401, 403, 422}:
                raise KalshiDemoAmbiguousWriteError(
                    "Kalshi Demo write response is not conclusive; "
                    "reconcile remote truth before retry",
                    reason_code=f"http_{response.status_code}",
                )
            if method in {"POST", "DELETE"}:
                raise KalshiDemoWriteRejectedError(
                    f"Kalshi Demo {method} was conclusively rejected",
                    reason_code="http_4xx",
                )
            raise KalshiDemoExecutionError(
                f"Kalshi Demo {method} returned HTTP {response.status_code}"
            )
        try:
            return _object(response)
        except KalshiDemoExecutionError:
            if method in {"POST", "DELETE"}:
                raise KalshiDemoAmbiguousWriteError(
                    "Kalshi Demo write response is malformed; reconcile remote truth before retry",
                    reason_code="malformed_response",
                ) from None
            raise

    def balance(self) -> DemoAccountSnapshot:
        value = self._request("GET", "/portfolio/balance")
        balance = _decimal(value.get("balance"), "balance") / Decimal(100)
        portfolio_value = _decimal(value.get("portfolio_value"), "portfolio_value") / Decimal(100)
        updated = _decimal(value.get("updated_ts"), "updated_ts")
        if updated != updated.to_integral_value():
            raise KalshiDemoExecutionError("malformed Demo updated_ts")
        return DemoAccountSnapshot(balance, portfolio_value, int(updated))

    def exchange_status(self) -> DemoExchangeStatus:
        """Read official exchange/trading availability; never infer it locally."""

        value = self._request("GET", "/exchange/status")
        exchange_active = value.get("exchange_active")
        trading_active = value.get("trading_active")
        resume = value.get("exchange_estimated_resume_time")
        if not isinstance(exchange_active, bool) or not isinstance(trading_active, bool):
            raise KalshiDemoExecutionError("malformed Demo exchange status")
        if resume is not None and not isinstance(resume, str):
            raise KalshiDemoExecutionError("malformed Demo exchange resume time")
        return DemoExchangeStatus(
            exchange_active=exchange_active,
            trading_active=trading_active,
            estimated_resume_time=resume,
            received_at=self._utc_now(),
        )

    def market(self, ticker: str) -> DemoMarketTruth:
        """Read exact official market identity/lifecycle truth for a pre-trade gate."""

        if not _TICKER_PATTERN.fullmatch(ticker):
            raise KalshiDemoExecutionError("malformed Demo market ticker")
        wrapper = self._request("GET", f"/markets/{ticker}")
        value = wrapper.get("market")
        if not isinstance(value, Mapping):
            raise KalshiDemoExecutionError("malformed Demo market payload")
        remote_ticker = value.get("ticker")
        status = value.get("status")
        result = value.get("result")
        if (
            not isinstance(remote_ticker, str)
            or not _TICKER_PATTERN.fullmatch(remote_ticker)
            or not isinstance(status, str)
            or not status
            or (result is not None and not isinstance(result, str))
        ):
            raise KalshiDemoExecutionError("malformed Demo market truth")
        return DemoMarketTruth(
            ticker=remote_ticker,
            status=status,
            result=result,
            close_time=_timestamp_or_none(value.get("close_time"), "market close_time"),
            received_at=self._utc_now(),
        )

    def positions(self) -> tuple[DemoRemotePosition, ...]:
        values = _complete_page(
            self._request("GET", "/portfolio/positions", params={"limit": 1000}),
            "market_positions",
        )
        if any(not isinstance(value, Mapping) for value in values):
            raise KalshiDemoExecutionError("malformed Demo positions")
        result: list[DemoRemotePosition] = []
        for value in values:
            assert isinstance(value, Mapping)
            ticker = value.get("ticker")
            updated = value.get("last_updated_ts", "")
            resting = _decimal(value.get("resting_orders_count", 0), "resting orders")
            if (
                not isinstance(ticker, str)
                or not ticker
                or not isinstance(updated, str)
                or resting != resting.to_integral_value()
            ):
                raise KalshiDemoExecutionError("malformed Demo position")
            result.append(
                DemoRemotePosition(
                    ticker=ticker,
                    quantity=_decimal(value.get("position_fp", 0), "position"),
                    exposure=_decimal(value.get("market_exposure_dollars", 0), "exposure"),
                    realized_pnl=_decimal(value.get("realized_pnl_dollars", 0), "realized PnL"),
                    fees=_decimal(value.get("fees_paid_dollars", 0), "position fees"),
                    resting_orders=int(resting),
                    updated_timestamp=updated,
                )
            )
        return tuple(result)

    def orders(self) -> tuple[DemoRemoteOrder, ...]:
        values = _complete_page(
            self._request("GET", "/portfolio/orders", params={"limit": 1000}), "orders"
        )
        return tuple(_parse_order(value) for value in values)

    def open_orders(self) -> tuple[DemoRemoteOrder, ...]:
        return tuple(
            order
            for order in self.orders()
            if order.state in {DemoRemoteOrderState.OPEN, DemoRemoteOrderState.PARTIALLY_FILLED}
        )

    def find_order_by_client_id(self, client_order_id: str) -> DemoRemoteOrder | None:
        matches = [order for order in self.orders() if order.client_order_id == client_order_id]
        if len(matches) > 1:
            raise KalshiDemoExecutionError("duplicate remote client order ID")
        return matches[0] if matches else None

    def order(self, order_id: str) -> DemoRemoteOrder:
        value = self._request("GET", f"/portfolio/orders/{order_id}").get("order")
        return _parse_order(value)

    def fills(self, *, order_id: str | None = None) -> tuple[DemoRemoteFill, ...]:
        params: dict[str, object] = {"limit": 1000}
        if order_id is not None:
            params["order_id"] = order_id
        values = _complete_page(self._request("GET", "/portfolio/fills", params=params), "fills")
        result: list[DemoRemoteFill] = []
        for value in values:
            if not isinstance(value, Mapping):
                raise KalshiDemoExecutionError("malformed Demo fill")
            identifiers = (value.get("fill_id"), value.get("order_id"), value.get("ticker"))
            if any(not isinstance(item, str) or not item for item in identifiers):
                raise KalshiDemoExecutionError("malformed Demo fill identifiers")
            side = str(value.get("side", value.get("outcome_side", "yes")))
            price_field = "yes_price_dollars" if side == "yes" else "no_price_dollars"
            created = value.get("created_time", "")
            if not isinstance(created, str):
                raise KalshiDemoExecutionError("malformed Demo fill timestamp")
            result.append(
                DemoRemoteFill(
                    fill_id=identifiers[0],
                    order_id=identifiers[1],
                    ticker=identifiers[2],
                    count=_decimal(value.get("count_fp", value.get("count")), "fill count"),
                    price=_decimal(value.get(price_field), "fill price"),
                    fee=_decimal(value.get("fee_cost", 0), "fill fee"),
                    created_time=created,
                )
            )
        return tuple(result)

    def create_order(self, request: DemoOrderRequest) -> DemoRemoteOrder:
        payload = self._request(
            "POST",
            _CREATE_ORDER_PATH,
            body={
                "ticker": request.ticker,
                "client_order_id": request.client_order_id,
                "side": request.side.value,
                "count": str(request.count),
                "price": str(request.price),
                "time_in_force": request.time_in_force.value,
                "self_trade_prevention_type": "taker_at_cross",
                "cancel_order_on_pause": True,
            },
        )
        value = payload.get("order", payload)
        if isinstance(value, Mapping) and all(
            field in value for field in ("client_order_id", "ticker", "status", "initial_count_fp")
        ):
            return _parse_order(value)
        if not isinstance(value, Mapping):
            raise KalshiDemoExecutionError("malformed Demo create-order response")
        try:
            return _parse_compact_create_ack(value, request)
        except KalshiDemoExecutionError:
            raise KalshiDemoAmbiguousWriteError(
                "Kalshi Demo order ACK could not be interpreted; reconcile before retry",
                reason_code="compact_ack_invalid",
            ) from None

    def cancel_order(self, order_id: str) -> Mapping[str, Any]:
        return self._request("DELETE", f"/portfolio/events/orders/{order_id}")
