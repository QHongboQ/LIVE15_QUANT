"""Kalshi-sdk v12 WebSocket shadow/parity component with explicit lifecycle ownership."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from live15_quant.config import Settings, load_settings
from live15_quant.kalshi_gateway.client import (
    KalshiEnvironment,
    KalshiGatewayConfig,
    production_credentials,
)
from live15_quant.kalshi_gateway.shadow import (
    KalshiSdkReliabilityAdapter,
    ShadowParityComparator,
    ShadowTelemetryStore,
)
from live15_quant.kalshi_gateway.websocket import (
    GatewayReceivedMessage,
    KalshiWebSocketGateway,
)
from live15_quant.logging_config import configure_logging
from live15_quant.models import Asset
from live15_quant.runtime_status import (
    RuntimePidLease,
    atomic_json,
    component_status,
    read_json,
    utc_timestamp,
)

logger = logging.getLogger(__name__)

_LIFECYCLE_OWNER_ENV = "LIVE15_KALSHI_SDK_SHADOW_LIFECYCLE_OWNER"
_LEGACY_LIFECYCLE_OWNER = "runtime_supervisor"
_NOMAD_LIFECYCLE_OWNER = "nomad"


def _lifecycle_owner(environ: Mapping[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    owner = source.get(_LIFECYCLE_OWNER_ENV, _LEGACY_LIFECYCLE_OWNER).strip().lower()
    if owner not in {_LEGACY_LIFECYCLE_OWNER, _NOMAD_LIFECYCLE_OWNER}:
        raise ValueError(
            f"{_LIFECYCLE_OWNER_ENV} lifecycle owner must be "
            f"{_LEGACY_LIFECYCLE_OWNER} or {_NOMAD_LIFECYCLE_OWNER}"
        )
    return owner


def _sanitized_error_code(error: BaseException) -> str:
    parts = [type(error).__name__]
    cause = error.__cause__
    if cause is not None:
        parts.append(type(cause).__name__)
        response = getattr(cause, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            parts.append(f"HTTP_{status}")
    return "/".join(parts)


def _paths(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    runtime = root / "runtime"
    return (
        runtime / "kalshi-sdk-ws-shadow-status.json",
        runtime / "kalshi-sdk-ws-shadow.pid",
        runtime / "runtime-supervisor-control.json",
        root / "data" / "kalshi_sdk_ws_shadow.sqlite3",
        root / "data" / "kalshi-live-ws-books.json",
    )


def _stop_requested(control_path: Path) -> bool:
    payload = read_json(control_path)
    return payload is not None and payload.get("desired") == "stopped"


def _current_universe(settings: Settings) -> dict[str, Asset]:
    health = read_json(settings.recorder_health_path)
    raw = health.get("current_markets") if isinstance(health, dict) else None
    if not isinstance(raw, dict):
        return {}
    universe: dict[str, Asset] = {}
    for asset in Asset:
        ticker = raw.get(asset.value)
        if isinstance(ticker, str) and ticker:
            universe[ticker] = asset
    return universe


class KalshiSdkShadowRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        store: ShadowTelemetryStore,
        old_projection_path: Path,
        status: dict[str, object],
        status_path: Path,
        control_path: Path | None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.old_projection_path = old_projection_path
        self.status = status
        self.status_path = status_path
        self.control_path = control_path
        self.stop_event = asyncio.Event()
        self.stop_reason: str | None = None
        self.adapter: KalshiSdkReliabilityAdapter | None = None
        self.active_tickers: tuple[str, ...] = ()
        self.rollover_count = 0
        self.last_rollover_reason: str | None = None
        self.last_error: str | None = None

    def request_stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
        self.stop_event.set()

    def _status_payload(self, state: str) -> dict[str, object]:
        health = (
            self.adapter.health(datetime.now(UTC))
            if self.adapter is not None
            else {
                "connected_status": state,
                "synchronized_count": 0,
                "subscribed_assets": len(self.active_tickers),
                "assets": {},
                "metrics": self.store.summary(),
            }
        )
        metrics = health.get("metrics")
        mismatch_count = metrics.get("recent_mismatch_count") if isinstance(metrics, dict) else None
        gap_count = metrics.get("gap_count") if isinstance(metrics, dict) else None
        payload = {
            "status": state,
            "last_heartbeat": utc_timestamp(),
            "process_alive": True,
            "last_error": self.last_error,
            "connected_status": health.get("connected_status"),
            "synchronized_count": health.get("synchronized_count", 0),
            "subscribed_assets": health.get("subscribed_assets", 0),
            "recent_mismatch_count": mismatch_count,
            "recent_gap_count": gap_count,
            "rollover_count": self.rollover_count,
            "last_rollover_reason": self.last_rollover_reason,
            "active_tickers": list(self.active_tickers),
            "parity_status": (
                "OLD_WS_UNAVAILABLE"
                if isinstance(metrics, dict) and metrics.get("aligned_comparisons") == 0
                else "MEASURING"
            ),
            "health": health,
        }
        self.status.update(payload)
        return dict(self.status)

    async def _heartbeat(self) -> None:
        while not self.stop_event.is_set():
            if self.control_path is not None and _stop_requested(self.control_path):
                self.request_stop("SUPERVISOR_STOP_REQUESTED")
                break
            if self.adapter is None:
                state = "WAITING_TICKERS"
            elif self.adapter.last_state in {"connected", "streaming"}:
                state = "RUNNING"
            else:
                state = "RECONNECTING"
            atomic_json(self.status_path, self._status_payload(state))
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=2.0)
            except TimeoutError:
                continue

    async def _watch_rollover(self, original: tuple[str, ...], changed: asyncio.Event) -> None:
        while not self.stop_event.is_set() and not changed.is_set():
            await asyncio.sleep(1.0)
            current = tuple(sorted(_current_universe(self.settings)))
            if len(current) == len(Asset) and current != original:
                if not changed.is_set():
                    self.rollover_count += 1
                    self.last_rollover_reason = "RECORDER_UNIVERSE_CHANGED"
                changed.set()

    async def _pump(
        self,
        stream: Any,
        *,
        unknown_ticker_policy: str = "error",
        rollover: asyncio.Event | None = None,
    ) -> None:
        if unknown_ticker_policy not in {"error", "ignore", "rollover"}:
            raise ValueError("invalid unknown ticker policy")
        async for received in stream:
            if self.stop_event.is_set():
                return
            if isinstance(received, GatewayReceivedMessage):
                message = received.message
                received_at = received.received_at
            else:
                message = received
                received_at = datetime.now(UTC)
            # SDK v12 multiplexes event-level fee updates onto the market
            # lifecycle channel. They do not identify one market/asset and are
            # outside the Recorder lifecycle contract, so they must not tear
            # down the market-data session.
            if str(getattr(message, "type", "")) == "event_fee_update":
                continue
            adapter = self.adapter
            if adapter is None:
                raise RuntimeError("SDK shadow adapter is unavailable")
            ticker = str(getattr(getattr(message, "msg", None), "market_ticker", ""))
            if ticker and ticker not in adapter.asset_by_ticker:
                if unknown_ticker_policy == "ignore":
                    # The SDK lifecycle stream is event-wide in production and
                    # can include markets outside the requested ticker set.
                    # Such frames are not evidence that the 10-market Recorder
                    # universe changed and must never enter canonical state.
                    continue
                # A rollover can reach the live SDK stream before Recorder's
                # periodically projected 10-market universe changes. Do not
                # guess an asset or accept data into the old session. End it
                # cleanly and rebuild only after a complete new universe is
                # authoritative.
                if unknown_ticker_policy != "rollover" or rollover is None:
                    raise ValueError("SDK WebSocket ticker is outside the shadow universe")
                if not rollover.is_set():
                    self.rollover_count += 1
                    self.last_rollover_reason = "SDK_TICKER_ROLLOVER"
                    logger.info(
                        "Kalshi SDK shadow detected ticker rollover",
                        extra={
                            "event": "kalshi_sdk_shadow_ticker_rollover",
                            "reason": self.last_rollover_reason,
                        },
                    )
                    rollover.set()
                return
            adapter.accept(message, received_at=received_at)
            await asyncio.sleep(0)

    async def _drain_sdk_orderbook(self, stream: Any) -> None:
        """Drain the SDK's mutable raw iterator without using it as truth."""

        async for _message in stream:
            if self.stop_event.is_set():
                return
            await asyncio.sleep(0)

    async def _run_session(self, universe: dict[str, Asset]) -> None:
        credentials = production_credentials(self.settings)
        config = KalshiGatewayConfig.for_environment(
            KalshiEnvironment.PRODUCTION,
            timeout_seconds=self.settings.kalshi_websocket_read_timeout_seconds,
            read_retries=3,
        )
        comparator = ShadowParityComparator(self.old_projection_path, alignment_seconds=1.0)
        self.adapter = KalshiSdkReliabilityAdapter(
            universe,
            self.store,
            comparator,
            stale_seconds=self.settings.kalshi_websocket_stale_seconds,
        )

        async def state_change(old: Any, new: Any) -> None:
            adapter = self.adapter
            if adapter is None:
                return
            adapter.connection_state_changed(
                str(getattr(old, "value", old)),
                str(getattr(new, "value", new)),
                datetime.now(UTC),
            )

        async def on_error(error: Any) -> None:
            self.last_error = _sanitized_error_code(error)

        gateway = KalshiWebSocketGateway(config, credentials)
        immutable_orderbook = gateway.immutable_orderbook_stream(maxsize=20_000)
        websocket = gateway.build(
            on_state_change=state_change,
            on_error=on_error,
        )
        tickers = sorted(universe)
        self.active_tickers = tuple(tickers)
        rollover = asyncio.Event()
        async with websocket.connect() as session:
            # Establish low-volume channels before the high-volume orderbook.
            # SDK v12 pauses its recv loop while synchronously waiting for each
            # subscribe acknowledgement; an already-live orderbook can bury a
            # later control ack behind enough market frames to hit that bounded
            # timeout. The immutable gateway feed captures any snapshots that
            # arrive before the final orderbook ack, so this order is lossless.
            ticker = await session.subscribe_ticker(tickers=tickers, maxsize=2_000)
            lifecycle = await session.subscribe_market_lifecycle(tickers=tickers, maxsize=1_000)
            sdk_orderbook = await session.subscribe_orderbook_delta(tickers=tickers, maxsize=10_000)
            tasks = {
                asyncio.create_task(
                    self._pump(
                        immutable_orderbook,
                        unknown_ticker_policy="rollover",
                        rollover=rollover,
                    ),
                    name="sdk-shadow-orderbook",
                ),
                asyncio.create_task(
                    self._drain_sdk_orderbook(sdk_orderbook),
                    name="sdk-shadow-orderbook-sdk-drain",
                ),
                asyncio.create_task(
                    self._pump(
                        ticker,
                        unknown_ticker_policy="rollover",
                        rollover=rollover,
                    ),
                    name="sdk-shadow-ticker",
                ),
                asyncio.create_task(
                    self._pump(lifecycle, unknown_ticker_policy="ignore"),
                    name="sdk-shadow-lifecycle",
                ),
            }
            try:
                tasks.update(
                    {
                        asyncio.create_task(
                            self._watch_rollover(self.active_tickers, rollover),
                            name="sdk-shadow-rollover",
                        ),
                        asyncio.create_task(self.stop_event.wait(), name="sdk-shadow-stop"),
                        asyncio.create_task(rollover.wait(), name="sdk-shadow-rollover-wait"),
                    }
                )
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if task.get_name() in {"sdk-shadow-stop", "sdk-shadow-rollover-wait"}:
                        continue
                    error = task.exception()
                    if error is not None:
                        raise error
            finally:
                pending_tasks = [task for task in tasks if not task.done()]
                for task in pending_tasks:
                    task.cancel()
                if pending_tasks:
                    await asyncio.gather(*pending_tasks, return_exceptions=True)

    async def run(self) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(), name="sdk-shadow-heartbeat")
        try:
            backoff = 1.0
            while not self.stop_event.is_set():
                universe = _current_universe(self.settings)
                if len(universe) != len(Asset):
                    self.active_tickers = tuple(sorted(universe))
                    atomic_json(self.status_path, self._status_payload("WAITING_TICKERS"))
                    await asyncio.sleep(1.0)
                    continue
                try:
                    self.last_error = None
                    await self._run_session(universe)
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.last_error = _sanitized_error_code(error)
                    adapter = self.adapter
                    if adapter is not None:
                        adapter.connection_state_changed(
                            adapter.last_state,
                            "reconnecting",
                            datetime.now(UTC),
                        )
                    logger.warning(
                        "Kalshi SDK shadow session failed",
                        exc_info=True,
                        extra={
                            "event": "kalshi_sdk_shadow_session_failed",
                            "error_type": self.last_error,
                        },
                    )
                    atomic_json(self.status_path, self._status_payload("RECONNECTING"))
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=backoff)
                    except TimeoutError:
                        pass
                    backoff = min(15.0, backoff * 2)
                finally:
                    self.adapter = None
        finally:
            self.stop_event.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)


