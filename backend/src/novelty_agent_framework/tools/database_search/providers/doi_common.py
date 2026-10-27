"""Transport and identity helpers for public DOI lookup providers.

Provider-specific HTTP miss semantics and record parsing live in each provider.
No probe-script imports, validation decisions, agent calls, or cross-API fallback.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
import math
import re
from typing import Any, Literal
from urllib.parse import quote, unquote, urlsplit

import httpx

from ....ports import DoiLookupResult, DoiMetadataRecord
from ....schemas import EvidenceSource
from .common import HttpRequestPolicy, ProviderConfigurationError, ProviderRequestError, ResilientHttpClient
from .structured_text import markup_to_text


class DoiResponseError(ValueError):
    """The response does not satisfy the provider's DOI/JSON contract."""


def normalize_doi(value: str) -> str:
    """Accept a bare DOI, doi: prefix, or a DOI resolver URL; preserve suffixes."""
    if not isinstance(value, str):
        raise ValueError("DOI must be a string")
    value = value.strip()
    if value.lower().startswith("doi:"):
        value = value[4:].strip()
    if value.lower().startswith(("https://", "http://")):
        parts = urlsplit(value)
        if (parts.netloc.lower() not in {"doi.org", "dx.doi.org"}
                or parts.query or parts.fragment):
            raise ValueError("expected a DOI resolver URL without query/fragment; encode suffix delimiters")
        value = unquote(parts.path[1:], errors="strict")
    if (not re.fullmatch(r"10\.\d{4,9}/\S+", value)
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("expected a DOI in the form 10.<registrant>/<suffix>")
    return value.lower()


def verify_doi(value: Any, requested: str) -> None:
    if not isinstance(value, str) or value.lower() != requested:
        raise DoiResponseError("API record DOI does not match requested DOI")


def json_object(response: httpx.Response) -> dict[str, Any]:
    media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if not (media_type == "application/json" or media_type.endswith("+json")):
        raise DoiResponseError("DOI API returned a non-JSON content type")
    try:
        payload = response.json()
    except (ValueError, UnicodeError):
        raise DoiResponseError("DOI API returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise DoiResponseError("DOI API response must be an object")
    return payload


def text_field(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DoiResponseError("metadata text field must be a string")
    return markup_to_text(value) or None


def object_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise DoiResponseError("metadata field must be a list of objects")
    return value


def publication_year(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise DoiResponseError("publication year must be an integer")
    try:
        year = int(value)
    except ValueError:
        raise DoiResponseError("invalid publication year") from None
    if not 1 <= year <= 9999:
        raise DoiResponseError("invalid publication year")
    return year


def landing_url(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DoiResponseError("metadata URL must be a string")
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password:
        return value
    return None


class PublicDoiApi(ABC):
    """One source's HTTP client; injected clients are owned by the caller.

    Default transport ignores proxy environment variables and uses verified TLS
    over IPv4. No redirects or retries by default. Connection pools are reusable.
    """

    source_id: str
    base_url: str
    path_prefix: str
    coverage: Literal["handle_registry", "crossref_metadata", "datacite_findable"]

    def __init__(
        self, *, client: httpx.Client | None = None, timeout: float = 10.0,
        ip_family: str = "ipv4", policy: HttpRequestPolicy | None = None,
        base_url: str | None = None,
        user_agent: str = "Novelty-DOI-Metadata/1.0",
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ProviderConfigurationError("timeout must be positive and finite")
        if ip_family not in {"auto", "ipv4", "ipv6"}:
            raise ProviderConfigurationError("ip_family must be auto, ipv4, or ipv6")
        self.base_url = (base_url or self.base_url).rstrip("/")
        parts = urlsplit(self.base_url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or parts.path):
            raise ProviderConfigurationError("DOI API base_url must be an HTTPS origin without credentials")
        if not user_agent or not user_agent.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in user_agent):
            raise ProviderConfigurationError("user_agent must contain printable ASCII")
        policy = policy or HttpRequestPolicy(min_interval_seconds=0.5, max_retries=0)
        if any(not math.isfinite(value) for value in (
            policy.min_interval_seconds, policy.retry_backoff_seconds, policy.max_retry_after_seconds
        )):
            raise ProviderConfigurationError("request policy delays must be finite")
        self._owns_client = client is None
        self._closed = False
        self.timeout = timeout
        self.user_agent = user_agent
        if client is None:
            local_address = {"auto": None, "ipv4": "0.0.0.0", "ipv6": "::"}[ip_family]
            client = httpx.Client(
                transport=httpx.HTTPTransport(local_address=local_address, trust_env=False, retries=0),
                trust_env=False, timeout=timeout, follow_redirects=False,
            )
        self.transport = ResilientHttpClient(client, policy=policy, provider=self.source_id)

    @abstractmethod
    def lookup(self, doi: str) -> DoiLookupResult:
        """Read exact DOI facts using this source's response contract."""

    def _get(self, doi: str) -> httpx.Response:
        if self._closed:
            raise RuntimeError("DOI provider is closed")
        # Encode the entire DOI: suffix ?/#/%/slash characters must not alter the endpoint.
        response = self.transport.get(
            self.base_url + self.path_prefix + quote(doi, safe=""),
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
            timeout=self.timeout, follow_redirects=False,
        )
        if response.status_code not in {200, 404}:
            self._http_error(response)
        return response

    def _http_error(self, response: httpx.Response) -> None:
        raise ProviderRequestError(
            f"{self.source_id} API returned HTTP {response.status_code}",
            status_code=response.status_code,
        )

    def _found(self, doi: str, record: DoiMetadataRecord) -> DoiLookupResult:
        return DoiLookupResult(self.source_id, doi, "found", self.coverage, 200, record)

    def _not_found(self, doi: str) -> DoiLookupResult:
        return DoiLookupResult(self.source_id, doi, "not_found", self.coverage, 404)

    def close(self) -> None:
        if not self._closed and self._owns_client:
            self.transport.client.close()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class BibliographicDoiApi(PublicDoiApi):
    """Bridge real bibliographic metadata to the existing MetadataTool contract."""

    def resolve(self, document_id: str) -> EvidenceSource | None:
        result = self.lookup(document_id)
        if result.status == "not_found":
            return None
        record = result.record
        if record is None or not record.title:
            raise DoiResponseError("found DOI has no title for EvidenceSource; use lookup for partial metadata")
        return EvidenceSource(title=record.title, doi=record.doi, url=record.url)


def provider_options(config: Mapping[str, Any]) -> dict[str, Any]:
    if config.get("adapter_only", False):
        raise ProviderConfigurationError("DOI lookup sources do not provide a query adapter")
    return {
        "timeout": float(config.get("timeout_seconds", 10.0)),
        "ip_family": str(config.get("ip_family", "ipv4")),
        "base_url": config.get("base_url"),
        "user_agent": str(config.get("user_agent", "Novelty-DOI-Metadata/1.0")),
        "policy": HttpRequestPolicy(
            min_interval_seconds=float(config.get("min_interval_seconds", .5)),
            max_retries=int(config.get("max_retries", 0)),
            retry_backoff_seconds=float(config.get("retry_backoff_seconds", .5)),
            max_retry_after_seconds=float(config.get("max_retry_after_seconds", 30)),
        ),
    }
