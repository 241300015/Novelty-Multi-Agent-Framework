"""Run-scoped, explicit consent before moving to a more costly model tier."""

from __future__ import annotations

import contextvars
import sys
import threading
from dataclasses import asdict, dataclass
from typing import Callable
from uuid import uuid4


MODEL_ORDER = ("local-qwen2.5-7b", "deepseek-flash", "deepseek-official-flash")
MODEL_LABELS = dict(zip(MODEL_ORDER, ("本地模型", "硅基流动 API", "私人 DeepSeek API 账户")))


class ModelSwitchAborted(BaseException):
    """Stop the whole run, including agents that normally catch model errors."""


@dataclass(frozen=True)
class ModelSwitchRequest:
    request_id: str
    source: str
    target: str
    target_label: str
    reason: str
    message: str

    def as_dict(self):
        return asdict(self)


_terminal_lock = threading.Lock()


def terminal_approval(request: ModelSwitchRequest, cancelled: Callable[[], bool]) -> bool:
    with _terminal_lock:
        if cancelled():
            raise ModelSwitchAborted("任务已中断。")
        if not sys.stdin.isatty():
            raise ModelSwitchAborted(request.message + " 当前入口无法交互确认，任务已中断。")
        try:
            return input(request.message + " [y/N] ").strip().lower() in {"y", "yes", "是", "允许"}
        except (EOFError, KeyboardInterrupt):
            return False


class ModelSwitchSession:
    """One consent per target per run; simultaneous failures share the decision."""

    def __init__(self, approve=terminal_approval):
        self.approve = approve
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._approved: set[str] = set()
        self._selected: dict[tuple[str, ...], int] = {}

    def stop(self):
        self._stopped.set()

    def check(self, cancelled=lambda: False):
        if self._stopped.is_set() or cancelled():
            raise ModelSwitchAborted("模型切换被拒绝或任务已中断。")

    def selected(self, chain: tuple[str, ...]) -> int:
        self.check()
        return self._selected.get(chain, 0)

    def switch(self, chain: tuple[str, ...], index: int, reason: str, cancelled=lambda: False):
        # Poll so a cancelled caller never waits indefinitely behind another prompt.
        while not self._lock.acquire(timeout=0.1):
            self.check(cancelled)
        try:
            self.check(cancelled)
            target = chain[index]
            if target not in self._approved:
                label = MODEL_LABELS.get(target, target)
                source = chain[index - 1]
                request = ModelSwitchRequest(
                    uuid4().hex, source, target, label, reason,
                    f"{MODEL_LABELS.get(source, source)}调用失败（{reason}）。"
                    f"是否允许本次任务切换到{label}？论文内容将发送到该服务，可能产生费用；拒绝将中断任务。",
                )
                if self.approve(request, cancelled) is not True:
                    self.stop()
                    raise ModelSwitchAborted("已拒绝切换模型，任务已中断。")
                self.check(cancelled)
                self._approved.add(target)
            self._selected[chain] = max(index, self._selected.get(chain, 0))
        finally:
            self._lock.release()


model_switch_session: contextvars.ContextVar[ModelSwitchSession | None] = contextvars.ContextVar(
    "model_switch_session", default=None
)
