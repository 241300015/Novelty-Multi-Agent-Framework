"""Shared infrastructure for authenticated HTTP database providers.

Query syntax and response parsing deliberately stay in provider modules.  This
module only centralizes the parts that should behave consistently across
Springer, IEEE Xplore, ScienceDirect, and future HTTP-backed catalogs:
environment-owned credentials, request throttling, and bounded retries.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ....core.runtime_artifacts import current_runtime_artifacts


class ProviderConfigurationError(ValueError):
    """A provider cannot be built from the supplied non-secret configuration."""


class MissingProviderCredentialError(ProviderConfigurationError):
    """A required provider credential is absent from the environment."""


class ProviderRequestError(RuntimeError):
    """An HTTP provider failed without exposing credential-bearing request URLs."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 transport_error_type: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transport_error_type = transport_error_type


def raise_for_provider_status(response: httpx.Response, *, provider: str) -> None:
    """Raise a sanitized error because some providers put API keys in the URL."""

    if response.is_error:
        raise ProviderRequestError(
            f"{provider} API returned HTTP {response.status_code}",
            status_code=response.status_code
        )


def resolve_env_credential(
    config: Mapping[str, Any],
    *,
    setting: str,
    default_env: str | None = None,
    required: bool = True,
) -> str | None:
    """Resolve one secret without ever accepting or returning it in config dumps."""

    raw_name = config.get(setting, default_env)
    env_name = str(raw_name).strip() if raw_name is not None else ""
    if not env_name:
        if required:
            raise ProviderConfigurationError(
                f"provider setting {setting!r} must name an environment variable"
            )
        return None
    value = os.getenv(env_name)
    if value:
        return value
    if required:
        raise MissingProviderCredentialError(
            f"required provider credential environment variable {env_name!r} is not set"
        )
    return None


@dataclass(frozen=True)
class HttpRequestPolicy:
    """Transport policy shared by one API quota domain."""

    min_interval_seconds: float = 0.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.5
    max_retry_after_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.min_interval_seconds < 0:
            raise ProviderConfigurationError("min_interval_seconds must be non-negative")
        if self.max_retries < 0:
            raise ProviderConfigurationError("max_retries must be non-negative")
        if self.retry_backoff_seconds < 0:
            raise ProviderConfigurationError("retry_backoff_seconds must be non-negative")
        if self.max_retry_after_seconds < 0:
            raise ProviderConfigurationError(
                "max_retry_after_seconds must be non-negative"
            )


class ResilientHttpClient:
    """Small synchronous transport with process-local throttling and retries."""

    _RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        client: httpx.Client,
        *,
        policy: HttpRequestPolicy | None = None,
    ) -> None:
        self.client = client
        self.policy = policy or HttpRequestPolicy()
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(self.policy.max_retries + 1):
            self._throttle()
            runtime = current_runtime_artifacts()
            if runtime is not None:
                runtime.reserve_provider_request(provider="authenticated_database",
                                                 operation=method.upper())
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                # httpx exception messages can include the complete request URL,
                # including provider credentials supplied as query parameters.
                raise ProviderRequestError(
                    f"provider HTTP request failed: {type(exc).__name__}",
                    transport_error_type=type(exc).__name__
                ) from None
            if (
                response.status_code not in self._RETRYABLE_STATUS_CODES
                or attempt >= self.policy.max_retries
            ):
                return response
            time.sleep(self._retry_delay(response, attempt))
        assert response is not None
        return response

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def _throttle(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last_request_at
            remaining = self.policy.min_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
            self._last_request_at = time.monotonic()

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "").strip()
        try:
            requested = float(retry_after)
        except ValueError:
            requested = self.policy.retry_backoff_seconds * (2**attempt)
        return min(max(requested, 0.0), self.policy.max_retry_after_seconds)
