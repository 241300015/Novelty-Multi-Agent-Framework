"""统一模型调用入口。

该模块只负责模型运行配置和通用调用协议，不放具体查新业务逻辑。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
import os
import time
import threading
import uuid
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .context_admission import (ContextAdmissionConfig, ContextTokenMeasurement, ContextRecordingError, ContextMeasurementUnavailable,
    TokenCounter, count_vllm_tokens, diagnostic_endpoint, evaluate_context_admission)


def _load_dev_env() -> None:
    """开发环境可选加载 ``backend/.env``；未安装 python-dotenv 时静默跳过。"""

    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # override=False：真实环境变量优先于 .env，生产部署不受影响
    load_dotenv(env_path, override=False)


_load_dev_env()


class ModelClientError(RuntimeError):
    """模型客户端调用失败；failure_reason 只记录调用边界可核实的事实。"""

    def __init__(self, message: str, *, failure_reason: str | None = None):
        super().__init__(message)
        self.failure_reason = failure_reason


class ModelContextAdmissionError(ModelClientError):
    """A context policy refused this request before chat transport."""

    def __init__(self, admission: Mapping[str, Any]) -> None:
        self.admission = dict(admission)
        self.code = str(admission["code"])
        super().__init__(f"{self.code}: {admission.get('reason') or 'input plus output exceeds effective context window'}")


class ModelTransportTimeout(ModelClientError):
    """The HTTP transport timed out before a complete model response."""


class ModelTraceError(RuntimeError):
    """Runtime trace failed; stop before another paid model request."""


class ModelCallBudgetExceeded(RuntimeError):
    """A run has reached its model-call cap before dispatch."""


@dataclass(frozen=True)
class ModelCallEvent:
    """A model request boundary; the recorder redacts before persistence."""

    alias: str
    provider: str
    model: str
    started_at: datetime
    duration_ms: int
    message_count: int
    response: ModelResponse | None = None
    error: BaseException | None = None
    call_id: str = ""
    phase: str = "COMPLETE"
    request_payload: Mapping[str, Any] | None = None
    request_options: Mapping[str, Any] | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    # Provider consumption can be known before choices/tool arguments validate.
    provider_usage: Mapping[str, Any] | None = None


ModelCallObserver = Callable[[ModelCallEvent], None]
_serialized_call_payload: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("novelty_serialized_call_payload", default=None)
)
_model_call_observer: contextvars.ContextVar[ModelCallObserver | None] = (
    contextvars.ContextVar("novelty_model_call_observer", default=None)
)
_model_call_budget: contextvars.ContextVar[ModelCallObserver | None] = (
    contextvars.ContextVar("novelty_model_call_budget", default=None)
)
_async_call_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "novelty_async_model_call_id", default=None
)
_async_cancel_event: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "novelty_async_model_cancel_event", default=None
)


def set_model_call_observer(
    observer: ModelCallObserver,
) -> contextvars.Token[ModelCallObserver | None]:
    """Install a task-local observer and return the token needed to restore it."""

    return _model_call_observer.set(observer)


def reset_model_call_observer(
    token: contextvars.Token[ModelCallObserver | None],
) -> None:
    _model_call_observer.reset(token)


def set_model_call_budget(budget: ModelCallObserver) -> contextvars.Token[ModelCallObserver | None]:
    return _model_call_budget.set(budget)


def reset_model_call_budget(token: contextvars.Token[ModelCallObserver | None]) -> None:
    _model_call_budget.reset(token)


def _emit_model_call(event: ModelCallEvent) -> None:
    budget = _model_call_budget.get()
    if budget is not None:
        budget(event)
    observer = _model_call_observer.get()
    if observer is None:
        return
    try:
        observer(event)
    except ModelCallBudgetExceeded:
        raise
    except Exception as exc:
        raise ModelTraceError("trace_incomplete: model call recording failed") from exc


@dataclass(frozen=True)
class ImageContentPart:
    """多模态消息中的图片片段（OpenAI vision 格式）。"""

    image_url: str
    detail: str = "high"


ContentPart = str | ImageContentPart


@dataclass(frozen=True)
class ToolDefinition:
    """Provider-independent function tool definition."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class ModelToolCall:
    """A model request to invoke one named tool with JSON-object arguments."""

    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str | Sequence[ContentPart] | None = None
    tool_calls: Sequence[ModelToolCall] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ModelCallOptions:
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: float | None = None
    response_format: Mapping[str, Any] | None = None
    top_p: float | None = None
    stop: Sequence[str] | None = None
    tools: Sequence[ToolDefinition] | None = None
    tool_choice: str | Mapping[str, Any] | None = None
    extra_body: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    content: str | None
    tool_calls: Sequence[ModelToolCall] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)
    usage: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRuntimeConfig:
    provider: str = "openai_compatible"
    model: str = "gpt-4.1-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    default_temperature: float = 0.2
    default_timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls, prefix: str = "NOVELTY") -> "ModelRuntimeConfig":
        """从项目专属环境变量读取配置，并回退到通用 LLM_* 配置。"""

        def read(name: str, default: str | None = None) -> str | None:
            return os.getenv(f"{prefix}_{name}") or os.getenv(f"LLM_{name}") or default

        temperature = read("TEMPERATURE")
        timeout = read("TIMEOUT_SECONDS")
        return cls(
            provider=read("PROVIDER", cls.provider) or cls.provider,
            model=read("MODEL", cls.model) or cls.model,
            base_url=read("BASE_URL", cls.base_url) or cls.base_url,
            api_key=read("API_KEY"),
            default_temperature=(
                float(temperature) if temperature else cls.default_temperature
            ),
            default_timeout_seconds=(
                float(timeout) if timeout else cls.default_timeout_seconds
            ),
        )