def _nomad_break_handler(
    runner: KalshiSdkShadowRunner,
    loop: asyncio.AbstractEventLoop,
):
    def handle_break(_signum: int, _frame: object) -> None:
        loop.call_soon_threadsafe(runner.request_stop, "NOMAD_CTRL_BREAK")

    return handle_break


async def _run() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    root = Path.cwd().resolve()
    status_path, lease_path, legacy_control_path, store_path, old_projection = _paths(root)
    lifecycle_owner = _lifecycle_owner()
    control_path = legacy_control_path if lifecycle_owner == _LEGACY_LIFECYCLE_OWNER else None
    lease = RuntimePidLease(lease_path)
    lease.acquire()
    started = utc_timestamp()
    status = component_status(
        name="kalshi_sdk_ws_shadow",
        status="STARTING",
        pid=os.getpid(),
        started_at=started,
        last_heartbeat=started,
        expected_mode="SDK_WS_SHADOW_NO_RECORDER_WRITES",
        working_directory=root,
        log_path=root / "logs" / "kalshi_sdk_ws_shadow.log",
        extra={
            "store_path": str(store_path),
            "official_recorder_writes": False,
            "sdk_endpoint": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
            "lifecycle_owner": lifecycle_owner,
        },
    )
    atomic_json(status_path, status)
    store = ShadowTelemetryStore(store_path)
    runner = KalshiSdkShadowRunner(
        settings=settings,
        store=store,
        old_projection_path=old_projection,
        status=status,
        status_path=status_path,
        control_path=control_path,
    )
    sigbreak: Any | None = None
    previous_sigbreak: Any | None = None
    try:
        if lifecycle_owner == _NOMAD_LIFECYCLE_OWNER:
            sigbreak = getattr(signal, "SIGBREAK", None)
            if os.name != "nt" or sigbreak is None:
                raise RuntimeError("Nomad shadow lifecycle requires Windows SIGBREAK support")
            previous_sigbreak = signal.signal(
                sigbreak,
                _nomad_break_handler(runner, asyncio.get_running_loop()),
            )
        await runner.run()
        status.update(
            {
                "status": "STOPPED",
                "last_heartbeat": utc_timestamp(),
                "last_error": None,
                "stop_reason": runner.stop_reason or "STOP_REQUESTED",
            }
        )
        atomic_json(status_path, status)
    except Exception as error:
        status.update(
            {
                "status": "ERROR",
                "last_heartbeat": utc_timestamp(),
                "last_error": type(error).__name__,
            }
        )
        atomic_json(status_path, status)
        raise
    finally:
        if sigbreak is not None and previous_sigbreak is not None:
            signal.signal(sigbreak, previous_sigbreak)
        store.close()
        lease.release()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
