"""论文查新 Web 应用配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NoveltyWebSettings:
    app_name: str = "论文查新 Multi-Agent"
    api_prefix: str = "/api/novelty"
    host: str = "0.0.0.0"
    port: int = 8010
    workflow_mode: str = "real"
    runs_root: Path = Path("outputs/web-runs")
    max_upload_bytes: int = 30 * 1024 * 1024
    cors_origins: tuple[str, ...] = (
        "http://localhost:3000",
        "http://localhost:5173",
    )

    @classmethod
    def from_env(cls) -> "NoveltyWebSettings":
        origins = os.getenv("NOVELTY_CORS_ORIGINS")
        workflow_mode = os.getenv("NOVELTY_WORKFLOW_MODE", cls.workflow_mode).strip().lower()
        if workflow_mode not in {"real", "demo"}:
            raise ValueError("NOVELTY_WORKFLOW_MODE 必须是 real 或 demo")
        max_upload_mb = int(os.getenv("NOVELTY_MAX_UPLOAD_MB", "30"))
        if max_upload_mb < 1:
            raise ValueError("NOVELTY_MAX_UPLOAD_MB 必须大于 0")
        return cls(
            host=os.getenv("NOVELTY_HOST", cls.host),
            port=int(os.getenv("NOVELTY_PORT", str(cls.port))),
            workflow_mode=workflow_mode,
            runs_root=Path(os.getenv("NOVELTY_RUNS_ROOT", str(cls.runs_root))),
            max_upload_bytes=max_upload_mb * 1024 * 1024,
            cors_origins=(
                tuple(item.strip() for item in origins.split(",") if item.strip())
                if origins
                else cls.cors_origins
            ),
        )
