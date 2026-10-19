"""论文查新 Web API 响应模型。"""

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    application: str
    workflow: str
