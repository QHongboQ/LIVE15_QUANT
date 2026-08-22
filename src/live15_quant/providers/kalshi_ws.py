"""Official authenticated, read-only Kalshi production WebSocket adapter."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from live15_quant.config import KALSHI_PRODUCTION_WEBSOCKET_URL
from live15_quant.kalshi_ws import (
    KalshiOrderBookDelta,
    KalshiOrderBookSnapshot,
    KalshiServerMessage,
    KalshiSubscriptionCommand,
    KalshiTickerUpdate,
    KalshiWsPayloadError,
    KalshiWsPayloadIssue,
    KalshiWsProtocolNotice,
    KalshiWsRuntimeState,
    parse_kalshi_server_message,
    subscribe_command,
)

if TYPE_CHECKING:
    from live15_quant.config import Settings

KALSHI_PRODUCTION_WS_PATH = "/trade-api/ws/v2"
_SAFE_CONTEXT_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)
_SAFE_SCHEMA_KEYS = frozenset(
    {
        "type",
        "id",
        "sid",
        "seq",
        "msg",
        "channel",
        "market_ticker",
        "market_tickers",
        "market_id",
        "yes_dollars_fp",
        "no_dollars_fp",
        "price_dollars",
        "yes_bid_dollars",
        "yes_ask_dollars",
        "volume_fp",
        "delta_fp",
        "side",
        "ts",
        "ts_ms",
        "code",
    }
)


def _safe_payload_context(payload: object) -> str:
    """Return only allow-listed market-data identity fields, never raw payload data."""

    if not isinstance(payload, Mapping):
        return "type=unknown"
    message = payload.get("msg")
    message_mapping = message if isinstance(message, Mapping) else {}

    def token(value: object, fallback: str = "unknown") -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            return fallback
        return value if set(value) <= _SAFE_CONTEXT_CHARACTERS else fallback

    def integer(value: object) -> str:
        if isinstance(value, bool):
            return "unknown"
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            return "unknown"
        return str(parsed) if parsed >= 0 and str(parsed) == str(value) else "unknown"

    return ",".join(
        (
            f"type={token(payload.get('type'))}",
            f"channel={token(message_mapping.get('channel'))}",
            f"ticker={token(message_mapping.get('market_ticker'))}",
            f"sid={integer(payload.get('sid'))}",
            f"seq={integer(payload.get('seq'))}",
        )
    )


def _payload_shape(payload: object) -> tuple[tuple[str, ...], str]:
    """Describe JSON structure without retaining market values or authentication data."""

    if not isinstance(payload, Mapping):
        keys: tuple[str, ...] = ()
        shape = type(payload).__name__
    else:
        top_names = {key if key in _SAFE_SCHEMA_KEYS else "<other>" for key in payload}
        top = tuple(sorted(top_names))
        message = payload.get("msg")
        msg_keys = ()
        if isinstance(message, Mapping):
            msg_names = {
                key if key in _SAFE_SCHEMA_KEYS else "<other>"
                for key in message
                if isinstance(key, str)
            }
            msg_keys = tuple(sorted(msg_names))
        keys = tuple(f"top:{key}" for key in top) + tuple(f"msg:{key}" for key in msg_keys)
        shape = "|".join(keys) or "mapping"
    return keys, hashlib.sha256(shape.encode("utf-8")).hexdigest()[:16]


def _safe_positive_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 1 and str(parsed) == str(value) else None


def _safe_identity(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    return value if set(value) <= _SAFE_CONTEXT_CHARACTERS else None


def _classify_payload_error(
    payload: object,
    error: KalshiWsPayloadError,
    *,
    connection_id: str,
    received: datetime,
    parsed: datetime,
) -> KalshiWsPayloadIssue | KalshiWsProtocolNotice | None:
    """Localize one safe market-data failure; return None for global protocol damage."""

    if not isinstance(payload, Mapping):
        return None
    kind = _safe_identity(payload.get("type"))
    message = payload.get("msg")
    message_mapping = message if isinstance(message, Mapping) else {}
    keys, shape_hash = _payload_shape(payload)
    channel = _safe_identity(message_mapping.get("channel"))
    if kind not in {"orderbook_snapshot", "orderbook_delta", "ticker"}:
        if payload.get("seq") is None:
            return KalshiWsProtocolNotice(
                connection_id=connection_id,
                message_type=kind or "unknown",
                channel=channel,
                socket_received_timestamp=received,
                parse_timestamp=parsed,
                payload_shape_hash=shape_hash,
            )
    subscription_id = _safe_positive_integer(payload.get("sid"))
    if subscription_id is None or kind is None:
        return None
    reason = str(error)
    return KalshiWsPayloadIssue(
        connection_id=connection_id,
        message_type=kind,
        channel=channel,
        subscription_id=subscription_id,
        sequence=_safe_positive_integer(payload.get("seq")),
        ticker=_safe_identity(message_mapping.get("market_ticker")),
        parser_stage="data_payload",
        reason=reason[:120],
        schema_keys=keys,
        payload_shape_hash=shape_hash,
        affects_orderbook=kind != "ticker",
        socket_received_timestamp=received,
        parse_timestamp=parsed,
    )


class KalshiReadOnlyWsError(RuntimeError):
    """Sanitized adapter error that never retains authentication state."""


class KalshiWsSigner(Protocol):
    def sign(self, message: bytes) -> str: ...


class KalshiWsConnection(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class KalshiProductionCredentialFiles:
    """External Production credential references; secret contents are never represented."""

    api_key_id_path: Path
    private_key_path: Path

    def validate(self, repository_root: Path | None = None) -> None:
        root = (repository_root or Path.cwd()).resolve()
        for path, label in (
            (self.api_key_id_path, "API key ID"),
            (self.private_key_path, "private key"),
        ):
            if not path.is_absolute() or path.resolve().is_relative_to(root):
                raise KalshiReadOnlyWsError(
                    f"Kalshi Production {label} must be an absolute file outside the repository"
                )
            if not path.is_file():
                raise KalshiReadOnlyWsError(f"Kalshi Production {label} file is unavailable")
        if self.private_key_path.suffix.lower() not in {".key", ".pem"}:
            raise KalshiReadOnlyWsError("Kalshi Production private key must be a .key or .pem file")

    def key_id(self) -> str:
        try:
            value = self.api_key_id_path.read_text(encoding="utf-8").strip()
        except OSError:
            raise KalshiReadOnlyWsError(
                "Kalshi Production API key ID could not be loaded"
            ) from None
        if not value or any(character.isspace() for character in value):
            raise KalshiReadOnlyWsError("Kalshi Production API key ID is malformed")
        return value


class KalshiProductionRsaPssSigner:
    """Kalshi's documented RSA-PSS/SHA-256 signer with sanitized failures."""

    def __init__(self, path: Path) -> None:
        try:
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        except (OSError, TypeError, ValueError):
            raise KalshiReadOnlyWsError(
                "Kalshi Production private key could not be loaded"
            ) from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise KalshiReadOnlyWsError("Kalshi Production private key is not RSA")
        self._key = key

    def sign(self, message: bytes) -> str:
        signature = self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")


