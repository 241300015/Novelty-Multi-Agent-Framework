"""Zero-LLM Playwright Browser runtime preflight."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any, Mapping

from ..tools.browser_backend import _validate_public_url
from ..tools.browser_runtime import playwright_launch_kwargs
from ..tools.browser_runtime import BrowserDependencyError


async def run_browser_preflight(
    *,
    network_mode: str = "inherit",
    public_url: str = "https://example.com/",
    environ: Mapping[str, str] | None = None,
    navigation_timeout_ms: int = 15_000,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "browser_backend": "playwright",
        "network_mode": network_mode,
        "model_api_calls": 0,
        "playwright_import": "FAIL",
        "chromium_executable": "FAIL",
        "chromium_launch": "FAIL",
        "new_context": "FAIL",
        "new_page": "FAIL",
        "local_render": "FAIL",
        "public_fetch": "NOT_RUN",
        "ready": False,
        "failure_class": None,
        "error": None,
    }
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        return _fail(result, "DEPENDENCY_ERROR", exc)
    result["playwright_import"] = "PASS"

    try:
        launch_kwargs, network, runtime = playwright_launch_kwargs(
            network_mode, environ
        )
    except BrowserDependencyError as exc:
        return _fail(result, "DEPENDENCY_ERROR", exc)
    except (TypeError, ValueError) as exc:
        return _fail(result, "NETWORK_CONFIG_ERROR", exc)
    result.update(network.safe_summary())
    result["runtime_dependency_mode"] = runtime.mode
    result["runtime_library_fallback"] = (
        runtime.mode == "environment_fallback"
    )

    try:
        target = _validate_public_url(public_url)
        async with async_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            result["chromium_executable"] = (
                "PASS" if executable.is_file() else "FAIL"
            )
            if not executable.is_file():
                return _fail(
                    result,
                    "DEPENDENCY_ERROR",
                    RuntimeError("Chromium executable is unavailable"),
                )
            try:
                browser = await playwright.chromium.launch(**launch_kwargs)
            except Exception as exc:
                return _fail(result, "LAUNCH_ERROR", exc)
            result["chromium_launch"] = "PASS"
            try:
                context = await browser.new_context()
                result["new_context"] = "PASS"
                try:
                    page = await context.new_page()
                    result["new_page"] = "PASS"
                    await page.set_content(
                        "<html><title>browser-preflight</title>"
                        "<body>local-render-ok</body></html>"
                    )
                    if (
                        await page.title() != "browser-preflight"
                        or await page.locator("body").inner_text()
                        != "local-render-ok"
                    ):
                        return _fail(
                            result,
                            "CONTENT_ERROR",
                            RuntimeError("local HTML content mismatch"),
                        )
                    result["local_render"] = "PASS"
                    try:
                        response = await page.goto(
                            target,
                            wait_until="domcontentloaded",
                            timeout=navigation_timeout_ms,
                        )
                        text = await page.locator("body").inner_text(
                            timeout=navigation_timeout_ms
                        )
                    except Exception as exc:
                        return _fail(result, "NAVIGATION_ERROR", exc)
                    if not text.strip():
                        return _fail(
                            result,
                            "CONTENT_ERROR",
                            RuntimeError("public page body is empty"),
                        )
                    result["public_fetch"] = "PASS"
                    result["public_status"] = (
                        response.status if response is not None else None
                    )
                    result["public_title"] = (await page.title()).strip() or None
                    result["public_text_chars"] = len(text)
                finally:
                    await context.close()
            finally:
                await browser.close()
    except ValueError as exc:
        return _fail(result, "NETWORK_CONFIG_ERROR", exc)
    except Exception as exc:
        return _fail(result, "LAUNCH_ERROR", exc)

    result["ready"] = True
    return result


def _fail(result: dict[str, Any], category: str, exc: Exception) -> dict[str, Any]:
    result["failure_class"] = category
    result["error"] = _safe_error(exc)
    return result


def _safe_error(exc: Exception) -> str:
    message = str(exc)
    message = re.sub(r"--proxy-server=\S+", "--proxy-server=<redacted>", message)
    message = re.sub(
        r"--proxy-bypass-list=\S+", "--proxy-bypass-list=<redacted>", message
    )
    message = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1<redacted>@", message)
    message = re.sub(
        r"(?i)(proxy|password|username|authorization)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        message,
    )
    return f"{type(exc).__name__}: {message}"[:4000]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--network-mode", choices=("inherit", "direct"), default="inherit")
    parser.add_argument("--public-url", default="https://example.com/")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(
        run_browser_preflight(
            network_mode=args.network_mode,
            public_url=args.public_url,
        )
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(0 if result["ready"] else 1)


if __name__ == "__main__":
    main()
