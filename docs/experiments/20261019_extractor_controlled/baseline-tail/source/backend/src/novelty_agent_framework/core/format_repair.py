"""One tool-free format recovery; callers still validate semantics and provenance."""
import json
from dataclasses import replace

from backend.env import ChatMessage, ModelCallOptions


async def repair_json(client, content, schema, options=None, *, validation_error=None):
    payload = {"previous_output": content, "schema": schema}
    if validation_error is not None:
        payload["validation_error"] = str(validation_error)[:2000]
    response = await client.acomplete([
        ChatMessage(role="system", content=(
            "Repair the format of the previous output into the supplied JSON schema. "
            "Return only JSON. Do not add facts, quotes, identifiers or stronger conclusions. "
            "Preserve the existing meaning and exact quotations. Do not call tools. "
            "If the output does not support a verdict, use the schema's insufficient-evidence form."
        )),
        ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    ], options=replace(options or ModelCallOptions(), tools=(), tool_choice="none",
        timeout_seconds=min((options.timeout_seconds if options else None) or 60, 60),
        extra_body={**((options.extra_body if options else None) or {}), "enable_thinking": False}))
    if response.tool_calls:
        raise ValueError("format repair attempted a tool call")
    return response.content
