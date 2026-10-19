"""M5 诊断：用项目 ModelClient 分别测 flash/r1 × max_tokens 8192/2048。
"""

from __future__ import annotations

import json
import sys
import time

from backend.env import ChatMessage, ModelCallOptions
from backend.env.model_client import _load_dev_env
from novelty_agent_framework.config import build_model_registry, load_application_config


def main() -> None:
    _load_dev_env()
    config = load_application_config()
    registry = build_model_registry(config)
    for alias in ("deepseek-flash", "r1-qwen3-8b"):
        for max_tokens in (8192, 2048):
            try:
                client = registry.client_for(alias)
            except Exception as exc:
                print(json.dumps({"alias": alias, "max_tokens": max_tokens, "ok": False, "error": f"registry: {exc}"}))
                continue
            started = time.perf_counter()
            try:
                response = client.complete(
                    [ChatMessage(role="user", content="以 JSON 格式回答，只输出：{\"ok\": true}")],
                    options=ModelCallOptions(
                        temperature=0.2,
                        max_tokens=max_tokens,
                        response_format={"type": "json_object"},
                    ),
                )
                elapsed = round(time.perf_counter() - started, 2)
                print(json.dumps({"alias": alias, "max_tokens": max_tokens, "ok": True, "elapsed_seconds": elapsed, "content": response.content[:80]}))
            except Exception as exc:
                elapsed = round(time.perf_counter() - started, 2)
                print(json.dumps({"alias": alias, "max_tokens": max_tokens, "ok": False, "elapsed_seconds": elapsed, "error": str(exc)[:150]}))


if __name__ == "__main__":
    main()