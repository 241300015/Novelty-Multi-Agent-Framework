"""M5 诊断：代理检查 + 禁用代理直连测试。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from backend.env.model_client import _load_dev_env


def main() -> None:
    _load_dev_env()
    proxies = {k: v for k, v in os.environ.items() if "proxy" in k.lower()}
    print(json.dumps({"proxy_env": proxies}), flush=True)
    api_key = os.environ.get("SILICONFLOW_API_KEY", "")
    payload = json.dumps({
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "messages": [{"role": "user", "content": "以 JSON 格式回答：{\"ok\": true}"}],
        "response_format": {"type": "json_object"},
        "max_tokens": 64,
    }).encode("utf-8")
    for label, opener in (
        ("default(代理按环境)", None),
        ("no_proxy(禁用代理)", urllib.request.build_opener(urllib.request.ProxyHandler({}))),
    ):
        started = time.perf_counter()
        try:
            request = urllib.request.Request(
                "https://api.siliconflow.cn/v1/chat/completions",
                data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            handler = opener if opener else urllib.request.urlopen
            with handler(request, timeout=30) as response:
                body = response.read().decode("utf-8")
            elapsed = round(time.perf_counter() - started, 2)
            print(json.dumps({"mode": label, "ok": True, "elapsed_seconds": elapsed, "sample": body[:100]}), flush=True)
        except Exception as exc:
            elapsed = round(time.perf_counter() - started, 2)
            print(json.dumps({"mode": label, "ok": False, "elapsed_seconds": elapsed, "error": str(exc)[:150]}), flush=True)


if __name__ == "__main__":
    main()