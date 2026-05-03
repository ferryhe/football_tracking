from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from football_tracking.api.routes.ai import router as ai_router
from football_tracking.api.routes.artifacts import router as artifacts_router
from football_tracking.api.routes.configs import router as configs_router
from football_tracking.api.routes.health import router as health_router
from football_tracking.api.routes.inputs import router as inputs_router
from football_tracking.api.routes.runs import router as runs_router
from football_tracking.api.service import ApiService


def create_app(repo_root: Path | None = None) -> FastAPI:
    resolved_repo_root = repo_root or Path(__file__).resolve().parents[2]
    service = ApiService(resolved_repo_root)

    app = FastAPI(
        title="Football Tracking API",
        version="0.1.0",
        summary="Local orchestration API for configs, runs, artifacts, cleanup, and follow-cam review.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.api_service = service
    app.include_router(health_router, prefix="/api/v1", tags=["health"])
    app.include_router(inputs_router, prefix="/api/v1", tags=["inputs"])
    app.include_router(configs_router, prefix="/api/v1", tags=["configs"])
    app.include_router(runs_router, prefix="/api/v1", tags=["runs"])
    app.include_router(artifacts_router, prefix="/api/v1", tags=["artifacts"])
    app.include_router(ai_router, prefix="/api/v1", tags=["ai"])
    return app


app = create_app()