def websocket_signature_message(timestamp_ms: str) -> bytes:
    if not timestamp_ms.isdecimal():
        raise KalshiReadOnlyWsError("Kalshi WebSocket signature timestamp must be milliseconds")
    return f"{timestamp_ms}GET{KALSHI_PRODUCTION_WS_PATH}".encode()


@dataclass(slots=True)
class KalshiWsDiagnostics:
    connection_attempts: int = 0
    reconnects: int = 0
    transport_errors: int = 0
    messages: int = 0
    payload_issues: int = 0
    protocol_notices: int = 0
    protocol_reconnects: int = 0
    connected_at: datetime | None = None
    last_disconnect_at: datetime | None = None
    last_message_received_at: datetime | None = None
    last_reconnect_duration_seconds: float | None = None
    receive_queue_high_watermark: int = 0
    receive_queue_capacity: int = 0
    receive_queue_depth: int = 0
    receive_queue_enqueued: int = 0
    receive_queue_dequeued: int = 0
    receive_queue_full_waits: int = 0
    receive_queue_dropped: int = 0
    receive_queue_max_backlog_seconds: float = 0.0
    receive_queue_above_50_seconds: float = 0.0
    receive_queue_above_75_seconds: float = 0.0
    receive_queue_above_90_seconds: float = 0.0
    transport_state: KalshiWsRuntimeState = KalshiWsRuntimeState.CONNECTING


