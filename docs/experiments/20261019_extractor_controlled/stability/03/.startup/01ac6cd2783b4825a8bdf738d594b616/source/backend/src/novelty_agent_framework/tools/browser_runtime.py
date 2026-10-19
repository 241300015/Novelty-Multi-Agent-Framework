"""Trusted runtime adaptation for Playwright network and shared libraries."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping
from urllib.parse import unquote, urlsplit, urlunsplit

BrowserNetworkMode = Literal["inherit", "direct"]
PROXY_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")
CHROMIUM_RUNTIME_LIBRARIES = (
    "libnspr4.so",
    "libnss3.so",
    "libnssutil3.so",
    "libasound.so.2",
)


class BrowserDependencyError(RuntimeError):
    """Chromium cannot use either system or interpreter-local libraries."""


@dataclass(frozen=True)
class ChromiumRuntimeSettings:
    mode: Literal["system", "environment_fallback"]
    child_environment: dict[str, str] | None = None


@dataclass(frozen=True)
class BrowserNetworkSettings:
    mode: BrowserNetworkMode
    server: str | None = None
    bypass: str | None = None
    username: str | None = None
    password: str | None = None

    @property
    def proxy_configured(self) -> bool:
        return self.server is not None

    def playwright_proxy(self) -> dict[str, str] | None:
        if self.server is None:
            return None
        proxy = {"server": self.server}
        if self.bypass:
            proxy["bypass"] = self.bypass
        if self.username is not None:
            proxy["username"] = self.username
        if self.password is not None:
            proxy["password"] = self.password
        return proxy

    def safe_summary(self) -> dict[str, object]:
        scheme = urlsplit(self.server).scheme if self.server else None
        return {
            "mode": self.mode,
            "proxy_configured": self.proxy_configured,
            "proxy_scheme": scheme or None,
            "proxy_host": "<redacted>" if self.server else None,
            "credentials": "redacted" if self.username or self.password else "none",
            "bypass_configured": bool(self.bypass),
        }


def resolve_browser_network(
    mode: str,
    environ: Mapping[str, str] | None = None,
) -> BrowserNetworkSettings:
    if mode not in {"inherit", "direct"}:
        raise ValueError("browser network mode must be 'inherit' or 'direct'")
    if mode == "direct":
        return BrowserNetworkSettings(mode="direct")

    values = os.environ if environ is None else environ
    raw_proxy = next(
        (values[key].strip() for key in PROXY_KEYS if values.get(key, "").strip()),
        None,
    )
    if raw_proxy is None:
        return BrowserNetworkSettings(mode="inherit")
    server, username, password = _parse_proxy(raw_proxy)
    bypass = next(
        (values[key].strip() for key in NO_PROXY_KEYS if values.get(key, "").strip()),
        None,
    )
    return BrowserNetworkSettings(
        mode="inherit",
        server=server,
        bypass=bypass,
        username=username,
        password=password,
    )


def playwright_launch_kwargs(
    mode: str,
    environ: Mapping[str, str] | None = None,
    *,
    python_prefix: str | Path | None = None,
) -> tuple[dict[str, object], BrowserNetworkSettings, ChromiumRuntimeSettings]:
    values = os.environ if environ is None else environ
    network = resolve_browser_network(mode, values)
    kwargs: dict[str, object] = {"headless": True}
    proxy = network.playwright_proxy()
    if proxy is not None:
        kwargs["proxy"] = proxy
    runtime = resolve_chromium_runtime(
        values, python_prefix=python_prefix
    )
    if runtime.child_environment is not None:
        kwargs["env"] = runtime.child_environment
    return kwargs, network, runtime


def chromium_child_environment(
    environ: Mapping[str, str] | None = None,
    *,
    python_prefix: str | Path | None = None,
) -> dict[str, str] | None:
    """Compatibility wrapper returning only a fallback child environment."""

    return resolve_chromium_runtime(
        environ, python_prefix=python_prefix
    ).child_environment


def resolve_chromium_runtime(
    environ: Mapping[str, str] | None = None,
    *,
    python_prefix: str | Path | None = None,
    system_libraries_available: bool | None = None,
) -> ChromiumRuntimeSettings:
    """Resolve system-first, environment-fallback, or fail with install guidance."""

    system_ready = (
        _runtime_libraries_resolve()
        if system_libraries_available is None
        else system_libraries_available
    )
    if system_ready:
        return ChromiumRuntimeSettings(mode="system")
    prefix = Path(python_prefix or sys.prefix)
    library_dir = prefix / "lib"
    if not all((library_dir / name).exists() for name in CHROMIUM_RUNTIME_LIBRARIES):
        raise BrowserDependencyError(
            "Chromium runtime libraries are unavailable in both the system loader "
            "and the current Python environment; install them with "
            "'playwright install-deps chromium' during deployment"
        )
    values = dict(os.environ if environ is None else environ)
    current = values.get("LD_LIBRARY_PATH", "")
    values["LD_LIBRARY_PATH"] = (
        f"{library_dir}{os.pathsep}{current}" if current else str(library_dir)
    )
    return ChromiumRuntimeSettings(
        mode="environment_fallback",
        child_environment=values,
    )


def _runtime_libraries_resolve() -> bool:
    """Check the system loader cache without inheriting interpreter search paths."""

    child_environment = dict(os.environ)
    child_environment.pop("LD_LIBRARY_PATH", None)
    try:
        completed = subprocess.run(
            ["ldconfig", "-p"],
            check=False,
            capture_output=True,
            env=child_environment,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    available = completed.stdout
    return all(name in available for name in CHROMIUM_RUNTIME_LIBRARIES)


def _parse_proxy(value: str) -> tuple[str, str | None, str | None]:
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https", "socks4", "socks5"}:
        raise ValueError("host proxy uses an unsupported scheme")
    if not parts.hostname:
        raise ValueError("host proxy is missing a hostname")
    hostname = (
        f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    )
    netloc = f"{hostname}:{parts.port}" if parts.port is not None else hostname
    server = urlunsplit((parts.scheme.lower(), netloc, "", "", ""))
    username = unquote(parts.username) if parts.username is not None else None
    password = unquote(parts.password) if parts.password is not None else None
    return server, username, password
