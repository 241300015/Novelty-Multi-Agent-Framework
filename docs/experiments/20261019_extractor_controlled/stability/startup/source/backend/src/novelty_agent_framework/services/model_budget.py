"""A small per-Web-run model dispatch ledger; reservations are never recycled."""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from threading import RLock

from backend.env import ModelCallBudgetExceeded, ModelCallEvent
from novelty_agent_framework.diagnostics.llm_usage import LlmPricingCatalog


class RunModelBudget:
    def __init__(self, path: Path, *, cap_rmb: Decimal, max_attempts: int | None,
                 pricing: LlmPricingCatalog | None = None, resume: bool = False) -> None:
        if cap_rmb <= 0 or (max_attempts is not None and max_attempts < 1):
            raise ValueError("model budget caps must be positive")
        self.path = path
        self.cap_rmb = cap_rmb
        self.max_attempts = max_attempts
        self.pricing = pricing or LlmPricingCatalog.load()
        self._lock = RLock()
        if resume:
            existing = json.loads(path.read_text())
            if (Decimal(str(existing["cap_rmb"])) != cap_rmb
                    or existing["max_attempts"] != max_attempts):
                raise ValueError("existing run budget caps do not match")
            self._attempts = {
                f"prior-{index}": entry
                for index, entry in enumerate(existing["attempts"])
            }
            self._reserved = Decimal(str(existing["reserved_total_rmb"]))
            if self._reserved < sum(
                (Decimal(str(entry["reserved_rmb"])) for entry in self._attempts.values()),
                Decimal(0),
            ):
                raise ValueError("existing run budget reserve is inconsistent")
        else:
            if path.exists():
                raise FileExistsError(f"run budget ledger already exists: {path}")
            self._attempts = {}
            self._reserved = Decimal(0)
            self._write()

    def __call__(self, event: ModelCallEvent) -> None:
        if event.phase not in {"START", "COMPLETE", "CANCELLED"}:
            return
        with self._lock:
            if event.phase == "START":
                payload = event.request_payload
                if not isinstance(payload, dict) or not isinstance(payload.get("max_tokens"), int):
                    raise ModelCallBudgetExceeded("run budget requires a bounded max_tokens payload")
                rates, _ = self.pricing._select_rates(self.pricing.models.get(event.model), event.started_at)
                if rates is None:
                    raise ModelCallBudgetExceeded("run budget has no price for this model")
                body = json.dumps(payload, ensure_ascii=False).encode()
                # UTF-8 byte count is a conservative input-token estimate for this model family.
                reserve = (Decimal(len(body)) * Decimal(str(rates["input"]))
                           + Decimal(payload["max_tokens"]) * Decimal(str(rates["output"]))) \
                          / Decimal(self.pricing.unit_tokens)
                if ((self.max_attempts is not None and len(self._attempts) >= self.max_attempts)
                        or self._reserved + reserve > self.cap_rmb):
                    raise ModelCallBudgetExceeded("run model budget exhausted before dispatch")
                self._attempts[event.call_id] = {
                    "attempt": len(self._attempts) + 1,
                    "model": event.model,
                    "payload_sha256": hashlib.sha256(body).hexdigest(),
                    "request_bytes": len(body),
                    "max_output_tokens": payload["max_tokens"],
                    "reserved_rmb": float(reserve),
                    "started_at": event.started_at.isoformat(),
                    "status": "reserved_billing_unknown",
                    "actual_bill_rmb": None,
                }
                self._reserved += reserve
            else:
                entry = self._attempts.get(event.call_id)
                if entry is None:
                    return
                if event.phase == "CANCELLED":
                    entry["status"] = "cancelled_transport_outcome_unknown"
                elif event.response is not None:
                    bill = self.pricing.calculate(event.model, event.response.usage,
                                                  occurred_at=event.started_at)["billing"]
                    entry["status"] = "response_received"
                    entry["usage_estimated_charge_rmb"] = bill["amount"]
                    entry["usage_estimate_status"] = bill["status"]
                elif event.error is not None:
                    entry["status"] = "failed_billing_unknown"
                    entry["error_type"] = type(event.error).__name__
            self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"cap_rmb": float(self.cap_rmb), "max_attempts": self.max_attempts,
                "reserved_total_rmb": float(self._reserved),
                "attempts": list(self._attempts.values())}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, self.path)
