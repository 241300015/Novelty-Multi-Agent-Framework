"""Provider-neutral contracts for acquiring a known Web page."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import Field

from ..schemas import StrictModel
from .browser_runtime import playwright_launch_kwargs


class BrowserFetchResult(StrictModel):
    requested_url: str
    final_url: str
    title: str | None = None
    html: str
    text: str
    content_type: str | None = None
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrowserBackend(Protocol):
    name: str

    async def fetch(self, url: str) -> BrowserFetchResult: ...


@dataclass(frozen=True)
class _ContentLimits:
    html_chars: int = 2_000_000
    text_chars: int = 500_000


class PlaywrightBrowserBackend:
    """Acquire one public Web page in a fresh headless Chromium context."""

    name = "playwright"

    def __init__(
        self,
        *,
        navigation_timeout_ms: int = 30_000,
        max_html_chars: int = 2_000_000,
        max_text_chars: int = 500_000,
        network_mode: str = "inherit",
    ) -> None:
        if navigation_timeout_ms <= 0:
            raise ValueError("navigation_timeout_ms must be positive")
        if max_html_chars <= 0 or max_text_chars <= 0:
            raise ValueError("content limits must be positive")
        self.navigation_timeout_ms = navigation_timeout_ms
        self.limits = _ContentLimits(max_html_chars, max_text_chars)
        if network_mode not in {"inherit", "direct"}:
            raise ValueError("browser network mode must be 'inherit' or 'direct'")
        self.network_mode = network_mode

    async def fetch(self, url: str) -> BrowserFetchResult:
        requested_url = _validate_public_url(url)
        async_playwright = _load_async_playwright()
        warnings: list[str] = []
        async with async_playwright() as playwright:
            launch_kwargs, _network, _runtime = playwright_launch_kwargs(
                self.network_mode
            )
            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context()
                try:
                    page = await context.new_page()
                    page.set_default_navigation_timeout(self.navigation_timeout_ms)
                    response = await page.goto(
                        requested_url,
                        wait_until="domcontentloaded",
                        timeout=self.navigation_timeout_ms,
                    )
                    final_url = _validate_public_url(page.url)
                    title = (await page.title()).strip() or None
                    html = await page.content()
                    body = page.locator("body")
                    text = await body.inner_text(timeout=self.navigation_timeout_ms)
                    html, html_truncated = _truncate(html, self.limits.html_chars)
                    text, text_truncated = _truncate(text, self.limits.text_chars)
                    if html_truncated:
                        warnings.append(
                            f"HTML truncated to {self.limits.html_chars} characters"
                        )
                    if text_truncated:
                        warnings.append(
                            f"text truncated to {self.limits.text_chars} characters"
                        )
                    content_type = None
                    status = None
                    headers: dict[str, str] = {}
                    if response is not None:
                        headers = await response.all_headers()
                        content_type = headers.get("content-type")
                        status = response.status
                    return BrowserFetchResult(
                        requested_url=requested_url,
                        final_url=final_url,
                        title=title,
                        html=html,
                        text=text,
                        content_type=content_type,
                        warnings=warnings,
                        metadata={"status": status},
                    )
                finally:
                    await context.close()
            finally:
                await browser.close()


def _load_async_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed; install the browser extra and Chromium"
        ) from exc
    return async_playwright


def _validate_public_url(url: str) -> str:
    value = url.strip()
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("browser URL scheme must be http or https")
    if parts.username is not None or parts.password is not None:
        raise ValueError("browser URL must not contain userinfo")
    hostname = (parts.hostname or "").rstrip(".").lower()
    if not hostname:
        raise ValueError("browser URL must include a hostname")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("browser URL must not target localhost")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("browser URL must not target a private or local address")
    return value


def _truncate(content: str, limit: int) -> tuple[str, bool]:
    if len(content) <= limit:
        return content, False
    return content[:limit], True
