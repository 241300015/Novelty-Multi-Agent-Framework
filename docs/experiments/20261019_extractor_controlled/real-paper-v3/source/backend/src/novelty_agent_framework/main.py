"""论文查新 FastAPI 应用入口。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .config import NoveltyWebSettings
from .routers import health_router, runs_router
from .routers.report_artifacts import router as report_artifacts_router
from .services.report_resources import ReportResourceStore
from .services import (
    NoveltyWorkflowService,
    build_demo_workflow_service,
    build_real_workflow_service,
)

logger = logging.getLogger(__name__)


def create_app(
    settings: NoveltyWebSettings | None = None,
    *,
    service: NoveltyWorkflowService | None = None,
    static_root: Path | None = None,
    report_resources_root: Path | None = None,
) -> FastAPI:
    settings = settings or NoveltyWebSettings.from_env()
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Evidence-grounded novelty research workflow API",
    )
    application.state.settings = settings
    application.state.report_resources = ReportResourceStore(
        report_resources_root or Path(os.getenv("NOVELTY_REPORT_RESOURCE_ROOT", "outputs/report-resources"))
    )
    application.state.workflow_service = service
    application.state.workflow_error = None
    if service is None:
        try:
            application.state.workflow_service = (
                build_real_workflow_service(settings)
                if settings.workflow_mode == "real"
                else build_demo_workflow_service(settings)
            )
        except Exception as exc:  # noqa: BLE001 - keep app available for health checks
            application.state.workflow_error = type(exc).__name__
            logger.error("workflow initialization failed: %s", type(exc).__name__)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(health_router, prefix=settings.api_prefix)
    application.include_router(runs_router, prefix=settings.api_prefix)
    application.include_router(report_artifacts_router, prefix=settings.api_prefix)

    @application.middleware('http')
    async def cache_policy(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        elif request.url.path in ('/', '/index.html', '/sw.js'):
            response.headers['Cache-Control'] = 'no-cache'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    static_root = static_root or Path(os.getenv('NOVELTY_FRONTEND_DIST', 'frontend/dist'))
    if static_root.is_dir():
        application.mount('/', StaticFiles(directory=static_root, html=True), name='frontend')
    return application


app = create_app()


def run() -> None:
    import uvicorn

    settings = NoveltyWebSettings.from_env()
    uvicorn.run(
        "novelty_agent_framework.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