Connector = Callable[..., Any]
Sleeper = Callable[[float], Awaitable[None]]


class KalshiProductionReadOnlyWebSocket:
    """Production market-data adapter; deliberately exposes no write/account method."""

    def __init__(
        self,
        credentials: KalshiProductionCredentialFiles,
        *,
        connector: Connector = connect,
        signer: KalshiWsSigner | None = None,
        endpoint: str = KALSHI_PRODUCTION_WEBSOCKET_URL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        monotonic: Callable[[], float] = time.monotonic,
        perf_counter_ns: Callable[[], int] = time.perf_counter_ns,
        sleeper: Sleeper = asyncio.sleep,
        connection_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        read_timeout_seconds: float = 45,
        base_backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 15,
        receive_queue_capacity: int = 8192,
        repository_root: Path | None = None,
    ) -> None:
        if endpoint != KALSHI_PRODUCTION_WEBSOCKET_URL:
            raise KalshiReadOnlyWsError("Kalshi WebSocket endpoint must be documented Production")
        if (
            read_timeout_seconds <= 0
            or base_backoff_seconds <= 0
            or max_backoff_seconds <= 0
            or receive_queue_capacity < 1
        ):
            raise ValueError("Kalshi WebSocket timeout/backoff must be positive")
        credentials.validate(repository_root)
        self._credentials = credentials
        self._connector = connector
        self._signer = signer or KalshiProductionRsaPssSigner(credentials.private_key_path)
        self._endpoint = endpoint
        self._clock = clock
        self._clock_ms = clock_ms
        self._monotonic = monotonic
        self._perf_counter_ns = perf_counter_ns
        self._sleeper = sleeper
        self._connection_id_factory = connection_id_factory
        self._read_timeout = read_timeout_seconds
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._receive_queue_capacity = receive_queue_capacity
        self._closed = False
        self._active: KalshiWsConnection | None = None
        self._desired_tickers: tuple[str, ...] = ()
        self.diagnostics = KalshiWsDiagnostics(receive_queue_capacity=receive_queue_capacity)
        self._queue_last_observed_ns: int | None = None
        self._queue_last_depth = 0
        self._queue_backlog_started_ns: int | None = None

    def _observe_queue(self, depth: int, observed_ns: int) -> None:
        """Measure bounded-queue pressure with the local monotonic clock."""

        previous = self._queue_last_observed_ns
        if previous is not None:
            elapsed = max(0, observed_ns - previous) / 1_000_000_000
            capacity = self._receive_queue_capacity
            ratio = self._queue_last_depth / capacity
            if ratio >= 0.5:
                self.diagnostics.receive_queue_above_50_seconds += elapsed
            if ratio >= 0.75:
                self.diagnostics.receive_queue_above_75_seconds += elapsed
            if ratio >= 0.9:
                self.diagnostics.receive_queue_above_90_seconds += elapsed
        if depth > 0 and self._queue_backlog_started_ns is None:
            self._queue_backlog_started_ns = observed_ns
        elif depth == 0 and self._queue_backlog_started_ns is not None:
            duration = max(0, observed_ns - self._queue_backlog_started_ns) / 1_000_000_000
            self.diagnostics.receive_queue_max_backlog_seconds = max(
                self.diagnostics.receive_queue_max_backlog_seconds, duration
            )
            self._queue_backlog_started_ns = None
        self._queue_last_observed_ns = observed_ns
        self._queue_last_depth = depth
        self.diagnostics.receive_queue_depth = depth
        self.diagnostics.receive_queue_high_watermark = max(
            self.diagnostics.receive_queue_high_watermark, depth
        )

    async def _receive_frames(
        self,
        websocket: KalshiWsConnection,
        queue: asyncio.Queue[tuple[str | bytes, datetime, int, datetime, int] | BaseException],
    ) -> None:
        """Drain the socket promptly so consumer persistence cannot distort receive time."""

        try:
            while not self._closed:
                raw = await websocket.recv()
                received = self._clock()
                received_monotonic_ns = self._perf_counter_ns()
                while True:
                    enqueued = self._clock()
                    enqueued_monotonic_ns = self._perf_counter_ns()
                    try:
                        queue.put_nowait(
                            (
                                raw,
                                received,
                                received_monotonic_ns,
                                enqueued,
                                enqueued_monotonic_ns,
                            )
                        )
                        break
                    except asyncio.QueueFull:
                        self.diagnostics.receive_queue_full_waits += 1
                        self._observe_queue(queue.qsize(), self._perf_counter_ns())
                        await self._sleeper(0)
                self.diagnostics.receive_queue_enqueued += 1
                self._observe_queue(queue.qsize(), enqueued_monotonic_ns)
        except asyncio.CancelledError:
            raise
        except (ConnectionClosed, OSError, TimeoutError) as error:
            await queue.put(error)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        repository_root: Path | None = None,
        **overrides: Any,
    ) -> KalshiProductionReadOnlyWebSocket:
        if not settings.enable_kalshi_production_websocket:
            raise KalshiReadOnlyWsError("Kalshi Production WebSocket is not enabled")
        if (
            settings.kalshi_production_api_key_id_path is None
            or settings.kalshi_production_private_key_path is None
        ):
            raise KalshiReadOnlyWsError("Kalshi Production credential files are not configured")
        return cls(
            KalshiProductionCredentialFiles(
                settings.kalshi_production_api_key_id_path,
                settings.kalshi_production_private_key_path,
            ),
            read_timeout_seconds=settings.kalshi_websocket_read_timeout_seconds,
            receive_queue_capacity=settings.kalshi_websocket_queue_capacity,
            repository_root=repository_root,
            **overrides,
        )

    def _headers(self) -> Mapping[str, str]:
        timestamp = str(self._clock_ms())
        signature = self._signer.sign(websocket_signature_message(timestamp))
        return {
            "KALSHI-ACCESS-KEY": self._credentials.key_id(),
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    async def messages(self, tickers: Sequence[str]) -> AsyncIterator[KalshiServerMessage]:
        self.set_reconnect_tickers(tickers)
        failures = 0
        consecutive_global_payload_failures = 0
        while not self._closed:
            self.diagnostics.connection_attempts += 1
            self.diagnostics.transport_state = (
                KalshiWsRuntimeState.CONNECTING
                if self.diagnostics.connection_attempts == 1
                else KalshiWsRuntimeState.RECONNECTING
            )
            started = self._monotonic()
            connection_id = self._connection_id_factory()
            try:
                headers = self._headers()
                async with self._connector(
                    self._endpoint,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=10,
                    close_timeout=5,
                    max_queue=4096,
                ) as websocket:
                    headers = {}  # discard authentication header references immediately
                    self._active = websocket
                    self.diagnostics.transport_state = KalshiWsRuntimeState.WAITING_SNAPSHOT
                    self.diagnostics.connected_at = self._clock()
                    self.diagnostics.last_reconnect_duration_seconds = self._monotonic() - started
                    await websocket.send(subscribe_command(1, self._desired_tickers).payload)
                    failures = 0
                    queue: asyncio.Queue[
                        tuple[str | bytes, datetime, int, datetime, int] | BaseException
                    ] = asyncio.Queue(maxsize=self._receive_queue_capacity)
                    self._observe_queue(0, self._perf_counter_ns())
                    receiver = asyncio.create_task(self._receive_frames(websocket, queue))
                    try:
                        while not self._closed:
                            queued = await asyncio.wait_for(queue.get(), timeout=self._read_timeout)
                            self.diagnostics.receive_queue_dequeued += 1
                            self._observe_queue(queue.qsize(), self._perf_counter_ns())
                            if isinstance(queued, BaseException):
                                raise queued
                            (
                                raw,
                                received,
                                received_monotonic_ns,
                                enqueued,
                                enqueued_monotonic_ns,
                            ) = queued
                            try:
                                decoded = json.loads(raw, parse_float=Decimal, parse_int=Decimal)
                            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                                raise KalshiWsPayloadError(
                                    "parser_stage=json_decode,reason=malformed_json,"
                                    "type=unknown,channel=unknown,ticker=unknown,sid=unknown,"
                                    "seq=unknown,keys=none,shape=unavailable"
                                ) from None
                            parsed = self._clock()
                            try:
                                message = parse_kalshi_server_message(
                                    decoded,
                                    connection_id=connection_id,
                                    socket_received_timestamp=received,
                                    parse_timestamp=parsed,
                                    socket_received_monotonic_ns=received_monotonic_ns,
                                    enqueue_timestamp=enqueued,
                                    enqueue_monotonic_ns=enqueued_monotonic_ns,
                                )
                            except KalshiWsPayloadError as error:
                                localized = _classify_payload_error(
                                    decoded,
                                    error,
                                    connection_id=connection_id,
                                    received=received,
                                    parsed=parsed,
                                )
                                if localized is None:
                                    keys, shape_hash = _payload_shape(decoded)
                                    key_summary = ",".join(keys[:8]) or "none"
                                    raise KalshiWsPayloadError(
                                        f"parser_stage=data_payload,reason={error}; "
                                        f"{_safe_payload_context(decoded)},keys={key_summary},"
                                        f"shape={shape_hash}"
                                    ) from None
                                if isinstance(localized, KalshiWsPayloadIssue):
                                    self.diagnostics.payload_issues += 1
                                else:
                                    self.diagnostics.protocol_notices += 1
                                consecutive_global_payload_failures = 0
                                self.diagnostics.messages += 1
                                yield localized
                                continue
                            consecutive_global_payload_failures = 0
                            if isinstance(
                                message,
                                (
                                    KalshiOrderBookSnapshot,
                                    KalshiOrderBookDelta,
                                    KalshiTickerUpdate,
                                ),
                            ):
                                # Only parsed market-data payloads prove the application
                                # channel is live.  WebSocket ping/pong and subscription or
                                # status frames must never keep an orderbook fresh.
                                self.diagnostics.last_message_received_at = received
                            self.diagnostics.messages += 1
                            yield message
                    finally:
                        receiver.cancel()
                        await asyncio.gather(receiver, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except KalshiWsPayloadError:
                consecutive_global_payload_failures += 1
                if consecutive_global_payload_failures >= 3:
                    raise
                failures = consecutive_global_payload_failures - 1
                self.diagnostics.protocol_reconnects += 1
                self.diagnostics.transport_state = KalshiWsRuntimeState.RECONNECTING
            except (ConnectionClosed, InvalidHandshake, OSError, TimeoutError):
                if self._closed:
                    return
                self.diagnostics.transport_errors += 1
                self.diagnostics.transport_state = KalshiWsRuntimeState.RECONNECTING
            finally:
                self._active = None
                self.diagnostics.last_disconnect_at = self._clock()
            if self._closed:
                return
            self.diagnostics.reconnects += 1
            delay = min(self._max_backoff, self._base_backoff * (2 ** min(failures, 8)))
            failures += 1
            await self._sleeper(delay)

    def set_reconnect_tickers(self, tickers: Sequence[str]) -> None:
        """Set exact read-only markets used by the next authenticated connection."""

        command = subscribe_command(1, tickers)
        payload = command.as_object()
        params = payload["params"]
        assert isinstance(params, Mapping)
        values = params["market_tickers"]
        assert isinstance(values, list)
        self._desired_tickers = tuple(str(value) for value in values)

    async def send_command(self, command: KalshiSubscriptionCommand) -> None:
        """Send one documented market-data subscription command on the active socket."""

        active = self._active
        if active is None:
            raise KalshiReadOnlyWsError("Kalshi WebSocket is not connected")
        await active.send(command.payload)

    async def close(self) -> None:
        self._closed = True
        active = self._active
        if active is not None:
            try:
                await active.close()
            except (ConnectionClosed, OSError, TimeoutError):
                return

    async def request_reconnect(self) -> None:
        """Close only the current socket so ``messages`` performs its bounded reconnect."""

        active = self._active
        if active is not None:
            try:
                await active.close()
            except (ConnectionClosed, OSError, TimeoutError):
                return