@dataclass(frozen=True)
class ModelProfile:
    """单个模型的完整运行描述，供按角色选择不同模型。

    ``supported_params`` 是允许透传给厂商 API 的参数白名单（例如
    ``enable_thinking``、``thinking_budget``、``reasoning_effort``）。
    ``defaults`` 存放模型级默认参数，优先级低于单次调用选项。
    """

    alias: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    provider: str = "openai_compatible"
    context_window: int = 128_000
    supported_params: frozenset[str] = frozenset()
    defaults: Mapping[str, Any] = field(default_factory=dict)
    context_admission: ContextAdmissionConfig = field(default_factory=ContextAdmissionConfig)


class ModelClient(Protocol):
    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: ModelCallOptions | None = None,
    ) -> ModelResponse:
        ...

    async def acomplete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: ModelCallOptions | None = None,
    ) -> ModelResponse:
        ...


class OpenAICompatibleChatClient:
    """兼容 OpenAI chat completions 格式的模型客户端。"""

    def __init__(self, profile: ModelProfile | ModelRuntimeConfig, *,
                 token_counter: TokenCounter | None = None) -> None:
        if isinstance(profile, ModelRuntimeConfig):
            profile = ModelProfile(
                alias="default",
                provider=profile.provider,
                model=profile.model,
                base_url=profile.base_url,
                api_key=profile.api_key,
                defaults={
                    "temperature": profile.default_temperature,
                    "timeout_seconds": profile.default_timeout_seconds,
                },
            )
        self.profile = profile
        self._token_counter = token_counter
        self._context_measurement_mismatch: dict[str, Any] | None = None

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: ModelCallOptions | None = None,
    ) -> ModelResponse:
        started_at = datetime.now(timezone.utc)
        monotonic_started = time.monotonic()
        call_id = _async_call_id.get() or uuid.uuid4().hex
        effective_options = options or ModelCallOptions()
        # Snapshot once: count, trace and transport must refer to identical
        # serialized input even if a caller later mutates a defaults mapping.
        request_payload = json.loads(json.dumps(self._build_payload(messages, effective_options), ensure_ascii=False))
        request_options = {
            "timeout_seconds": (effective_options.timeout_seconds
                                if effective_options.timeout_seconds is not None
                                else self.profile.defaults.get("timeout_seconds", 60.0)),
            "endpoint": diagnostic_endpoint(f"{self.profile.base_url.rstrip('/')}/chat/completions"),
        }
        common = dict(alias=self.profile.alias, provider=self.profile.provider,
                      model=self.profile.model, started_at=started_at,
                      message_count=len(messages), call_id=call_id,
                      request_payload=request_payload, request_options=request_options)
        _emit_model_call(ModelCallEvent(**common, duration_ms=0, phase="START"))
        call_token = _async_call_id.set(call_id)
        payload_token = _serialized_call_payload.set(request_payload)
        try:
            def measurement_event(phase, details):
                try:
                    _emit_model_call(ModelCallEvent(**common, duration_ms=0,
                        phase=phase, details=details))
                except Exception as exc:
                    raise ContextRecordingError("context admission trace could not be recorded") from exc

            config = self.profile.context_admission
            counter = self._token_counter
            if counter is None and config.counter == "vllm":
                counter = lambda payload: count_vllm_tokens(payload,
                    base_url=self.profile.base_url, api_key=self.profile.api_key,
                    config=config, observe=measurement_event)
            if self._context_measurement_mismatch is not None:
                def invalid_counter(_payload):
                    raise ContextMeasurementUnavailable("prior_measurement_usage_mismatch",
                        previous=self._context_measurement_mismatch)
                counter = invalid_counter
            admission = evaluate_context_admission(request_payload,
                profile_context_window=self.profile.context_window, config=config, counter=counter)
            if config.mode != "off":
                measurement_event("CONTEXT_ADMISSION", admission)
            if admission["decision"] == "reject":
                raise ModelContextAdmissionError(admission)
            cancellation = _async_cancel_event.get()
            if cancellation is not None and cancellation.is_set():
                raise asyncio.CancelledError("model call cancelled before chat dispatch")
            response = self._complete(messages, options=options)
            if admission["exact"]:
                usage = response.usage if isinstance(response.usage, Mapping) else {}
                actual = usage.get("prompt_tokens", usage.get("input_tokens"))
                comparable = isinstance(actual, int) and not isinstance(actual, bool) and actual >= 0
                verification = {"status": ("matched" if actual == admission["input_tokens"] else "mismatched")
                                if comparable else "usage_unavailable",
                    "measured_input_tokens": admission["input_tokens"], "provider_input_tokens": actual,
                    "payload_sha256": admission["payload_sha256"]}
                if verification["status"] == "mismatched":
                    self._context_measurement_mismatch = verification
                measurement_event("CONTEXT_MEASUREMENT_VERIFIED", verification)
        except BaseException as exc:
            _emit_model_call(
                ModelCallEvent(
                    **common,
                    phase=("CANCELLED" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError))
                           else "COMPLETE"),
                    duration_ms=max(
                        0, int((time.monotonic() - monotonic_started) * 1000)
                    ),
                    error=exc,
                )
            )
            raise
        finally:
            _serialized_call_payload.reset(payload_token)
            _async_call_id.reset(call_token)
        _emit_model_call(
            ModelCallEvent(
                **common,
                duration_ms=max(
                    0, int((time.monotonic() - monotonic_started) * 1000)
                ),
                response=response,
            )
        )
        return response

    def _complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: ModelCallOptions | None = None,
    ) -> ModelResponse:
        if not self.profile.api_key:
            raise ModelClientError("缺少模型 API Key，请配置 NOVELTY_API_KEY 或 LLM_API_KEY", failure_reason='model.config.credential_missing')

        call_options = options or ModelCallOptions()
        payload = _serialized_call_payload.get()
        if payload is None:
            payload = self._build_payload(messages, call_options)
        endpoint = f"{self.profile.base_url.rstrip('/')}/chat/completions"
        timeout = call_options.timeout_seconds
        if timeout is None:
            timeout = self.profile.defaults.get("timeout_seconds", 60.0)
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.profile.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        def milestone(phase: str, *, provider_usage: Mapping[str, Any] | None = None,
                      **details: Any) -> None:
            _emit_model_call(ModelCallEvent(
                alias=self.profile.alias, provider=self.profile.provider,
                model=self.profile.model, started_at=datetime.now(timezone.utc),
                duration_ms=0, message_count=len(messages),
                call_id=_async_call_id.get() or "", phase=phase, details=details,
                provider_usage=provider_usage,
            ))

        try:
            milestone("TRANSPORT_INVOKED", timeout_seconds=timeout)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                headers = getattr(response, "headers", None)
                milestone("RESPONSE_HEADERS", http_status=getattr(response, "status", None),
                          provider_request_id=(headers.get("x-request-id") if headers else None))
                body = response.read()
                milestone("RESPONSE_BODY_COMPLETE", body_bytes=len(body))
                raw = json.loads(body.decode("utf-8"))
                provider_usage = raw.get("usage") if isinstance(raw, Mapping) else None
                milestone("RESPONSE_PARSED", response_id=raw.get("id") if isinstance(raw, Mapping) else None,
                          usage_available=isinstance(provider_usage, Mapping) and bool(provider_usage),
                          provider_usage=provider_usage if isinstance(provider_usage, Mapping) else None)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ModelClientError(f"模型 HTTP 调用失败: {exc.code} {detail}", failure_reason='http_status') from exc
        except TimeoutError as exc:
            raise ModelTransportTimeout(f"模型传输超时: {exc}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise ModelTransportTimeout(f"模型传输超时: {exc.reason}") from exc
            raise ModelClientError(f"模型网络调用失败: {exc}", failure_reason='model.transport') from exc
        except OSError as exc:
            raise ModelClientError(f"模型网络调用失败: {exc}", failure_reason='model.transport') from exc

        try:
            message = raw["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelClientError(f"模型返回格式不符合 chat completions: {raw}", failure_reason='model.response.envelope_invalid') from exc
        if not isinstance(message, Mapping):
            raise ModelClientError(f"模型返回格式不符合 chat completions: {raw}", failure_reason='model.response.envelope_invalid')

        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ModelClientError("模型返回的 message.content 必须是字符串或 null", failure_reason='model.response.content_type_invalid')
        tool_calls = _parse_tool_calls(message.get("tool_calls"))

        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            raw=raw,
            usage=provider_usage if isinstance(provider_usage, Mapping) else {},
        )

    def _build_payload(
        self,
        messages: Sequence[ChatMessage],
        options: ModelCallOptions,
    ) -> dict[str, Any]:
        """按“单次调用 > 模型默认”合并参数，并过滤厂商参数白名单。"""

        profile = self.profile
        defaults = dict(profile.defaults or {})
        payload: dict[str, Any] = {
            "model": profile.model,
            "messages": [_serialize_message(message) for message in messages],
            "temperature": (
                options.temperature
                if options.temperature is not None
                else defaults.get("temperature", 0.7)
            ),
        }
        for key in ("max_tokens", "top_p", "stop"):
            value = getattr(options, key, None)
            if value is None:
                value = defaults.get(key)
            if value is not None:
                payload[key] = value
        if options.response_format is not None:
            payload["response_format"] = dict(options.response_format)
        elif "response_format" in defaults:
            payload["response_format"] = dict(defaults["response_format"])
        if options.tools:
            payload["tools"] = [_serialize_tool_definition(tool) for tool in options.tools]
        if options.tool_choice is not None:
            payload["tool_choice"] = (
                dict(options.tool_choice)
                if isinstance(options.tool_choice, Mapping)
                else options.tool_choice
            )

        for key, value in defaults.items():
            if key in (
                "temperature",
                "max_tokens",
                "top_p",
                "stop",
                "timeout_seconds",
                "response_format",
            ):
                continue
            if key in profile.supported_params and key not in payload:
                if profile.base_url.rstrip("/") == "https://api.deepseek.com" and key == "enable_thinking":
                    payload["thinking"] = {"type": "enabled" if value else "disabled"}
                else:
                    payload[key] = value
        for key, value in (options.extra_body or {}).items():
            if key in profile.supported_params:
                if profile.base_url.rstrip("/") == "https://api.deepseek.com" and key == "enable_thinking":
                    payload["thinking"] = {"type": "enabled" if value else "disabled"}
                else:
                    payload[key] = value
        return payload

    async def acomplete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: ModelCallOptions | None = None,
    ) -> ModelResponse:
        call_id = uuid.uuid4().hex
        token = _async_call_id.set(call_id)
        cancellation = threading.Event()
        cancel_token = _async_cancel_event.set(cancellation)
        started_at = datetime.now(timezone.utc)
        try:
            # Poll a daemon transport thread. Some hosted event loops do not wake
            # reliably from the cross-thread callback used by asyncio.to_thread().
            # The synchronous transport still runs after coroutine cancellation;
            # its observer records any late result under the same call_id.
            result: concurrent.futures.Future[ModelResponse] = concurrent.futures.Future()
            context = contextvars.copy_context()

            def invoke() -> None:
                if not result.set_running_or_notify_cancel():
                    return
                try:
                    result.set_result(context.run(self.complete, messages, options=options))
                except BaseException as exc:
                    result.set_exception(exc)

            threading.Thread(target=invoke, name=f"model-call-{call_id[:8]}", daemon=True).start()
            while not result.done():
                await asyncio.sleep(0.02)
            return result.result()
        except asyncio.CancelledError as exc:
            cancellation.set()
            effective_options = options or ModelCallOptions()
            _emit_model_call(ModelCallEvent(
                alias=self.profile.alias, provider=self.profile.provider,
                model=self.profile.model, started_at=started_at,
                duration_ms=max(0, int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)),
                message_count=len(messages), call_id=call_id, phase="CANCELLED",
                request_payload=self._build_payload(messages, effective_options),
                request_options={
                    "timeout_seconds": (effective_options.timeout_seconds
                                        if effective_options.timeout_seconds is not None
                                        else self.profile.defaults.get("timeout_seconds", 60.0)),
                    "endpoint": diagnostic_endpoint(f"{self.profile.base_url.rstrip('/')}/chat/completions"),
                }, error=exc,
            ))
            raise
        finally:
            _async_cancel_event.reset(cancel_token)
            _async_call_id.reset(token)


