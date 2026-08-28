"""SDK WebSocket construction without weakening LIVE15 book provenance semantics."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from live15_quant.kalshi_gateway.client import (
    GatewayCredentials,
    KalshiGatewayConfig,
    _sdk_types,
)


def _load_ws_json_with_sparse_snapshot_compat(raw: bytes | str) -> Any:
    """Parse a frame and normalize one unambiguously omitted empty book side.

    Production omits an orderbook side when that side has no levels, while
    ``kalshi-sdk==12.0.0`` requires both ``*_dollars_fp`` fields. Add only one
    absent side when the opposite side is a valid list. Two absent sides,
    malformed present data, and non-snapshot frames remain strict/fail-closed.
    """

    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("type") != "orderbook_snapshot":
        return value
    message = value.get("msg")
    if not isinstance(message, dict):
        return value
    yes_present = "yes_dollars_fp" in message or "yes" in message
    no_present = "no_dollars_fp" in message or "no" in message
    if yes_present == no_present:
        return value
    present_name = "yes_dollars_fp" if yes_present else "no_dollars_fp"
    alias_name = "yes" if yes_present else "no"
    if not isinstance(message.get(present_name, message.get(alias_name)), list):
        return value
    message["no_dollars_fp" if yes_present else "yes_dollars_fp"] = []
    return value


@dataclass(frozen=True, slots=True)
class GatewayWireDiagnostic:
    diagnostic_kind: str
    wire_type: str
    market_ticker: str | None
    event_ticker: str | None
    subscription_id: int | None
    sequence: int | None
    received_at: datetime
    series_ticker: str | None = None
    exchange_index: int | None = None


@dataclass(frozen=True, slots=True)
class GatewayReceivedMessage:
    message: Any
    received_at: datetime


class _ImmutableOrderbookFeed:
    """Capture validated wire events before the SDK mutates snapshot maps.

    ``kalshi-sdk==12.0.0`` intentionally identity-adopts snapshot ``yes`` and
    ``no`` dictionaries into its local orderbook manager.  Raw
    ``subscribe_orderbook_delta`` consumers therefore observe those same
    dictionaries changing before a slow consumer can read the queued
    snapshot.  The SDK also discards data frames that arrive before a normal
    subscribe acknowledgement.  This gateway feed makes one independently
    validated SDK message at JSON decode time, before either behavior can
    alter or discard the authoritative wire event.
    """

    def __init__(self, *, maxsize: int) -> None:
        if maxsize < 1:
            raise ValueError("immutable orderbook feed maxsize must be positive")
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._diagnostics: asyncio.Queue[GatewayWireDiagnostic] = asyncio.Queue(maxsize=1_000)

    def load(self, raw: bytes | str) -> Any:
        received_at = datetime.now(UTC)
        value = _load_ws_json_with_sparse_snapshot_compat(raw)
        if not isinstance(value, dict):
            return value
        kind = value.get("type")
        if isinstance(kind, str) and "lifecycle" in kind.lower() and kind != "market_lifecycle_v2":
            payload = value.get("msg")
            safe_payload = payload if isinstance(payload, dict) else {}
            diagnostic = GatewayWireDiagnostic(
                diagnostic_kind=(
                    "EVENT_LIFECYCLE" if kind == "event_lifecycle" else "UNKNOWN_LIFECYCLE"
                ),
                wire_type=kind[:120],
                market_ticker=(
                    str(safe_payload["market_ticker"])
                    if isinstance(safe_payload.get("market_ticker"), str)
                    else None
                ),
                event_ticker=(
                    str(safe_payload["event_ticker"])
                    if isinstance(safe_payload.get("event_ticker"), str)
                    else None
                ),
                subscription_id=(value.get("sid") if isinstance(value.get("sid"), int) else None),
                sequence=(value.get("seq") if isinstance(value.get("seq"), int) else None),
                received_at=received_at,
                series_ticker=(
                    str(safe_payload["series_ticker"])
                    if isinstance(safe_payload.get("series_ticker"), str)
                    else None
                ),
                exchange_index=(
                    safe_payload.get("exchange_index")
                    if isinstance(safe_payload.get("exchange_index"), int)
                    and not isinstance(safe_payload.get("exchange_index"), bool)
                    else None
                ),
            )
            try:
                self._diagnostics.put_nowait(diagnostic)
            except asyncio.QueueFull as error:
                raise RuntimeError("LIVE15 SDK wire diagnostic feed overflow") from error
        if kind not in {"orderbook_snapshot", "orderbook_delta"}:
            return value
        try:
            from kalshi.ws.models.orderbook_delta import (
                OrderbookDeltaMessage,
                OrderbookSnapshotMessage,
            )
        except ImportError as error:  # pragma: no cover - deployment failure path
            raise RuntimeError("kalshi-sdk==12.0.0 orderbook models are unavailable") from error
        model = OrderbookSnapshotMessage if kind == "orderbook_snapshot" else OrderbookDeltaMessage
        # Pydantic creates fresh Decimal maps from the wire lists.  This
        # message is never handed to the SDK OrderbookManager, so its snapshot
        # sides remain immutable from LIVE15's point of view.
        try:
            message = model.model_validate(value)
        except Exception:
            payload = value.get("msg")
            safe_payload = payload if isinstance(payload, dict) else {}
            diagnostic = GatewayWireDiagnostic(
                diagnostic_kind="MALFORMED_ORDERBOOK",
                wire_type=str(kind)[:120],
                market_ticker=(
                    str(safe_payload["market_ticker"])
                    if isinstance(safe_payload.get("market_ticker"), str)
                    else None
                ),
                event_ticker=None,
                subscription_id=(value.get("sid") if isinstance(value.get("sid"), int) else None),
                sequence=(value.get("seq") if isinstance(value.get("seq"), int) else None),
                received_at=received_at,
            )
            try:
                self._diagnostics.put_nowait(diagnostic)
            except asyncio.QueueFull as error:
                raise RuntimeError("LIVE15 SDK wire diagnostic feed overflow") from error
            raise
        try:
            self._queue.put_nowait(GatewayReceivedMessage(message, received_at))
        except asyncio.QueueFull as error:
            # Sequence integrity is more important than availability.  Raising
            # tears down this bounded session so the SDK reconnect path can
            # obtain a fresh authoritative snapshot; no frame is silently lost.
            raise RuntimeError("LIVE15 immutable orderbook feed overflow") from error
        return value

    def __aiter__(self) -> _ImmutableOrderbookFeed:
        return self

    async def __anext__(self) -> Any:
        return await self._queue.get()

    async def next_diagnostic(self) -> GatewayWireDiagnostic:
        return await self._diagnostics.get()


class _WireDiagnosticStream:
    def __init__(self, feed: _ImmutableOrderbookFeed) -> None:
        self._feed = feed

    def __aiter__(self) -> _WireDiagnosticStream:
        return self

    async def __anext__(self) -> GatewayWireDiagnostic:
        return await self._feed.next_diagnostic()


class KalshiWebSocketGateway:
    """Build SDK transport; Recorder activation remains an explicit separate decision."""

    recorder_transport_activated = False

    def __init__(self, config: KalshiGatewayConfig, credentials: GatewayCredentials) -> None:
        config.validate()
        credentials.validate()
        self._config = config
        self._credentials = credentials
        self._orderbook_feed: _ImmutableOrderbookFeed | None = None

    def immutable_orderbook_stream(self, *, maxsize: int = 20_000) -> _ImmutableOrderbookFeed:
        """Return the one pre-dispatch orderbook stream for this gateway."""

        if self._orderbook_feed is None:
            self._orderbook_feed = _ImmutableOrderbookFeed(maxsize=maxsize)
        return self._orderbook_feed

    def wire_diagnostic_stream(self) -> _WireDiagnosticStream:
        """Return sanitized unknown-lifecycle diagnostics captured before SDK dispatch."""

        return _WireDiagnosticStream(self.immutable_orderbook_stream())

    def build(
        self,
        *,
        on_state_change: Callable[[Any, Any], Awaitable[None]] | None = None,
        on_error: Callable[[Any], Awaitable[None]] | None = None,
        capture_pre_dispatch: bool = False,
    ) -> Any:
        _, config_type = _sdk_types()
        try:
            from kalshi.auth import KalshiAuth
            from kalshi.ws import KalshiWebSocket
        except ImportError as error:  # pragma: no cover - deployment failure path
            raise RuntimeError("kalshi-sdk==12.0.0 WebSocket support is unavailable") from error
        config_kwargs: dict[str, Any] = {
            "base_url": self._config.rest_base_url,
            "ws_base_url": self._config.websocket_url,
            "timeout": self._config.timeout_seconds,
            "max_retries": self._config.read_retries,
            "allow_unknown_host": True,
        }
        # Every SDK decode path must tolerate exactly one legitimately empty
        # orderbook side.  This normalizes only the wire representation before
        # SDK validation; it does not capture, route, or own any SDK stream.
        config_kwargs["ws_json_loads"] = _load_ws_json_with_sparse_snapshot_compat
        if capture_pre_dispatch:
            # Shadow diagnostics alone may inspect wire frames before SDK
            # dispatch.  The production Recorder must consume only typed,
            # SID-routed SDK streams and never install this parallel feed.
            config_kwargs["ws_json_loads"] = self.immutable_orderbook_stream().load
        sdk_config = config_type(**config_kwargs)
        auth = KalshiAuth.from_key_path(
            self._credentials.api_key_id,
            self._credentials.private_key_path,
        )
        return KalshiWebSocket(
            auth=auth,
            config=sdk_config,
            on_state_change=on_state_change,
            on_error=on_error,
        )
