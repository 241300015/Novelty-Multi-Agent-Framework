"""Executable, trigger-scoped Coordinator work specifications and choice menus.

Like ResearchReadingPolicy, the model selects only an issued option_id. The
runtime owns Reader arguments and the transition from material review to draft.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from types import MappingProxyType

from backend.env import ModelToolCall

from ..schemas.failures import CodedFailure, FailureCode as C


@dataclass(frozen=True)
class CoordinatorWorkSpec:
    name: str
    action: str
    task_type: str
    material_order: tuple[str, ...]
    draft_label: str
    version: int = 1

    def record(self, code, point_ids):
        specification = asdict(self)
        digest = hashlib.sha256(json.dumps(specification, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"trigger_code": code, "point_ids": list(point_ids), "specification": specification,
                "version": self.version, "sha256": digest,
                "guidance_id": f"coordinator.policy/{self.name}@{self.version}:{digest}"}


COORDINATOR_WORK_SPECS = MappingProxyType({
    "evidence.missing": CoordinatorWorkSpec("count_gap", "research_gap", "research_gap",
        ("evidence_card", "novelty_point", "task_result", "review", "recovery_decision", "paper"),
        "结束材料核对，按有效卡差额及已有覆盖生成完整补检规划"),
    "review.missing_features": CoordinatorWorkSpec("semantic_gap", "research_gap", "feature_supplement",
        ("review", "novelty_point", "evidence_card", "paper", "task_result", "recovery_decision"),
        "结束材料核对，针对 Reviewer 的缺失特征及比较证据生成完整补检规划"),
    "coverage.no_match": CoordinatorWorkSpec("zero_candidates", "replan_query", "replan_query",
        ("task_result", "novelty_point", "recovery_decision", "paper", "evidence_card", "review"),
        "结束材料核对，结合已执行查询调整概念范围与研究方向，生成完整补检规划"),
})


def work_spec(request):
    codes = {tuple(d.trigger_codes) for d in request.recovery_decisions}
    if len(codes) != 1 or len(next(iter(codes))) != 1:
        raise ValueError("Coordinator policy requires exactly one authorized trigger code")
    code = next(iter(codes))[0]
    return code, COORDINATOR_WORK_SPECS[code]


class CoordinatorPlanningPolicy:
    """Material review -> draft; no arbitrary tool calls, paths or pagination."""

    pending_instruction = (
        '补检材料核对阶段尚未结束。只返回当前菜单中的 {"option_id":"..."}；'
        '选择结束材料核对后才能提交决策草稿，不能自行调用工具或填写参数。'
    )

    def __init__(self, registry, scope, *, max_reads=4, max_read_chars=24000):
        self.code, self.spec = work_spec(scope)
        self.stage = "material_review"
        self.version = 0
        self.actions = {}
        self.issued_options = set()
        self.events = []
        self.reads_used = 0
        self.read_chars_used = 0
        self.max_reads = max_reads
        self.max_read_chars = max_read_chars
        self.targets = {r.material_id: r for r in sorted(scope.material_index,
            key=lambda r: self.spec.material_order.index(r.kind))}
        self.offsets = {key: 0 for key in self.targets}
        if "reader" not in registry.names:
            raise ValueError("Coordinator policy requires the Reader registry")
        self._rebuild()

    @property
    def can_finish(self):
        return self.stage == "draft"

    def _rebuild(self):
        self.version += 1
        self.actions = {}
        if self.can_finish:
            return
        remaining = (4000 if self.max_read_chars is None
                     else min(4000, self.max_read_chars - self.read_chars_used))
        if self.reads_used < self.max_reads and remaining > 0:
            for material_id, start in self.offsets.items():
                ref = self.targets[material_id]
                label = (f"{'继续读取' if start else '读取'} {material_id}：{ref.kind}，"
                         f"{ref.point_id or '论文全局'}，字符起点 {start}")
                self.actions[f"v{self.version}_{len(self.actions)}"] = (label, {
                    "material_id": material_id,
                    "content_pointer": "/full_text" if ref.kind == "paper" else "",
                    "char_start": start, "max_chars": remaining})
        self.actions[f"v{self.version}_{len(self.actions)}"] = (self.spec.draft_label, None)

    def choices(self):
        self.issued_options.update(self.actions)
        menu = {key: label for key, (label, _) in self.actions.items()}
        self.events.append({"event": "menu", "stage": self.stage, "options": menu})
        return menu

    def selection(self, arguments, call_id):
        return ModelToolCall(id=call_id, name="choose_coordinator_action", arguments=arguments)

    def resolve(self, call):
        if call.name != "choose_coordinator_action" or set(call.arguments) != {"option_id"}:
            raise CodedFailure(C.HARNESS_DECISION_SCHEMA, "only option_id is accepted")
        option = call.arguments["option_id"]
        if not isinstance(option, str) or option not in self.actions:
            code = C.HARNESS_OPTION_UNKNOWN
            if not isinstance(option, str):
                code = C.HARNESS_DECISION_SCHEMA
            elif option in (label for label, _ in self.actions.values()):
                code = C.HARNESS_OPTION_LABEL
            elif option in self.issued_options:
                code = C.HARNESS_OPTION_STALE
            raise CodedFailure(code, "select an option_id from the current Coordinator menu")
        label, arguments = self.actions[option]
        self.events.append({"event": "selection", "stage": self.stage, "option_id": option,
                            "label": label, "resolved_arguments": arguments})
        if arguments is None:
            self.stage = "draft"
            self._rebuild()
            return None
        return ModelToolCall(id=call.id, name="reader", arguments=dict(arguments))

    def observe(self, call, observation):
        material_id = call.arguments["material_id"]
        self.reads_used += 1
        self.offsets.pop(material_id, None)  # Failed/EOF material is not retried automatically.
        read = observation.payload.get("read_result", {}) if observation.succeeded else {}
        start, end = read.get("char_start"), read.get("char_end")
        if (read.get("material_id") == material_id and type(start) is int and type(end) is int
                and start == call.arguments["char_start"] and end > start):
            self.read_chars_used += end - start
            if read.get("has_more"):
                self.offsets[material_id] = end
        self.events.append({"event": "reader_result", "material_id": material_id,
                            "succeeded": observation.succeeded, "read_id": read.get("read_id"),
                            "next_start": self.offsets.get(material_id)})
        self._rebuild()

    def audit(self, *, forced_reason=None):
        return {"mode": "choice_only", "stage": "draft" if forced_reason else self.stage,
                "forced_finalization_reason": forced_reason, "events": self.events,
                "reads_used": self.reads_used, "read_chars_used": self.read_chars_used}
