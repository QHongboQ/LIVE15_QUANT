"""Official authenticated, read-only Kalshi production WebSocket adapter."""

from __future__ import annotations

import asyncio
import base64
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
    KalshiServerMessage,
    KalshiWsPayloadError,
    parse_kalshi_server_message,
    subscribe_command,
)

if TYPE_CHECKING:
    from live15_quant.config import Settings

KALSHI_PRODUCTION_WS_PATH = "/trade-api/ws/v2"


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
    connected_at: datetime | None = None
    last_disconnect_at: datetime | None = None
    last_reconnect_duration_seconds: float | None = None
    receive_queue_high_watermark: int = 0


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
        self.diagnostics = KalshiWsDiagnostics()

    async def _receive_frames(
        self,
        websocket: KalshiWsConnection,
        queue: asyncio.Queue[tuple[str | bytes, datetime, int] | BaseException],
    ) -> None:
        """Drain the socket promptly so consumer persistence cannot distort receive time."""

        try:
            while not self._closed:
                raw = await websocket.recv()
                received = self._clock()
                received_monotonic_ns = self._perf_counter_ns()
                await queue.put((raw, received, received_monotonic_ns))
                self.diagnostics.receive_queue_high_watermark = max(
                    self.diagnostics.receive_queue_high_watermark,
                    queue.qsize(),
                )
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
        command = subscribe_command(1, tickers)
        failures = 0
        while not self._closed:
            self.diagnostics.connection_attempts += 1
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
                    self.diagnostics.connected_at = self._clock()
                    self.diagnostics.last_reconnect_duration_seconds = self._monotonic() - started
                    await websocket.send(command.payload)
                    failures = 0
                    queue: asyncio.Queue[tuple[str | bytes, datetime, int] | BaseException] = (
                        asyncio.Queue(maxsize=self._receive_queue_capacity)
                    )
                    receiver = asyncio.create_task(self._receive_frames(websocket, queue))
                    try:
                        while not self._closed:
                            queued = await asyncio.wait_for(queue.get(), timeout=self._read_timeout)
                            if isinstance(queued, BaseException):
                                raise queued
                            raw, received, received_monotonic_ns = queued
                            try:
                                decoded = json.loads(raw, parse_float=Decimal, parse_int=Decimal)
                            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                                raise KalshiWsPayloadError(
                                    "malformed Kalshi WebSocket JSON"
                                ) from None
                            parsed = self._clock()
                            message = parse_kalshi_server_message(
                                decoded,
                                connection_id=connection_id,
                                socket_received_timestamp=received,
                                parse_timestamp=parsed,
                                socket_received_monotonic_ns=received_monotonic_ns,
                            )
                            self.diagnostics.messages += 1
                            yield message
                    finally:
                        receiver.cancel()
                        await asyncio.gather(receiver, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except KalshiWsPayloadError:
                raise
            except (ConnectionClosed, InvalidHandshake, OSError, TimeoutError):
                if self._closed:
                    return
                self.diagnostics.transport_errors += 1
            finally:
                self._active = None
                self.diagnostics.last_disconnect_at = self._clock()
            if self._closed:
                return
            self.diagnostics.reconnects += 1
            delay = min(self._max_backoff, self._base_backoff * (2 ** min(failures, 8)))
            failures += 1
            await self._sleeper(delay)

    async def close(self) -> None:
        self._closed = True
        active = self._active
        if active is not None:
            try:
                await active.close()
            except (ConnectionClosed, OSError):
                return