def _serialize_message(message: ChatMessage) -> dict[str, Any]:
    """Serialize normal, assistant tool-call, and tool-result messages."""

    serialized: dict[str, Any] = {
        "role": message.role,
        "content": _serialize_content(message.content),
    }
    if message.tool_calls:
        serialized["tool_calls"] = [
            _serialize_model_tool_call(tool_call) for tool_call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        serialized["tool_call_id"] = message.tool_call_id
    return serialized


def _serialize_content(content: str | Sequence[ContentPart] | None) -> Any:
    """把消息内容序列化为 OpenAI chat completions 的 content 字段。"""

    if content is None or isinstance(content, str):
        return content
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "text", "text": part})
        else:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": part.image_url, "detail": part.detail},
                }
            )
    return parts


def _serialize_tool_definition(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
        },
    }


def _serialize_model_tool_call(tool_call: ModelToolCall) -> dict[str, Any]:
    return {
        "id": tool_call.id,
        "type": "function",
        "function": {
            "name": tool_call.name,
            "arguments": json.dumps(tool_call.arguments, ensure_ascii=False),
        },
    }


def _parse_tool_calls(value: Any) -> tuple[ModelToolCall, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ModelClientError("模型返回的 message.tool_calls 必须是数组", failure_reason='model.response.tool_calls_not_array')

    parsed: list[ModelToolCall] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ModelClientError(f"模型返回的 tool_calls[{index}] 必须是对象", failure_reason='model.response.tool_call_not_object')
        call_id = item.get("id")
        function = item.get("function")
        if not isinstance(call_id, str) or not call_id:
            raise ModelClientError(f"模型返回的 tool_calls[{index}] 缺少 id", failure_reason='model.response.tool_call_id_missing')
        if not isinstance(function, Mapping):
            raise ModelClientError(f"模型返回的 tool_calls[{index}] 缺少 function", failure_reason='model.response.function_missing')
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ModelClientError(f"模型返回的 tool_calls[{index}] 缺少 function.name", failure_reason='model.response.function_name_missing')
        raw_arguments = function.get("arguments")
        if not isinstance(raw_arguments, str):
            raise ModelClientError(
                f"模型返回的 tool_calls[{index}].function.arguments 必须是 JSON 字符串",
                failure_reason='model.response.arguments_not_string',
            )
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ModelClientError(
                f"模型返回的 tool_calls[{index}].function.arguments 不是合法 JSON",
                failure_reason='model.response.arguments_json_invalid',
            ) from exc
        if not isinstance(arguments, dict):
            raise ModelClientError(
                f"模型返回的 tool_calls[{index}].function.arguments 必须是 JSON object",
                failure_reason='model.response.arguments_not_object',
            )
        parsed.append(ModelToolCall(id=call_id, name=name, arguments=arguments))
    return tuple(parsed)


class ModelRegistry:
    """按业务别名注册模型 profile，并为每个别名提供独立的 ModelClient。"""

    def __init__(self, profiles: Mapping[str, ModelProfile]) -> None:
        self._profiles = dict(profiles)
        self._clients: dict[str, ModelClient] = {}

    def client_for(self, alias: str) -> ModelClient:
        profile = self._profiles.get(alias)
        if profile is None:
            raise ModelClientError(f"未注册模型别名: {alias}", failure_reason='model.config.alias_unknown')
        if alias not in self._clients:
            self._clients[alias] = OpenAICompatibleChatClient(profile)
        return self._clients[alias]

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(self._profiles)


def build_model_client(
    config: ModelRuntimeConfig | None = None,
    *, 
    prefix: str = "NOVELTY",
) -> ModelClient:
    runtime_config = config or ModelRuntimeConfig.from_env(prefix)
    if runtime_config.provider != "openai_compatible":
        raise ModelClientError(f"暂不支持模型供应商: {runtime_config.provider}", failure_reason='model.config.provider_unsupported')
    return OpenAICompatibleChatClient(runtime_config)
