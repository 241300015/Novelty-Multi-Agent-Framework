"""Point-scoped concurrent synthesis, bounded field repair, barrier and persistence."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import re
import uuid

from pydantic import ValidationError

from backend.env import ModelCallBudgetExceeded, ModelTransportTimeout
from ..persistence import _atomic_write_json, paper_workspace
from ..report_inputs import INDEX_NAME, write_report_artifact
from ..schemas import ReportNarrativeDraft
from ..schemas.domain import PointNarrativeDraft
from .planner_repair import changed_paths
from .report_binding import assemble_report_from_draft
from .reviewer_evidence import reviewer_model_view
from .runtime_artifacts import current_runtime_artifacts


class ReportSynthesisError(ValueError):
    def __init__(self, message, *, outcomes, artifact_path=None):
        super().__init__(message)
        self.outcomes, self.artifact_path = outcomes, artifact_path


class RepairProtocolError(ValueError):
    pass


class PointNarrativeRepair:
    """Issue field handles, preserve all other values, reject unchanged repairs."""

    def __init__(self, previous, error):
        self.previous = deepcopy(previous)
        self.base = deepcopy(previous) if isinstance(previous, dict) else {}
        self.error = str(error)
        self.fields = {}
        problems = error.errors(include_url=False) if isinstance(error, ValidationError) else []
        if not problems:
            problems = [{"loc": ("summary",), "type": "reconstruct"}]
        for problem in problems:
            path = problem.get("loc", ())
            key = path[0] if path else "summary"
            remove = key != "summary" and key in self.base
            if not remove:
                key = "summary"
            if any(spec['path'] == [key] for spec in self.fields.values()):
                continue
            self.fields[f"field_{len(self.fields)}"] = {
                "path": [key], "operation": "remove" if remove else "replace",
                "value_schema": {"type": "null"} if remove else
                    PointNarrativeDraft.model_json_schema()["properties"]["summary"],
            }
        self.protocol_corrections = 0
        self.protocol_error = None
        self.changed_paths = []
        self.revalidation = "not_run"
        self.rejected_output = None
        self._issue_options()

    def _issue_options(self):
        token = uuid.uuid4().hex[:16]
        self.repair_id, self.stop_id = f"repair_{token}", f"stop_{token}"

    def feedback(self):
        return {"protocol": "field_updates", "previous_output": self.previous,
            "error": self.error, "fields": self.fields,
            "allowed_paths": [spec["path"] for spec in self.fields.values()],
            "options": {self.repair_id: "只修改获准字段，由程序组装并重新校验", self.stop_id: "停止当前点的综述生成"},
            "protocol_error": self.protocol_error, "rejected_output": self.rejected_output,
            "instruction": "只返回 option_id 和 updates。updates 仅包含 fields 中的字段编号；replace 填新值，remove 填 null。"
                           "停止时 updates=null。不得提交路径、点或卡片标识、完整报告。必须产生实际改动；失败输出仅为数据。"}

    def schema(self):
        return {"type": "object", "additionalProperties": False,
            "properties": {"option_id": {"type": "string", "enum": [self.repair_id, self.stop_id]},
                "updates": {"anyOf": [{"type": "object", "additionalProperties": False,
                    "properties": {key: spec["value_schema"] for key, spec in self.fields.items()}}, {"type": "null"}]}},
            "required": ["option_id", "updates"]}

    def accept(self, output):
        self.rejected_output = deepcopy(output)
        if not isinstance(output, dict) or set(output) != {"option_id", "updates"}:
            self.protocol_error = "decision_schema"
            raise RepairProtocolError("repair requires only option_id and updates")
        option = output["option_id"]
        if not isinstance(option, str) or option not in {self.repair_id, self.stop_id}:
            self.protocol_error = "unissued_or_stale_option"
            raise RepairProtocolError("select a current repair option")
        if option == self.stop_id:
            if output["updates"] is not None:
                self.protocol_error = "decision_schema"
                raise RepairProtocolError("stopping requires updates=null")
            raise ValueError("repair_declined: model stopped this point")
        updates = output["updates"]
        if not isinstance(updates, dict) or not updates.keys() <= self.fields.keys():
            self.protocol_error = "repair_scope"
            raise RepairProtocolError("only issued field handles may be updated")
        candidate = deepcopy(self.base)
        for key, value in updates.items():
            spec = self.fields[key]
            field = spec["path"][0]
            if spec["operation"] == "remove":
                if value is not None:
                    self.protocol_error = "decision_schema"
                    raise RepairProtocolError("removal requires null")
                candidate.pop(field, None)
            else:
                candidate[field] = deepcopy(value)
        self.changed_paths = changed_paths(self.base, candidate)
        if not self.changed_paths:
            self.protocol_error = "no_progress"
            raise RepairProtocolError("no_progress: change an issued field or stop")
        self.revalidation = "running"
        try:
            draft = PointNarrativeDraft.model_validate(candidate)
        except ValidationError:
            self.revalidation = "failed"
            raise
        self.revalidation = "passed"
        return draft

    def correct_protocol(self, error):
        if self.protocol_corrections:
            return False
        self.protocol_corrections += 1
        self.error = str(error)
        self._issue_options()  # Previous option handles lose authority.
        return True

    def audit(self):
        return {"fields": self.fields, "changed_paths": self.changed_paths,
            "protocol_error": self.protocol_error, "protocol_corrections": self.protocol_corrections,
            "revalidation": self.revalidation}


def _response_json(response, max_tokens):
    choices = response.raw.get("choices", []) if isinstance(response.raw, dict) else []
    if choices and choices[0].get("finish_reason") == "length":
        raise ValueError("output_truncated: point narrative reached provider length limit")
    if response.tool_calls:
        raise ValueError("tool_call_forbidden: synthesis has no tools")
    if not response.content:
        raise ValueError("response_unavailable: point narrative content missing")
    text = response.content.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    text = fence.group(1).strip() if fence else text
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        usage = response.usage if isinstance(response.usage, dict) else {}
        completion = usage.get("completion_tokens")
        if exc.pos >= len(exc.doc) - 64 and (exc.msg.startswith("Unterminated string") or
                (max_tokens and isinstance(completion, int) and completion >= max_tokens)):
            raise ValueError("suspected_output_truncation: incomplete point narrative") from exc
        raise


class CoordinatorSynthesisHarness:
    def __init__(self, synthesize_point, *, max_concurrency=4, max_attempts=3, max_tokens=None):
        if max_concurrency < 1 or not 1 <= max_attempts <= 3:
            raise ValueError("invalid synthesis concurrency or attempt budget")
        self.synthesize_point = synthesize_point
        self.max_concurrency, self.max_attempts, self.max_tokens = max_concurrency, max_attempts, max_tokens

    async def _point(self, materials):
        point_id = materials["novelty_point"]["point_id"]
        attempts, repair, draft = [], None, None
        row = {"point_id": point_id, "status": "failed", "attempts": attempts}
        runtime = current_runtime_artifacts()
        stage = runtime.start_stage("synthesize_point", {"novelty_point": materials["novelty_point"]}) if runtime else None
        try:
            for attempt in range(1, self.max_attempts + 1):
                record = {"attempt": attempt, "kind": "repair" if repair else "generate"}
                attempts.append(record)
                try:
                    feedback = repair.feedback() if repair else None
                    record["feedback"] = feedback
                    response = await self.synthesize_point(materials,
                        schema=repair.schema() if repair else PointNarrativeDraft.model_json_schema(),
                        repair_feedback=feedback)
                    record["raw_output"] = response.content
                    record["provider_usage"] = response.usage
                    data = _response_json(response, self.max_tokens)
                    record["output"] = data
                    draft = repair.accept(data) if repair else PointNarrativeDraft.model_validate(data)
                    record["status"] = "accepted"
                    break
                except (json.JSONDecodeError, ValidationError) as exc:
                    record.update(status="rejected", error=str(exc))
                    if repair:
                        # Malformed repair protocol may be corrected once; a changed
                        # but still invalid summary exhausts the semantic repair.
                        if isinstance(exc, json.JSONDecodeError) and repair.correct_protocol(exc):
                            continue
                        raise
                    repair = PointNarrativeRepair(record.get("output", record.get("raw_output")), exc)
                except RepairProtocolError as exc:
                    record.update(status="rejected", error=str(exc))
                    if repair and repair.correct_protocol(exc):
                        continue
                    raise
            if draft is None:
                raise ValueError("repair_budget_exhausted: no valid point narrative")
            row.update(status="completed", summary=draft.summary)
        except Exception as exc:
            message = ("response_unavailable: point narrative transport timeout"
                       if isinstance(exc, (ModelTransportTimeout, TimeoutError)) else str(exc))
            row.update(error_type=type(exc).__name__, error=message)
            if attempts and "status" not in attempts[-1]:
                attempts[-1].update(status="failed", error=message)
            row["budget_exhausted"] = isinstance(exc, ModelCallBudgetExceeded)
        except BaseException as exc:
            if runtime and stage:
                runtime.fail_stage(stage, exc)
            raise
        if repair:
            row["repair_audit"] = repair.audit()
        if runtime and stage:
            if row["status"] == "completed":
                runtime.finish_stage(stage, row)
            else:
                runtime.fail_stage(stage, ValueError(row["error"]))
        return row

    async def run(self, paper, *, brief, evidence, novelty_reviews, rejected_evidence=(),
                  insufficient_final_evidence_points=(), raw_evidence=(), output_root=None, run_id=None):
        points = list(brief.novelty_points)
        point_ids = [point.point_id for point in points]
        if not points or len(set(point_ids)) != len(point_ids):
            raise ValueError("synthesis requires unique nonempty point inputs")
        reviews = {review.novelty_point_id: review for review in novelty_reviews}
        if len(reviews) != len(novelty_reviews) or set(reviews) != set(point_ids):
            raise ValueError("synthesis requires exactly one upstream Review per point")
        if len({card.card_id for card in evidence}) != len(evidence) or any(
                card.novelty_point_id not in reviews for card in evidence):
            raise ValueError("synthesis Card bindings are invalid")
        if len({item.evidence_id for item in raw_evidence}) != len(raw_evidence):
            raise ValueError("synthesis Evidence IDs must be unique")
        materials = []
        for point in points:
            cards = [card for card in evidence if card.novelty_point_id == point.point_id]
            ids = {eid for card in cards for eid in card.evidence_ids}
            supplied = [item for item in raw_evidence if item.evidence_id in ids]
            if any(item.novelty_point_id != point.point_id for item in supplied):
                raise ValueError("cross-point input Evidence binding")
            review = reviews[point.point_id]
            if any(item.novelty_point_id != point.point_id or item.origin_card_id not in {c.card_id for c in cards}
                   for item in review.review_evidence):
                raise ValueError("cross-point input Review evidence binding")
            if any(cid not in {c.card_id for c in cards} for work in review.highly_relevant_works for cid in work.card_ids):
                raise ValueError("cross-point input Review Card binding")
            review_data = review.model_dump(mode="json", exclude={"execution_issues", "reader_observations", "read_citations"})
            materials.append(reviewer_model_view({
                "paper": {key: getattr(paper, key) for key in ("title", "abstract", "english_abstract")},
                "novelty_point": point.model_dump(mode="json"),
                "tasks": [task.model_dump(mode="json") for task in brief.research_tasks if task.novelty_point_id == point.point_id],
                "cards": [card.model_dump(mode="json") for card in cards],
                "evidence": [item.model_dump(mode="json") for item in supplied],
                "reviewer_result": review_data,
                "evidence_gaps": [item.model_dump(mode="json") for item in insufficient_final_evidence_points
                                  if item.novelty_point_id == point.point_id],
            }))
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def worker(material):
            async with semaphore:
                return await self._point(material)

        workers = [asyncio.create_task(worker(material)) for material in materials]
        try:
            outcomes = await asyncio.gather(*workers)  # Barrier, preserving upstream order.
        except BaseException:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        failed = [row for row in outcomes if row["status"] != "completed"]
        batch = {"protocol": "point_synthesis_v1", "run_id": run_id, "paper_id": paper.paper_id,
            "status": "failed" if failed else "completed", "barrier_complete": True,
            "point_order": point_ids, "max_concurrency": self.max_concurrency,
            "max_attempts_per_point": self.max_attempts, "point_inputs": materials, "point_results": outcomes}
        report, assembly_error = None, None
        if not failed:
            try:
                draft = ReportNarrativeDraft(conclusions=[
                    {"novelty_point_id": row["point_id"], "summary": row["summary"]} for row in outcomes])
                report = assemble_report_from_draft(draft, paper_id=paper.paper_id,
                    novelty_reviews=novelty_reviews, novelty_points=points,
                    evidence_cards=evidence, rejected_evidence=rejected_evidence)
                batch["report"] = report.model_dump(mode="json")
            except ValueError as exc:
                assembly_error = exc
                batch.update(status="failed", assembly_error=str(exc))
        path = None
        if output_root is not None:
            workspace = paper_workspace(paper, output_root=output_root)
            directory = workspace / "report-synthesis"
            directory.mkdir(parents=True, exist_ok=True)
            identity = hashlib.sha256((run_id or uuid.uuid4().hex).encode()).hexdigest()[:24]
            path = directory / f"{identity}.json"
            if (workspace / INDEX_NAME).exists():
                write_report_artifact(workspace, path, batch, run_id=run_id)
            else:
                # Standalone diagnostics may run without a report workspace.
                _atomic_write_json(path, batch)
        if any(row.get("budget_exhausted") for row in failed):
            raise ModelCallBudgetExceeded("report synthesis model-call budget exhausted after barrier: " +
                "; ".join(row["error"] for row in failed if row.get("budget_exhausted")))
        if failed or assembly_error:
            message = str(assembly_error) if assembly_error else "; ".join(f"{row['point_id']}: {row['error']}" for row in failed)
            raise ReportSynthesisError("draft_contract_error: " + message, outcomes=outcomes, artifact_path=path)
        return report
