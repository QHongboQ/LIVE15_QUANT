"""Localhost-only FastAPI backend for the LIVE15 Control Center."""

from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable
from datetime import datetime
from importlib.resources import files

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from live15_quant.config import Settings, load_settings
from live15_quant.control_center_models import (
    AccountFillResponse,
    AccountOrderResponse,
    AccountProfileResponse,
    AccountReadResponse,
    ArchiveResponse,
    CoverageResponse,
    DataResponse,
    EventSummaryResponse,
    HealthResponse,
    MarketResponse,
    OperationsResponse,
    RecorderControlResponse,
    RecorderEventResponse,
    ResearchDataResponse,
    StorageResponse,
    SystemResponse,
    TrainingResponse,
)
from live15_quant.control_center_service import ControlCenterService
from live15_quant.logging_config import configure_logging
from live15_quant.models import Asset, RecorderEventSeverity

LOCAL_HOST = "127.0.0.1"


def create_app(
    settings: Settings | None = None,
    service: ControlCenterService | None = None,
) -> FastAPI:
    configured = settings or load_settings()
    boundary = service or ControlCenterService(configured)
    app = FastAPI(
        title="LIVE15 Control Center",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    page = files("live15_quant").joinpath("web", "index.html")
    stylesheet = files("live15_quant").joinpath("web", "app.css")
    script = files("live15_quant").joinpath("web", "app.js")

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(str(page))

    @app.get("/assets/app.css", include_in_schema=False)
    def app_css() -> FileResponse:
        return FileResponse(str(stylesheet), media_type="text/css")

    @app.get("/assets/app.js", include_in_schema=False)
    def app_js() -> FileResponse:
        return FileResponse(str(script), media_type="text/javascript")

    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return boundary.health()

    @app.get("/api/markets", response_model=list[MarketResponse])
    def markets() -> list[MarketResponse]:
        return boundary.markets()

    @app.get("/api/markets/{asset}", response_model=MarketResponse)
    def market(asset: Asset) -> MarketResponse:
        return boundary.market(asset)

    @app.get("/api/accounts", response_model=list[AccountProfileResponse])
    def accounts() -> list[AccountProfileResponse]:
        return boundary.account_profiles()

    @app.get("/api/account", response_model=AccountReadResponse)
    def account(
        profile: str = Query(default="production_primary", min_length=1, max_length=64),
    ) -> AccountReadResponse:
        return boundary.account(profile)

    @app.get("/api/account/orders", response_model=list[AccountOrderResponse])
    def account_orders(profile: str = "production_primary") -> list[AccountOrderResponse]:
        return boundary.account(profile).orders

    @app.get("/api/account/fills", response_model=list[AccountFillResponse])
    def account_fills(profile: str = "production_primary") -> list[AccountFillResponse]:
        return boundary.account(profile).fills

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
