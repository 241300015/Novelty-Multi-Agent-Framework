"""Normalize provider usage payloads and calculate model-call cost in RMB."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


DEFAULT_PRICING_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "llm_pricing.json"
)
_MONEY_PLACES = Decimal("0.00000001")


@dataclass(frozen=True)
class LlmPricingCatalog:
    currency: str
    unit_tokens: int
    timezone_name: str
    models: Mapping[str, Any]
    source_path: Path

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PRICING_PATH) -> "LlmPricingCatalog":
        resolved = Path(path)
        with resolved.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        currency = raw.get("currency")
        unit_tokens = raw.get("unit_tokens")
        timezone_name = raw.get("timezone")
        models = raw.get("models")
        if currency != "RMB":
            raise ValueError("LLM pricing currency must be RMB")
        if (
            not isinstance(unit_tokens, int)
            or isinstance(unit_tokens, bool)
            or unit_tokens < 1
        ):
            raise ValueError("LLM pricing unit_tokens must be a positive integer")
        if not isinstance(timezone_name, str) or not timezone_name:
            raise ValueError("LLM pricing timezone must be a non-empty string")
        ZoneInfo(timezone_name)
        if not isinstance(models, Mapping):
            raise ValueError("LLM pricing models must be an object")
        return cls(currency, unit_tokens, timezone_name, models, resolved)

    def calculate(
        self,
        model: str,
        usage: Mapping[str, Any] | None,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        tokens = normalize_usage(usage)
        result: dict[str, Any] = {
            "status": "USAGE_UNAVAILABLE" if not tokens["available"] else "UNPRICED",
            "currency": self.currency,
            "amount": None,
            "unit_tokens": self.unit_tokens,
            "rate_name": None,
            "rates_per_unit": None,
            "pricing_table": str(self.source_path),
        }
        if not tokens["available"]:
            return {"tokens": tokens, "billing": result}
        if not tokens["billable_breakdown_available"]:
            result["status"] = "USAGE_INCOMPLETE"
            return {"tokens": tokens, "billing": result}

        entry = self.models.get(model)
        rates, rate_name = self._select_rates(entry, occurred_at)
        if rates is None:
            return {"tokens": tokens, "billing": result}

        input_rate = _decimal_rate(rates, "input")
        cached_rate = _decimal_rate(rates, "cached_input", default=input_rate)
        output_rate = _decimal_rate(rates, "output")
        if input_rate is None or cached_rate is None or output_rate is None:
            return {"tokens": tokens, "billing": result}

        cached = min(tokens["cached_input_tokens"], tokens["input_tokens"])
        uncached = tokens["input_tokens"] - cached
        amount = (
            Decimal(uncached) * input_rate
            + Decimal(cached) * cached_rate
            + Decimal(tokens["output_tokens"]) * output_rate
        ) / Decimal(self.unit_tokens)
        amount = amount.quantize(_MONEY_PLACES)
        result.update(
            status="FREE" if amount == 0 else "PRICED",
            amount=float(amount),
            rate_name=rate_name,
            rates_per_unit={
                "input": float(input_rate),
                "cached_input": float(cached_rate),
                "output": float(output_rate),
            },
        )
        return {"tokens": tokens, "billing": result}

    def _select_rates(
        self, entry: Any, occurred_at: datetime
    ) -> tuple[Mapping[str, Any] | None, str | None]:
        if not isinstance(entry, Mapping):
            return None, None
        local_time = occurred_at.astimezone(ZoneInfo(self.timezone_name)).time()
        overrides = entry.get("time_overrides", [])
        if isinstance(overrides, list):
            for override in overrides:
                if not isinstance(override, Mapping):
                    continue
                if _time_in_range(
                    local_time, override.get("start"), override.get("end")
                ):
                    return override, str(override.get("name") or "time_override")
        rates = entry.get("default")
        return (rates, "default") if isinstance(rates, Mapping) else (None, None)


def normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = usage if isinstance(usage, Mapping) else {}
    input_tokens = _token_count(raw.get("prompt_tokens", raw.get("input_tokens")))
    output_tokens = _token_count(
        raw.get("completion_tokens", raw.get("output_tokens"))
    )
    total_tokens = _token_count(raw.get("total_tokens"))
    prompt_details = raw.get(
        "prompt_tokens_details", raw.get("input_tokens_details", {})
    )
    completion_details = raw.get(
        "completion_tokens_details", raw.get("output_tokens_details", {})
    )
    cached_tokens = _detail_token_count(prompt_details, "cached_tokens")
    reasoning_tokens = _detail_token_count(completion_details, "reasoning_tokens")
    available = (
        input_tokens is not None
        or output_tokens is not None
        or total_tokens is not None
    )
    billable_breakdown_available = (
        input_tokens is not None and output_tokens is not None
    )
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    return {
        "available": available,
        "billable_breakdown_available": billable_breakdown_available,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": (
            total_tokens if total_tokens is not None else input_tokens + output_tokens
        ),
    }


def _token_count(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _detail_token_count(value: Any, key: str) -> int:
    if not isinstance(value, Mapping):
        return 0
    return _token_count(value.get(key)) or 0


def _decimal_rate(
    rates: Mapping[str, Any], key: str, *, default: Decimal | None = None
) -> Decimal | None:
    value = rates.get(key)
    if value is None:
        return default
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result >= 0 else None


def _time_in_range(current: time, start_value: Any, end_value: Any) -> bool:
    if not isinstance(start_value, str) or not isinstance(end_value, str):
        return False
    try:
        start = time.fromisoformat(start_value)
        end = time.fromisoformat(end_value)
    except ValueError:
        return False
    if start <= end:
        return start <= current < end
    return current >= start or current < end


__all__ = ["DEFAULT_PRICING_PATH", "LlmPricingCatalog", "normalize_usage"]
