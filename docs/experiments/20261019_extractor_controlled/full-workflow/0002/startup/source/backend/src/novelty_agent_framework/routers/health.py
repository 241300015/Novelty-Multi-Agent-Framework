"""论文查新服务健康检查。"""

from fastapi import APIRouter, Request

from ..schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    ready = request.app.state.workflow_service is not None
    return HealthResponse(
        status="ready" if ready else "degraded",
        application=settings.app_name,
        workflow=settings.workflow_mode if ready else "unavailable",
    )
