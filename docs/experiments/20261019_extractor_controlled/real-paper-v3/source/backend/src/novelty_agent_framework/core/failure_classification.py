"""Classify observed boundary failures without parsing narrative explanations."""
from __future__ import annotations

import urllib.error
import httpx

from ..schemas.failures import FailureCode as C, CodedFailure, FailureScope, make_failure


def provider_failure(exc: Exception, *, scope: FailureScope, occurrence_id: str):
    status = getattr(exc, "status_code", None)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    elif isinstance(exc, urllib.error.HTTPError):
        status = exc.code
    if status is not None:
        code = ({401: C.PROVIDER_AUTHENTICATION, 403: C.PROVIDER_AUTHORIZATION,
                 404: C.PROVIDER_RESOURCE_MISSING, 429: C.PROVIDER_RATE_LIMIT}.get(status)
                or (C.PROVIDER_SERVICE if 500 <= status <= 599 else C.PROVIDER_PROTOCOL))
        message = f"Provider returned HTTP {status}; underlying cause is not established by status alone."
    elif isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        code, message = C.PROVIDER_TIMEOUT, "Provider request timed out."
    elif isinstance(exc, (httpx.ConnectError, ConnectionError)):
        code, message = C.PROVIDER_CONNECTION, "Provider connection failed."
    elif isinstance(exc, (httpx.RequestError, TimeoutError, ConnectionError, urllib.error.URLError)) or getattr(exc, "transport_error_type", None):
        code, message = C.PROVIDER_NETWORK, "Provider transport failed; no search result was established."
    elif type(exc).__name__ == "MissingProviderCredentialError":
        code, message = C.PROVIDER_CREDENTIAL_MISSING, "Provider credential is missing."
    elif isinstance(exc, (ValueError, TypeError)):
        code, message = C.PROVIDER_RESULT_INVALID, "Provider result did not satisfy the expected data contract."
    else:
        code, message = C.PROVIDER_UNKNOWN, "Provider failed for an unclassified reason; no search result was established."
    return make_failure(code, scope=scope.model_copy(update={"node": scope.node or "provider.request"}),
                        message=message, occurrence_id=occurrence_id)


def material_failure_code(exc: Exception, *, phase: str = "artifact"):
    if isinstance(exc, CodedFailure):
        return exc.code
    if isinstance(exc, FileNotFoundError):
        return C.READER_FILE_MISSING
    if isinstance(exc, PermissionError):
        return C.READER_ACCESS_DENIED
    if isinstance(exc, UnicodeDecodeError):
        return C.READER_DECODE_FAILED
    if isinstance(exc, OSError):
        return C.MATERIAL_UNAVAILABLE  # Other I/O causes are not established.
    return C.READER_MANIFEST_INVALID if phase == "manifest" else C.READER_VERIFICATION_FAILED


def output_failure_code(exc: Exception):
    """Separate syntax from schema using typed validation details, not prose."""
    import json
    from pydantic import ValidationError
    if isinstance(exc, CodedFailure):
        return exc.code
    if isinstance(exc, json.JSONDecodeError):
        return C.MODEL_JSON_INVALID
    if isinstance(exc, ValidationError):
        return C.MODEL_JSON_INVALID if any(e["type"] == "json_invalid" for e in exc.errors()) else C.MODEL_SCHEMA_INVALID
    return C.MODEL_OUTPUT_SCHEMA


def model_failure_code(exc: Exception):
    from backend.env.model_client import (
        ModelCallBudgetExceeded, ModelContextAdmissionError, ModelTransportTimeout, ModelClientError,
    )
    if isinstance(exc, CodedFailure):
        return exc.code
    if isinstance(exc, ModelCallBudgetExceeded):
        return C.MODEL_CALL_BUDGET
    if isinstance(exc, ModelContextAdmissionError):
        return C.MODEL_CONTEXT_LIMIT if exc.code == "CONTEXT_LIMIT_EXCEEDED" else C.MODEL_CONTEXT_UNAVAILABLE
    if isinstance(exc, (TimeoutError, ModelTransportTimeout)):
        return C.MODEL_TIMEOUT
    if isinstance(exc, ModelClientError):
        reason = getattr(exc, "failure_reason", None)
        if reason == "http_status" and isinstance(exc.__cause__, urllib.error.HTTPError):
            status = exc.__cause__.code
            return {401:C.MODEL_HTTP_AUTH, 403:C.MODEL_HTTP_DENIED, 429:C.MODEL_HTTP_RATE}.get(
                status, C.MODEL_HTTP_SERVER if 500 <= status <= 599 else C.MODEL_HTTP_REJECTED)
        if reason is not None:
            try:
                return C(reason)
            except ValueError:
                pass
        return C.MODEL_TRANSPORT  # Old/custom clients supply no typed cause.
    # Do not label an arbitrary client/programming exception a transport failure.
    return C.UNKNOWN
