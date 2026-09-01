"""Localhost-only FastAPI backend for the LIVE15 Control Center."""

from __future__ import annotations

import argparse
import asyncio
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from importlib.resources import files
from pathlib import PurePosixPath

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from live15_quant.config import Settings, load_settings
from live15_quant.control_center_models import (
    AccountEquityHistoryResponse,
    AccountFillResponse,
    AccountOrderResponse,
    AccountProfileResponse,
    AccountReadResponse,
    ArchiveResponse,
    CoverageResponse,
    DataResponse,
    EventSummaryResponse,
    HealthResponse,
    MarketHistoryResponse,
    MarketResponse,
    OperationsResponse,
    RecorderControlResponse,
    RecorderEventResponse,
    ResearchDataResponse,
    StorageResponse,
    SystemResponse,
    TerminalSubscriptionAction,
    TerminalSubscriptionRequest,
    TrainingResponse,
)
from live15_quant.control_center_service import ControlCenterService
from live15_quant.logging_config import configure_logging
from live15_quant.models import Asset, RecorderEventSeverity

LOCAL_HOST = "127.0.0.1"
TERMINAL_ENTRY_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}
TERMINAL_ASSET_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
}
TERMINAL_CSP_NONCE_PLACEHOLDER = "__LIVE15_CSP_NONCE__"


