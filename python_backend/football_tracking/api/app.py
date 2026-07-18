from __future__ import annotations

import math
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from football_tracking.api.routes.ai import router as ai_router
from football_tracking.api.routes.artifacts import router as artifacts_router
from football_tracking.api.routes.broadcast import router as broadcast_router
from football_tracking.api.routes.configs import router as configs_router
from football_tracking.api.routes.detectors import router as detectors_router
from football_tracking.api.routes.health import router as health_router
from football_tracking.api.routes.inputs import router as inputs_router
from football_tracking.api.routes.runs import router as runs_router
from football_tracking.api.service import ApiService


def create_app(repo_root: Path | None = None, *, initialize_service: bool = True) -> FastAPI:
    resolved_repo_root = repo_root or Path(__file__).resolve().parents[2]
    service = ApiService(resolved_repo_root) if initialize_service else None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            if service is not None:
                service.close()

    app = FastAPI(
        title="Football Tracking API",
        version="0.1.0",
        summary="Local orchestration API for configs, runs, artifacts, cleanup, and follow-cam review.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def reject_untrusted_browser_origins(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin is not None and not _is_trusted_loopback_origin(origin):
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-origin access is not permitted"},
            )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def safe_request_validation_error(
        _request: Any, error: RequestValidationError
    ) -> JSONResponse:
        details = []
        for item in error.errors():
            details.append(
                {
                    "type": _safe_validation_value(item.get("type")),
                    "loc": _safe_validation_value(item.get("loc", ())),
                    "msg": _safe_validation_value(item.get("msg")),
                }
            )
        return JSONResponse(status_code=422, content={"detail": details})
    if service is not None:
        app.state.api_service = service
    app.include_router(health_router, prefix="/api/v1", tags=["health"])
    app.include_router(inputs_router, prefix="/api/v1", tags=["inputs"])
    app.include_router(configs_router, prefix="/api/v1", tags=["configs"])
    app.include_router(detectors_router, prefix="/api/v1", tags=["detectors"])
    app.include_router(runs_router, prefix="/api/v1", tags=["runs"])
    app.include_router(artifacts_router, prefix="/api/v1", tags=["artifacts"])
    app.include_router(broadcast_router, prefix="/api/v1", tags=["broadcast"])
    app.include_router(ai_router, prefix="/api/v1", tags=["ai"])
    return app


def _safe_validation_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, (list, tuple)):
        return [_safe_validation_value(item) for item in value]
    if isinstance(value, dict):
        return {
            _safe_validation_value(str(key)): _safe_validation_value(item)
            for key, item in value.items()
        }
    return str(value).encode("utf-8", "replace").decode("utf-8")


def _is_trusted_loopback_origin(origin: str) -> bool:
    if origin != origin.strip() or origin == "null":
        return False
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
    ):
        return False
    return port is None or 1 <= port <= 65_535


app = create_app()
