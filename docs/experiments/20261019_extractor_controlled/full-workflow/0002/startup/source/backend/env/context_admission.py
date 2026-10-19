"""Exact, non-destructive context admission for serialized model requests.

A counter must include the serving chat template, tool schemas and all prompt
transformations. Missing measurements are explicit; character estimates never
qualify as token measurements. Nothing in this module edits model input.
"""
from __future__ import annotations

import hashlib
import copy
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ContextAdmissionConfig:
    mode: Literal["off", "observe", "enforce"] = "off"
    counter: Literal["none", "vllm"] = "none"
    on_unavailable: Literal["allow", "reject"] = "reject"
    timeout_seconds: float = 5.0
    tokenize_path: str = "/tokenize"
    vllm_tools_mode: Literal["native", "v0_8_5_kwargs"] = "native"

    def __post_init__(self) -> None:
        if self.mode not in {"off", "observe", "enforce"}:
            raise ValueError("invalid context admission mode")
        if self.counter not in {"none", "vllm"}:
            raise ValueError("invalid context token counter")
        if self.on_unavailable not in {"allow", "reject"}:
            raise ValueError("invalid context measurement unavailable policy")
        if self.vllm_tools_mode not in {"native", "v0_8_5_kwargs"}:
            raise ValueError("invalid vLLM tools tokenization mode")
        if not 0 < self.timeout_seconds <= 60:
            raise ValueError("context measurement timeout must be in (0, 60]")
        parsed = urllib.parse.urlsplit(self.tokenize_path)
        if (not self.tokenize_path.startswith("/") or self.tokenize_path.startswith("//")
                or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment):
            raise ValueError("tokenize_path must be a same-origin absolute path")


@dataclass(frozen=True)
class ContextTokenMeasurement:
    input_tokens: int
    source: str
    exact: bool = True
    server_context_window: int | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


class ContextRecordingError(RuntimeError):
    """Failure to record a preflight boundary must stop dispatch in every mode."""


class ContextMeasurementUnavailable(ValueError):
    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


TokenCounter = Callable[[Mapping[str, Any]], ContextTokenMeasurement]
MeasurementObserver = Callable[[str, Mapping[str, Any]], None]


def payload_fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def evaluate_context_admission(
    payload: Mapping[str, Any], *, profile_context_window: int,
    config: ContextAdmissionConfig, counter: TokenCounter | None,
) -> dict[str, Any]:
    """Return an auditable decision on precisely this payload; never truncate it."""
    output_key = ("max_completion_tokens" if payload.get("max_completion_tokens") is not None
                  else "max_tokens")
    output = payload.get(output_key)
    result: dict[str, Any] = {
        "mode": config.mode, "counter": config.counter, "on_unavailable": config.on_unavailable,
        "vllm_tools_mode": config.vllm_tools_mode,
        "payload_sha256": payload_fingerprint(payload), "status": "disabled",
        "code": "CONTEXT_ADMISSION_DISABLED", "decision": "allow", "exact": False,
        "input_tokens": None, "output_tokens_reserved": output,
        "output_reservation_field": output_key, "requested_total_tokens": None,
        "profile_context_window": profile_context_window, "server_context_window": None,
        "effective_context_window": profile_context_window, "window_source": "profile_only",
        "source": None,
    }
    if config.mode == "off":
        return result
    try:
        if not _integer(profile_context_window, minimum=1):
            raise ContextMeasurementUnavailable("invalid_profile_context_window")
        if not _integer(output, minimum=1):
            raise ContextMeasurementUnavailable("explicit_output_reservation_required")
        if counter is None:
            raise ContextMeasurementUnavailable("no_exact_token_counter")
        measured = counter(copy.deepcopy(payload))
        if not isinstance(measured, ContextTokenMeasurement):
            raise ContextMeasurementUnavailable("invalid_counter_result")
        if measured.exact is not True:
            raise ContextMeasurementUnavailable("counter_is_not_exact", source=measured.source)
        if not _integer(measured.input_tokens) or not measured.source:
            raise ContextMeasurementUnavailable("invalid_exact_token_count")
        server_window = measured.server_context_window
        if server_window is not None and not _integer(server_window, minimum=1):
            raise ContextMeasurementUnavailable("invalid_server_context_window")
        window = (min(profile_context_window, server_window)
                  if server_window is not None else profile_context_window)
        total = measured.input_tokens + output
        exceeds = total > window
        result.update(status="context_limit" if exceeds else "admitted",
                      code="CONTEXT_LIMIT_EXCEEDED" if exceeds else "CONTEXT_ADMITTED",
                      decision="reject" if exceeds and config.mode == "enforce" else "allow",
                      exact=True, input_tokens=measured.input_tokens,
                      requested_total_tokens=total, server_context_window=server_window,
                      effective_context_window=window,
                      window_source="minimum_of_profile_and_server" if server_window is not None else "profile_only",
                      source=measured.source, measurement_details=dict(measured.details),
                      would_reject=exceeds)
    except ContextRecordingError:
        raise
    except Exception as exc:
        unavailable = exc if isinstance(exc, ContextMeasurementUnavailable) else ContextMeasurementUnavailable(
            "counter_failed", error_type=type(exc).__name__)
        result.update(status="measurement_unavailable", code="CONTEXT_MEASUREMENT_UNAVAILABLE",
                      decision=("reject" if config.mode == "enforce" and config.on_unavailable == "reject" else "allow"),
                      reason=unavailable.reason, measurement_details=unavailable.details,
                      would_reject=config.on_unavailable == "reject")
    return result