def create_app(
    settings: Settings | None = None,
    service: ControlCenterService | None = None,
) -> FastAPI:
    configured = settings or load_settings()
    boundary = service or ControlCenterService(configured)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop = asyncio.Event()
        sampler = asyncio.create_task(boundary.run_account_equity_sampler(stop))
        try:
            yield
        finally:
            stop.set()
            await sampler

    app = FastAPI(
        title="LIVE15 Control Center",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    terminal_page = files("live15_quant").joinpath("terminal", "index.html")
    terminal_assets = files("live15_quant").joinpath("terminal", "assets")
    if not terminal_page.is_file():
        raise RuntimeError(
            "packaged React terminal bundle is missing: live15_quant/terminal/index.html"
        )

    def terminal_asset(path: str):
        """Return a release-bundled terminal asset without traversal capability."""
        normalized = PurePosixPath(path)
        if normalized.is_absolute() or ".." in normalized.parts:
            return None
        candidate = terminal_assets.joinpath(*normalized.parts)
        return candidate if candidate.is_file() else None

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        nonce = secrets.token_urlsafe(32) if request.url.path == "/" else None
        request.state.terminal_csp_nonce = nonce
        response = await call_next(request)
        style_src_elem = "style-src-elem 'self'"
        if nonce is not None:
            style_src_elem += f" 'nonce-{nonce}'"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            f"{style_src_elem}; style-src-attr 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/", include_in_schema=False)
    def index(request: Request) -> Response:
        nonce = getattr(request.state, "terminal_csp_nonce", None)
        if not isinstance(nonce, str) or not nonce:
            raise RuntimeError("terminal CSP nonce was not initialized")
        page = terminal_page.read_text(encoding="utf-8")
        if page.count(TERMINAL_CSP_NONCE_PLACEHOLDER) != 1:
            raise RuntimeError("packaged terminal nonce placeholder is invalid")
        return Response(
            content=page.replace(TERMINAL_CSP_NONCE_PLACEHOLDER, nonce),
            media_type="text/html",
            headers=TERMINAL_ENTRY_CACHE_HEADERS,
        )

    @app.get("/terminal/assets/{asset_path:path}", include_in_schema=False)
    def terminal_static_asset(asset_path: str) -> FileResponse:
        asset = terminal_asset(asset_path)
        if asset is None:
            raise HTTPException(status_code=404, detail="terminal asset not found")
        return FileResponse(str(asset), headers=TERMINAL_ASSET_CACHE_HEADERS)

    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return boundary.health()

    @app.get("/api/markets", response_model=list[MarketResponse])
    def markets() -> list[MarketResponse]:
        return boundary.markets()

    @app.get("/api/markets/{asset}", response_model=MarketResponse)
    def market(asset: Asset) -> MarketResponse:
        return boundary.market(asset)

    @app.get("/api/markets/{asset}/history", response_model=MarketHistoryResponse)
    def market_history(asset: Asset) -> MarketHistoryResponse:
        try:
            return boundary.market_history(asset)
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.websocket("/ws/terminal")
    async def terminal_socket(websocket: WebSocket) -> None:
        client = websocket.client.host if websocket.client is not None else ""
        port = websocket.url.port or configured.ui_port
        origin = websocket.headers.get("origin")
        allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if client not in {"127.0.0.1", "::1", "testclient"} or (
            origin is not None and origin not in allowed_origins
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        subscriptions: set[str] = set()
        snapshots_pending: set[str] = set()
        last_cursors: dict[str, tuple[object, ...]] = {}
        sequence = 0

        def validated_channels(request: TerminalSubscriptionRequest) -> set[str]:
            return {channel.value for channel in request.channels}

        try:
            while True:
                raw: str | None = None
                if not subscriptions:
                    raw = await websocket.receive_text()
                else:
                    try:
                        raw = await asyncio.wait_for(websocket.receive_text(), timeout=0.1)
                    except TimeoutError:
                        pass
                if raw is not None:
                    try:
                        request = TerminalSubscriptionRequest.model_validate_json(raw)
                        channels = validated_channels(request)
                    except (ValueError, TypeError):
                        await websocket.close(code=1008)
                        return
                    if request.action is TerminalSubscriptionAction.SUBSCRIBE:
                        subscriptions.update(channels)
                        snapshots_pending.update(channels)
                    else:
                        subscriptions.difference_update(channels)
                        snapshots_pending.difference_update(channels)
                        for channel in channels:
                            last_cursors.pop(channel, None)
                if not subscriptions:
                    continue
                for channel in sorted(subscriptions):
                    cursor = await asyncio.to_thread(boundary.terminal_cursor, channel)
                    if cursor == last_cursors.get(channel) and channel not in snapshots_pending:
                        continue
                    sequence += 1
                    event = await asyncio.to_thread(
                        boundary.terminal_event,
                        channel,
                        sequence,
                        "snapshot" if channel in snapshots_pending else "update",
                    )
                    await websocket.send_text(event.model_dump_json())
                    last_cursors[channel] = cursor
                snapshots_pending.clear()
        except WebSocketDisconnect:
            return

    @app.get("/api/accounts", response_model=list[AccountProfileResponse])
    def accounts() -> list[AccountProfileResponse]:
        return boundary.account_profiles()

    @app.get("/api/account", response_model=AccountReadResponse)
    def account(
        profile: str = Query(default="production_primary", min_length=1, max_length=64),
    ) -> AccountReadResponse:
        return boundary.account(profile)

    @app.get("/api/account/summary", response_model=AccountReadResponse)
    def account_summary(
        profile: str = Query(default="production_primary", min_length=1, max_length=64),
    ) -> AccountReadResponse:
        return boundary.account_summary(profile)

    @app.get("/api/account/orders", response_model=list[AccountOrderResponse])
    def account_orders(profile: str = "production_primary") -> list[AccountOrderResponse]:
        return boundary.account_orders(profile)

    @app.get("/api/account/fills", response_model=list[AccountFillResponse])
    def account_fills(profile: str = "production_primary") -> list[AccountFillResponse]:
        return boundary.account_fills(profile)

    @app.get("/api/account/equity-history", response_model=AccountEquityHistoryResponse)
    def account_equity_history(
        profile: str = Query(default="production_primary", min_length=1, max_length=64),
    ) -> AccountEquityHistoryResponse:
        return boundary.account_equity_history(profile)

    @app.get("/api/coverage", response_model=CoverageResponse)
    def coverage() -> CoverageResponse:
        return boundary.coverage()

    @app.get("/api/data", response_model=DataResponse)
    def data() -> DataResponse:
        return boundary.data()

    @app.get("/api/training", response_model=TrainingResponse)
    def training() -> TrainingResponse:
        return boundary.training()

    @app.get("/api/research-data", response_model=ResearchDataResponse)
    def research_data() -> ResearchDataResponse:
        return boundary.research_data()

    @app.get("/api/archive", response_model=ArchiveResponse)
    def archive() -> ArchiveResponse:
        return boundary.archive()

    @app.get("/api/storage", response_model=StorageResponse)
    def storage() -> StorageResponse:
        return boundary.storage()

    @app.get("/api/operations", response_model=OperationsResponse)
    def operations() -> OperationsResponse:
        return boundary.operations()

    @app.get("/api/system", response_model=SystemResponse)
    def system() -> SystemResponse:
        return boundary.system()

    @app.get("/api/events", response_model=list[RecorderEventResponse])
    def events(
        limit: int = Query(default=100, ge=1, le=200),
        severity: RecorderEventSeverity | None = None,
        asset: Asset | None = None,
        source: str | None = Query(default=None, min_length=1, max_length=160),
        since: datetime | None = None,
    ) -> list[RecorderEventResponse]:
        if since is not None and (since.tzinfo is None or since.utcoffset() is None):
            raise HTTPException(status_code=422, detail="event time filter must include timezone")
        return boundary.recorder_events(
            limit=limit, severity=severity, asset=asset, source=source, since=since
        )

    @app.get("/api/events/summary", response_model=EventSummaryResponse)
    def event_summary(
        since: datetime | None = None,
        asset: Asset | None = None,
        source: str | None = Query(default=None, min_length=1, max_length=160),
    ) -> EventSummaryResponse:
        if since is None or since.tzinfo is None or since.utcoffset() is None:
            raise HTTPException(status_code=422, detail="event time filter must include timezone")
        try:
            return boundary.event_summary(asset=asset, source=source, since=since)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    def control(action: str, request: Request) -> Response:
        client = request.client.host if request.client is not None else ""
        if client not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(status_code=403, detail="recorder control is localhost-only")
        origin = request.headers.get("origin")
        if origin is not None and origin not in {
            f"http://127.0.0.1:{configured.ui_port}",
            f"http://localhost:{configured.ui_port}",
        }:
            raise HTTPException(status_code=403, detail="cross-origin recorder control denied")
        try:
            # The process action completes before the HTTP representation is created.
            # Return a pre-serialized typed receipt so FastAPI cannot perform a second,
            # post-action response-model conversion that turns a successful Pause into
            # an ambiguous 500 at the UI boundary.
            result = boundary.recorder_action(action)
            return Response(content=result.model_dump_json(), media_type="application/json")
        except TimeoutError as error:
            raise HTTPException(status_code=504, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/recorder/start", response_model=RecorderControlResponse)
    def recorder_start(request: Request) -> Response:
        return control("start", request)

    @app.post("/api/recorder/pause", response_model=RecorderControlResponse)
    def recorder_pause(request: Request) -> Response:
        return control("pause", request)

    @app.post("/api/recorder/resume", response_model=RecorderControlResponse)
    def recorder_resume(request: Request) -> Response:
        return control("resume", request)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the localhost-only LIVE15 Control Center")
    parser.add_argument("--port", type=int, default=None, help="localhost TCP port")
    arguments = parser.parse_args()
    settings = load_settings()
    port = settings.ui_port if arguments.port is None else arguments.port
    if not 1 <= port <= 65535:
        parser.error("--port must be in 1..65535")
    configure_logging(settings.log_level)
    print(f"LIVE15 Control Center: http://{LOCAL_HOST}:{port}", flush=True)
    uvicorn.run(create_app(settings), host=LOCAL_HOST, port=port, log_config=None)


if __name__ == "__main__":
    main()