# Request fields that affect decoding, not the input chat template. Everything
# else must be represented by TokenizeChatRequest, or the measurement is refused.
_DECODE_ONLY_FIELDS = {
    "temperature", "max_tokens", "max_completion_tokens", "top_p", "top_k", "min_p",
    "stop", "stop_token_ids", "seed", "n", "stream", "stream_options", "user",
    "frequency_penalty", "presence_penalty", "repetition_penalty", "logit_bias",
    "logprobs", "top_logprobs", "response_format", "request_id", "priority",
}
_TEMPLATE_FIELDS = {
    "model", "messages", "tools", "chat_template", "chat_template_kwargs",
    "add_generation_prompt", "continue_final_message", "add_special_tokens",
}


def diagnostic_endpoint(url: str) -> str:
    """Keep routable location metadata without URL credentials or query secrets."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None:
            host += f":{parts.port}"
        return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "invalid_endpoint"


def _json_request(
    url: str, *, data: Mapping[str, Any] | None, api_key: str | None,
    timeout: float, kind: str, observe: MeasurementObserver | None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url,
        data=None if data is None else json.dumps(data, ensure_ascii=False).encode(),
        headers=headers, method="GET" if data is None else "POST")
    if observe:
        observe("CONTEXT_MEASUREMENT_REQUEST", {"kind": kind, "endpoint": diagnostic_endpoint(url),
            "method": request.method, "payload_sha256": None if data is None else payload_fingerprint(data)})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.load(response)
        if not isinstance(value, dict):
            raise ContextMeasurementUnavailable("invalid_tokenizer_response", kind=kind)
    except Exception as exc:
        details = {"kind": kind, "error_type": type(exc).__name__,
                   **({"http_status": exc.code} if isinstance(exc, urllib.error.HTTPError) else {})}
        if observe:
            observe("CONTEXT_MEASUREMENT_FAILED", details)
        if isinstance(exc, ContextMeasurementUnavailable):
            raise
        raise ContextMeasurementUnavailable("tokenizer_request_failed", **details) from exc
    if observe:
        observe("CONTEXT_MEASUREMENT_RESPONSE", {"kind": kind,
            **({"count": value.get("count"), "max_model_len": value.get("max_model_len")}
               if kind == "tokenize" else {})})
    return value


def count_vllm_tokens(
    payload: Mapping[str, Any], *, base_url: str, api_key: str | None,
    config: ContextAdmissionConfig, observe: MeasurementObserver | None = None,
) -> ContextTokenMeasurement:
    """Use a same-origin vLLM tokenizer only for a representable text chat.

    The default native mode requires schema support for every template field.
    The opt-in v0_8_5_kwargs mode forwards tools through the documented template
    kwargs path only on the two locally verified 0.8.5 server versions.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ContextMeasurementUnavailable("invalid_chat_messages")
    if any(not isinstance(message, dict) or
           not (message.get("content") is None or isinstance(message.get("content"), str))
           for message in messages):
        raise ContextMeasurementUnavailable("multimodal_or_nontext_template_not_verified")
    choice = payload.get("tool_choice")
    if choice not in (None, "auto", "none"):
        raise ContextMeasurementUnavailable("tool_choice_template_not_verified")
    if choice == "none" and payload.get("tools"):
        raise ContextMeasurementUnavailable("disabled_tools_template_policy_unknown")
    unsupported = set(payload) - _DECODE_ONLY_FIELDS - _TEMPLATE_FIELDS - {"tool_choice"}
    if unsupported:
        raise ContextMeasurementUnavailable("unsupported_prompt_parameters", parameters=sorted(unsupported))
    origin_parts = urllib.parse.urlsplit(base_url)
    if origin_parts.scheme not in {"http", "https"} or not origin_parts.netloc:
        raise ContextMeasurementUnavailable("invalid_model_endpoint")
    if (origin_parts.username is not None or origin_parts.password is not None
            or origin_parts.query or origin_parts.fragment):
        raise ContextMeasurementUnavailable("model_endpoint_contains_credentials_or_query")
    origin = urllib.parse.urlunsplit((origin_parts.scheme, origin_parts.netloc, "", "", ""))
    schema = _json_request(origin + "/openapi.json", data=None, api_key=api_key,
        timeout=config.timeout_seconds, kind="schema", observe=observe)
    properties = schema.get("components", {}).get("schemas", {}).get("TokenizeChatRequest", {}).get("properties", {})
    chat_properties = schema.get("components", {}).get("schemas", {}).get("ChatCompletionRequest", {}).get("properties", {})
    if not isinstance(properties, dict) or not {"model", "messages"}.issubset(properties):
        raise ContextMeasurementUnavailable("tokenizer_chat_schema_unavailable")
    projected = {key: copy.deepcopy(value) for key, value in payload.items() if key in _TEMPLATE_FIELDS}
    server_version = None
    if config.vllm_tools_mode == "v0_8_5_kwargs":
        version = _json_request(origin + "/version", data=None, api_key=api_key,
            timeout=config.timeout_seconds, kind="version", observe=observe)
        server_version = version.get("version")
        if server_version not in {"0.8.5", "0.8.5.post1"}:
            raise ContextMeasurementUnavailable("legacy_tokenizer_version_not_verified", server_version=server_version)
        if "chat_template_kwargs" not in properties:
            raise ContextMeasurementUnavailable("legacy_tokenizer_template_kwargs_unavailable")
        if projected.get("tools"):
            # v0.8.5 serving_engine._preprocess_chat builds tools=tool_dicts,
            # then updates it with chat_template_kwargs. Preserve that precedence.
            kwargs = projected.setdefault("chat_template_kwargs", {})
            if not isinstance(kwargs, dict):
                raise ContextMeasurementUnavailable("invalid_chat_template_kwargs")
            kwargs.setdefault("tools", projected.pop("tools"))
    missing = set(projected) - set(properties)
    if missing:
        raise ContextMeasurementUnavailable("tokenizer_missing_template_fields", fields=sorted(missing))
    # An omitted template option must mean the same thing on both endpoints.
    for key in ("add_generation_prompt", "continue_final_message", "add_special_tokens"):
        if key not in projected:
            if key not in properties or key not in chat_properties or (
                properties[key].get("default") != chat_properties[key].get("default")
            ):
                raise ContextMeasurementUnavailable("template_defaults_not_verified", field=key)
            projected[key] = properties[key].get("default")
    response = _json_request(origin + config.tokenize_path, data=projected, api_key=api_key,
        timeout=config.timeout_seconds, kind="tokenize", observe=observe)
    count, window = response.get("count"), response.get("max_model_len")
    if not _integer(count) or not _integer(window, minimum=1):
        raise ContextMeasurementUnavailable("invalid_tokenizer_count_or_window")
    tokens = response.get("tokens")
    if not isinstance(tokens, list) or len(tokens) != count or any(not _integer(token) for token in tokens):
        raise ContextMeasurementUnavailable("tokenizer_count_ids_mismatch")
    return ContextTokenMeasurement(input_tokens=count, source="vllm_tokenize", exact=True,
        server_context_window=window, details={"endpoint": diagnostic_endpoint(origin + config.tokenize_path),
            "tokenize_payload_sha256": payload_fingerprint(projected),
            "template_fields": sorted(projected), "schema_verified": True,
            "vllm_tools_mode": config.vllm_tools_mode, "server_version": server_version})
