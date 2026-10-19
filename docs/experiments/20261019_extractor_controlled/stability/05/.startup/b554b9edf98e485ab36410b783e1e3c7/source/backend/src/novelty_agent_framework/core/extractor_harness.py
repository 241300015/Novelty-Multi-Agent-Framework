"""Bounded generation/review/repair over versioned PaperInput, with issued choices.

The model judges semantics; Python owns identities, menus, patches and budgets.
This module never opens a PDF or calls a parser/provider other than its model client.
"""
from __future__ import annotations

import hashlib
import json
from itertools import combinations
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import Field

from backend.env import ChatMessage, ModelCallOptions, PromptLibrary
from ..schemas.domain import NoveltyPoint, PaperDigest, StrictModel
from ..schemas.failures import FailureCode as C
from .failure_classification import model_failure_code, output_failure_code
from .paper_reading import PaperReader, PaperReadingError


@dataclass(frozen=True)
class ExtractorHarnessConfig:
    enabled: bool = False
    generation_calls: int = 3
    review_calls: int = 3
    reading_calls: int = 3
    max_read_segments: int = 24
    max_read_chars: int = 20000
    generate_prompt: str = "extractor/generate"
    review_prompt: str = "extractor/semantic_review"
    reader_prompt: str = "extractor/reader_choices"
    correction_prompt: str = "extractor/correct"

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in (
            self.generation_calls, self.review_calls, self.reading_calls,
            self.max_read_segments, self.max_read_chars,
        )):
            raise ValueError("Extractor budgets must be positive integers")


class DraftPoint(StrictModel):
    claim: str = Field(min_length=1)
    claim_en: str = ""
    technical_features: list[str] = Field(min_length=1)
    technical_features_en: list[str] = Field(default_factory=list)
    source_locations: list[str] = Field(min_length=1)


class SemanticIssue(StrictModel):
    code: Literal["unsupported", "scope", "duplicate", "mixed", "citation"]
    reason: str = Field(min_length=1)
    related_point_ids: list[str] = Field(default_factory=list)
    source_locations: list[str] = Field(min_length=1)


class PointReview(StrictModel):
    point_id: str
    status: Literal["pass", "revise"]
    issues: list[SemanticIssue] = Field(default_factory=list)


class MissingContribution(StrictModel):
    reason: str = Field(min_length=1)
    source_locations: list[str] = Field(min_length=1)


class ReadRequest(StrictModel):
    option_id: str
    reason: str = Field(min_length=1)


class CandidateComparison(StrictModel):
    option_id: str
    relation: Literal["duplicate", "distinct", "uncertain"]
    reason: str = Field(min_length=1)
    source_locations: list[str] = Field(min_length=1)


class ReviewDraft(StrictModel):
    point_reviews: list[PointReview]
    missing_contributions: list[MissingContribution]
    # No request means no tool action; an omitted empty request list carries no
    # semantic judgment and need not spend a model turn to manufacture [].
    read_requests: list[ReadRequest] = Field(default_factory=list)
    comparisons: list[CandidateComparison] = Field(default_factory=list)


class ExtractorHarnessError(ValueError):
    def __init__(self, code: C, message: str):
        self.code = code
        super().__init__(f"{code.value}: {message}")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ExtractorHarness:
    def __init__(self, client, *, config=None, prompts=None, model_options=None):
        self.client = client
        self.config = config or ExtractorHarnessConfig(enabled=True)
        self.prompts = prompts or PromptLibrary(Path(__file__).parents[1] / "prompts")
        self.model_options = model_options or ModelCallOptions(temperature=0.0)
        self.used = {"generate": 0, "review": 0, "read": 0}
        self.limits = dict(generate=self.config.generation_calls,
                           review=self.config.review_calls, read=self.config.reading_calls)
        self.points: dict[str, NoveltyPoint] = {}
        self.approved: dict[str, NoveltyPoint] = {}
        self.version = 0
        self.next_id = 0
        self.generation_feedback = []
        self.trace = {"harness": "extractor_choices_v1", "calls": [], "events": [],
                      "versions": [], "reviews": [], "corrections": [],
                      "execution_status": "running", "coverage_complete": False,
                      "semantic_completeness_verified": False}

    def _event(self, code, phase, message, **extra):
        row = {"code": code.value, "node": f"extract_points.{phase}",
               "message": message, "candidate_version": self.version, **extra}
        self.trace["events"].append(row)
        return row

    def _response_schema(self, role, context):
        """Guide local decoding as well as validating after generation."""
        def obj(properties):
            return {"type": "object", "additionalProperties": False,
                    "properties": properties, "required": list(properties)}
        def ids(values):
            return {"type": "string", "enum": sorted(values)}
        if role == "read":
            return obj({"option_ids": {"type": "array", "minItems": 1,
                "maxItems": 1,
                "items": ids(o["option_id"] for o in context["options"])},
                "reason": {"type": "string", "minLength": 1}})
        if role == "review":
            schema = ReviewDraft.model_json_schema()
            schema["properties"]["read_requests"]["maxItems"] = 1
            if self.points:
                schema["$defs"]["PointReview"]["properties"]["point_id"] = ids(self.points)
            pairs = [p["option_id"] for p in context["comparison_options"]]
            if pairs:
                schema["$defs"]["CandidateComparison"]["properties"]["option_id"] = ids(pairs)
            else:
                schema["properties"]["comparisons"]["maxItems"] = 0
            choices = [o["option_id"] for o in context["reader_options"]]
            if choices:
                schema["$defs"]["ReadRequest"]["properties"]["option_id"] = ids(choices)
            else:
                schema["properties"]["read_requests"]["maxItems"] = 0
            for name in ("SemanticIssue", "MissingContribution", "CandidateComparison"):
                schema["$defs"][name]["properties"]["source_locations"]["items"] = ids(self._sources())
            # Make reading and adjudication mutually exclusive on the wire too.
            schema["anyOf"] = [
                {"properties": {"read_requests": {"maxItems": 0},
                    "point_reviews": {"minItems": len(self.points), "maxItems": len(self.points)},
                    "comparisons": {"minItems": len(pairs), "maxItems": len(pairs)}}},
                {"properties": {"read_requests": {"minItems": 1},
                    "point_reviews": {"maxItems": 0}, "missing_contributions": {"maxItems": 0},
                    "comparisons": {"maxItems": 0}}},
            ] if choices else [{"properties": {"point_reviews": {
                "minItems": len(self.points), "maxItems": len(self.points)},
                "comparisons": {"minItems": len(pairs), "maxItems": len(pairs)}}}]
            return schema
        point = DraftPoint.model_json_schema()
        point["properties"]["source_locations"]["items"] = ids(self._sources())
        if context["phase"] == "generate":
            return obj({"option_id": ids([context["option_id"]]),
                        "points": {"type": "array", "maxItems": 8, "items": point}})
        repair = obj({
            "option_id": ids(o["option_id"] for o in context["options"]),
            "points": {"type": "array", "maxItems": 3, "items": point}})
        repair["properties"]["reason"] = {"type": "string", "minLength": 1}
        return obj({"repairs": {"type": "array", "minItems": 1, "items": repair}})

    def _request(self, role: str, context: dict, validate: Callable):
        """Retry invalid output once, under the owning role's independent quota."""
        prompt_name = {"generate": self.config.generate_prompt,
                       "review": self.config.review_prompt, "read": self.config.reader_prompt}[role]
        seen = set()
        correction_record = None
        for attempt in range(2):
            if self.used[role] >= self.limits[role]:
                if correction_record is not None:
                    correction_record.update(status="stopped", stop_reason="role_budget_exhausted")
                raise ExtractorHarnessError(C.BUDGET_TURNS, f"{role} budget exhausted")
            self.used[role] += 1
            payload = {**context, "budget": {"used": dict(self.used), "limits": self.limits}}
            prompt = self.prompts.render(prompt_name, context_json=json.dumps(payload, ensure_ascii=False))
            record = {"role": role, "prompt_name": prompt_name, "prompt_version": prompt.version,
                      "candidate_version": self.version, "system_sha256": _digest(prompt.system),
                      "input": payload, "output": None}
            self.trace["calls"].append(record)
            try:
                response = self.client.complete(
                    [ChatMessage(role="system", content=prompt.system), ChatMessage(role="user", content=prompt.user)],
                    options=replace(self.model_options, response_format={"type": "json_schema", "json_schema": {
                        "name": f"extractor_{role}", "strict": True,
                        "schema": self._response_schema(role, context)}}, tools=(), tool_choice="none"),
                )
            except Exception as exc:
                code = model_failure_code(exc)
                record.update(validation_status="transport_failed", validation_error=str(exc))
                if correction_record is not None:
                    correction_record.update(status="stopped", stop_reason="model_call_failed")
                self._event(code, role, str(exc), action="stop")
                raise ExtractorHarnessError(code, str(exc)) from exc
            record["output"] = response.content
            record["usage"] = dict(response.usage)
            try:
                if response.tool_calls:
                    raise ExtractorHarnessError(C.HARNESS_NATIVE_CALL, "native tool calls are not authorized")
                result = validate(json.loads(response.content))
                record["validation_status"] = "passed"
                if correction_record is not None:
                    correction_record["status"] = "verified"
                return result
            except (ValueError, TypeError, KeyError) as exc:
                record["validation_status"] = "failed"
                record["validation_error"] = str(exc)
                code = getattr(exc, "code", output_failure_code(exc))
                failure = self._event(code, role, str(exc), action="correct_output")
                signature = _digest(response.content)
                if signature in seen or attempt == 1:
                    if correction_record is not None:
                        correction_record.update(status="stopped", stop_reason="invalid_output_persisted")
                    raise ExtractorHarnessError(code, "invalid output persisted after one correction") from exc
                seen.add(signature)
                procedure = self.prompts.render(self.config.correction_prompt,
                    context_json=json.dumps({"failure": failure, "role": role}, ensure_ascii=False))
                context = {**context, "correction": {"procedure": procedure.user,
                    "failed_output": response.content, "error": str(exc),
                    "instruction": "只修正本次契约错误；选项和目标不变，不能执行新动作。"}}
                correction_record = {"kind": "legality", "role": role,
                    "failure": failure, "procedure_version": procedure.version, "status": "attempted"}
                self.trace["corrections"].append(correction_record)

    def _read_options(self):
        remaining = [row for row in self.index["segments"] if row["segment_id"] not in self.reads]
        available_chars = self.config.max_read_chars - sum(len(r["text"]) for r in self.reads.values())
        available_segments = self.config.max_read_segments - self.read_attempts
        if available_segments <= 0:
            return {}
        options = {}
        for row in remaining:
            if row["end"] - row["start"] <= available_chars:
                oid = f"{self.identity}:read:{row['segment_id']}"
                options[oid] = {"option_id": oid, "kind": "read_segment", **row}
        if remaining and len(remaining) <= available_segments and sum(r["end"] - r["start"] for r in remaining) <= available_chars:
            oid = f"{self.identity}:read_all"
            options[oid] = {"option_id": oid, "kind": "read_all", "segment_ids": [r["segment_id"] for r in remaining]}
        return options

    def _selected_segments(self, ids, menu):
        if not isinstance(ids, list) or len(ids) != 1 or any(not isinstance(i, str) or i not in menu for i in ids):
            raise ExtractorHarnessError(C.TOOL_ARGUMENT_SCHEMA, "choose exactly ONE issued Reader option per turn; further reads require another reviewed request")
        segments = []
        for oid in ids:
            entry = menu[oid]
            segments.extend(entry.get("segment_ids", [entry.get("segment_id")]))
        if len(set(segments)) != len(segments):
            raise ExtractorHarnessError(C.TOOL_ARGUMENT_SCHEMA, "duplicate or overlapping Reader choices")
        rows = {r["segment_id"]: r for r in self.index["segments"]}
        total = sum(rows[s]["end"] - rows[s]["start"] for s in segments)
        if (len(segments) + self.read_attempts > self.config.max_read_segments
                or total + sum(len(r["text"]) for r in self.reads.values()) > self.config.max_read_chars):
            raise ExtractorHarnessError(C.TOOL_ARGUMENT_SCHEMA, "Reader selection exceeds remaining read budget")
        return segments

    def _execute_reads(self, ids, menu, *, origin):
        segments = self._selected_segments(ids, menu)
        for segment in segments:
            for retry in range(2):
                if self.read_attempts >= self.config.max_read_segments:
                    raise ExtractorHarnessError(C.BUDGET_TURNS, "Reader execution budget exhausted")
                self.read_attempts += 1
                try:
                    chunk = self.reader.read(segment)
                    break
                except PaperReadingError as exc:
                    self._event(exc.code, "read", str(exc), segment_id=segment, action="stop")
                    # Identity, missing file and hash failures are terminal. Only an
                    # unspecified I/O verification failure gets one same-version retry.
                    if exc.code != C.READER_VERIFICATION_FAILED or retry:
                        raise
                    self._event(exc.code, "read", "retry same frozen input once", action="retry_same_version")
            self.reads[segment] = chunk
            self.trace["reading"]["reads"].append({**chunk, "origin": origin})
            self.trace["reading"]["unread_segments"] = [r["segment_id"] for r in self.index["segments"] if r["segment_id"] not in self.reads]

    def _sources(self):
        return {r["source_location"] for r in [*self.metadata, *self.reads.values()]}

    def _source_check(self, locations):
        if not locations or not set(locations).issubset(self._sources()):
            raise ExtractorHarnessError(C.MODEL_OUTPUT_REFERENCE, "source must be a verified, actually read location")

    def _material_context(self):
        return {"paper_title": self.paper.title, "metadata": self.metadata,
                "reads": list(self.reads.values()), "unread_segments": self.trace["reading"]["unread_segments"],
                "candidate_version": self.version,
                "points": [p.model_dump(mode="json") for p in self.points.values()]}

    def _drafts(self, values, *, minimum=1, maximum=8):
        if not isinstance(values, list) or not minimum <= len(values) <= maximum:
            raise ValueError(f"points must contain {minimum}..{maximum} entries")
        drafts = [DraftPoint.model_validate(v) for v in values]
        for p in drafts:
            if not any(s.strip() for s in p.technical_features):
                raise ValueError("technical_features cannot be blank")
            self._source_check(p.source_locations)
        return drafts

    def _new_point(self, draft):
        self.next_id += 1
        return NoveltyPoint(point_id=f"NP-{self.next_id}", **draft.model_dump())

    def _save_version(self):
        self.version += 1
        self.trace["versions"].append({"version": self.version,
            "points": [p.model_dump(mode="json") for p in self.points.values()]})

    def _review(self):
        menu = self._read_options()
        oid = f"{self.identity}:review:v{self.version}"
        pairs = {f"compare-{a}-{b}": [a, b] for a, b in combinations(self.points, 2)}
        context = {**self._material_context(), "phase": "semantic_review", "operation_id": oid,
                   "reader_options": list(menu.values()), "output_schema": ReviewDraft.model_json_schema(),
                   "comparison_options": [{"option_id": k, "point_ids": v} for k, v in pairs.items()],
                   "comparison_requirement": (f"Return exactly these comparison option_ids: {list(pairs)}"
                       if pairs else "There are no candidate pairs. Return comparisons: [] exactly; do not create a placeholder comparison."),
                   "generation_feedback": self.generation_feedback}

        def validate(data):
            # Report the actionable menu violation before nested field errors:
            # an invented placeholder needs removal, not a fabricated citation.
            if isinstance(data, dict) and not data.get("read_requests") and not pairs and data.get("comparisons"):
                raise ValueError("No candidate pairs were issued. Set comparisons to [] exactly; remove every placeholder comparison.")
            review = ReviewDraft.model_validate(data)
            if review.read_requests:
                if review.point_reviews or review.missing_contributions or review.comparisons:
                    raise ValueError("request reads before judging, not in the same response")
                self._selected_segments([r.option_id for r in review.read_requests], menu)
                return review
            if (len(review.point_reviews) != len(self.points)
                    or {r.point_id for r in review.point_reviews} != set(self.points)):
                raise ValueError("review every current point exactly once")
            for row in review.point_reviews:
                if (row.status == "pass") != (not row.issues):
                    raise ValueError("pass requires no issues; revise requires reasons")
                for issue in row.issues:
                    self._source_check(issue.source_locations)
                    if (not set(issue.related_point_ids).issubset(self.points)
                            or row.point_id in issue.related_point_ids):
                        raise ValueError("invalid related point IDs")
                    if issue.code == "duplicate" and len(issue.related_point_ids) != 1:
                        raise ValueError("duplicate needs exactly one retained representative")
            for missing in review.missing_contributions:
                self._source_check(missing.source_locations)
            representatives = {row.point_id: next((i.related_point_ids[0] for i in row.issues
                if i.code == "duplicate"), None) for row in review.point_reviews}
            for point_id in representatives:
                visited, current = set(), point_id
                while current is not None:
                    if current in visited:
                        raise ValueError("duplicate representatives must not form a cycle; retain a nonduplicate representative")
                    visited.add(current)
                    current = representatives[current]
            if (len(review.comparisons) != len(pairs)
                    or {c.option_id for c in review.comparisons} != set(pairs)):
                raise ValueError("explicitly compare every issued candidate pair exactly once")
            rows = {r.point_id: r for r in review.point_reviews}
            for comparison in review.comparisons:
                self._source_check(comparison.source_locations)
                a, b = pairs[comparison.option_id]
                duplicate_issues = [i for pid in (a, b) for i in rows[pid].issues
                    if i.code == "duplicate" and i.related_point_ids == [b if pid == a else a]]
                if comparison.relation == "duplicate" and not duplicate_issues:
                    raise ValueError("duplicate pair must produce a corresponding revise/duplicate issue")
                if comparison.relation != "duplicate" and duplicate_issues:
                    raise ValueError("pair relation conflicts with duplicate issue")
                if comparison.relation == "uncertain" and all(rows[p].status == "pass" for p in (a, b)):
                    raise ValueError("uncertain pair cannot mark both candidates passed")
            return review

        result = self._request("review", context, validate)
        self.trace["reviews"].append({"version": self.version, **result.model_dump(mode="json")})
        if result.read_requests:
            self._execute_reads([r.option_id for r in result.read_requests], menu, origin="semantic_review")
            return None
        self.approved = {r.point_id: self.points[r.point_id] for r in result.point_reviews if r.status == "pass"}
        return result

    def _repair_options(self, review):
        menu = {}

        def add(group, action, targets, issues, minimum, maximum):
            oid = f"{self.identity}:v{self.version}:{group}:{action}"
            menu[oid] = dict(option_id=oid, group=group, action=action, target_ids=targets,
                issues=issues, min_points=minimum, max_points=maximum)

        for row in review.point_reviews:
            if row.status == "pass":
                continue
            issues = [i.model_dump(mode="json") for i in row.issues]
            duplicates = [i for i in row.issues if i.code == "duplicate"]
            # Rewording a known duplicate cannot establish a separate mechanism.
            # Permit only consolidation/removal, never an invitation to invent one.
            if not duplicates:
                add(row.point_id, "revise", [row.point_id], issues, 1, 1)
            add(row.point_id, "withdraw", [row.point_id], issues, 0, 0)
            if not duplicates and any(i.code == "mixed" for i in row.issues):
                add(row.point_id, "split", [row.point_id], issues, 2, 3)
            if duplicates:
                add(row.point_id, "merge", [row.point_id, duplicates[0].related_point_ids[0]], issues, 1, 1)
        for i, missing in enumerate(review.missing_contributions):
            add(f"missing-{i}", "add", [], [missing.model_dump(mode="json")], 1, 2)
            add(f"missing-{i}", "request_recheck", [], [missing.model_dump(mode="json")], 0, 0)
        return menu

    def _repair(self, review):
        menu = self._repair_options(review)
        correction = self.prompts.render(self.config.correction_prompt,
            context_json=json.dumps({"review": review.model_dump(mode="json"), "options": list(menu.values())}, ensure_ascii=False))
        context = {**self._material_context(), "phase": "repair", "options": list(menu.values()),
                   "correction_procedure": correction.user, "point_schema": DraftPoint.model_json_schema()}

        def validate(data):
            if not isinstance(data, dict) or set(data) != {"repairs"} or not isinstance(data["repairs"], list):
                raise ValueError("repair requires only a repairs array")
            selected, groups, targets, feedback = [], set(), set(), []
            for item in data["repairs"]:
                if (not isinstance(item, dict) or not {"option_id", "points"}.issubset(item)
                        or set(item) - {"option_id", "points", "reason"}):
                    raise ValueError("each repair contains option_id, points and optional recheck reason")
                option = menu.get(item["option_id"]) if isinstance(item["option_id"], str) else None
                if option is None or option["group"] in groups:
                    raise ValueError("choose one issued option per problem; no invented or stale options")
                if targets.intersection(option["target_ids"]):
                    raise ValueError("overlapping patches are not allowed")
                drafts = self._drafts(item["points"], minimum=option["min_points"], maximum=option["max_points"])
                if option["action"] == "request_recheck":
                    if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                        raise ValueError("request_recheck requires a concrete reason; cannot silently skip the issue")
                    feedback.append({"issue": option["issues"], "reason": item["reason"]})
                if option["action"] == "add":
                    basis = set(option["issues"][0]["source_locations"])
                    if any(not basis.intersection(p.source_locations) for p in drafts):
                        raise ValueError("additions must cite the review-authorized missing contribution")
                selected.append((option, drafts))
                groups.add(option["group"])
                targets.update(option["target_ids"])
            required = {v["group"] for v in menu.values()}
            if groups != required:
                raise ValueError("address every review problem exactly once")
            candidate = {k: p for k, p in self.points.items() if k not in targets}
            next_id = self.next_id
            for option, drafts in selected:
                for i, draft in enumerate(drafts):
                    # Keep the target identity for a revision or merge representative.
                    if i == 0 and option["target_ids"]:
                        pid = option["target_ids"][-1] if option["action"] == "merge" else option["target_ids"][0]
                    else:
                        next_id += 1
                        pid = f"NP-{next_id}"
                    candidate[pid] = NoveltyPoint(point_id=pid, **draft.model_dump())
            if len(candidate) > 8:
                raise ValueError("candidate limit exceeded")
            if candidate == self.points and (not feedback or feedback == self.generation_feedback):
                raise ValueError("correction made no change")
            return candidate, next_id, data, feedback

        candidate, next_id, patch, feedback = self._request("generate", context, validate)
        self.trace["corrections"].append({"kind": "semantic", "before_version": self.version,
            "options": list(menu.values()), "patch": patch, "procedure_version": correction.version})
        self.approved = {k: p for k, p in self.approved.items() if candidate.get(k) == p}
        changed = candidate != self.points
        self.points, self.next_id = candidate, next_id
        self.generation_feedback = feedback
        if changed:
            self._save_version()

    def run(self, digest: PaperDigest):
        self.trace["paper_id"] = digest.paper_id
        self.trace["reading"] = {"reference": digest.paper_source.model_dump(mode="json") if digest.paper_source else None,
            "reads": [], "unread_segments": [], "scope_decision": None, "generation_allowed": False}
        self.reads, self.read_attempts = {}, 0
        completed = False
        try:
            if digest.paper_source is None:
                raise ExtractorHarnessError(C.READER_MANIFEST_INVALID, "controlled extraction requires persisted PaperInput")
            self.reader = PaperReader(digest.paper_source)
            self.paper, self.index = self.reader.load()
            from ..agents.point_extractor import build_paper_digest
            if build_paper_digest(self.paper).model_dump(exclude={"paper_source"}) != digest.model_dump(exclude={"paper_source"}):
                raise ExtractorHarnessError(C.RECOVERY_SCOPE_CHANGED, "digest differs from persisted PaperInput")
            self.identity = "x" + _digest(digest.paper_source.model_dump(mode="json"))[:8]
            prefix = f"paper:{digest.paper_source.run_id}:{digest.paper_source.sha256}:"
            self.metadata = [{"source_location": prefix + name, "text": getattr(self.paper, name)}
                for name in ("abstract", "english_abstract", "claimed_contributions") if getattr(self.paper, name)]
            self.trace["reading"]["unread_segments"] = [r["segment_id"] for r in self.index["segments"]]
            menu = self._read_options()
            if not menu:
                raise ExtractorHarnessError(C.BUDGET_TURNS, "no readable segment within configured budget")

            def reading_selection(data):
                if (not isinstance(data, dict) or set(data) != {"option_ids", "reason"}
                        or not isinstance(data["reason"], str) or not data["reason"].strip()):
                    raise ValueError("Reader selection requires option_ids and a nonempty reason")
                self._selected_segments(data["option_ids"], menu)
                return data

            choice = self._request("read", {"phase": "reading_scope", "question": "是否需要阅读全文，或补读哪些 PaperInput 分段？",
                "paper_title": self.paper.title, "metadata": self.metadata, "options": list(menu.values()),
                "read_budget": {"segments": self.config.max_read_segments, "chars": self.config.max_read_chars}}, reading_selection)
            self.trace["reading"]["scope_decision"] = choice
            self._execute_reads(choice["option_ids"], menu, origin="initial")
            self.trace["reading"]["generation_allowed"] = True
            oid = f"{self.identity}:generate:v0"

            def generation(data):
                if not isinstance(data, dict) or set(data) != {"option_id", "points"} or data["option_id"] != oid:
                    raise ValueError("initial generation requires issued option_id and points")
                return self._drafts(data["points"], minimum=0)

            drafts = self._request("generate", {**self._material_context(), "phase": "generate", "option_id": oid,
                "point_schema": DraftPoint.model_json_schema()}, generation)
            self.points = {p.point_id: p for p in (self._new_point(d) for d in drafts)}
            self._save_version()
            while self.used["review"] < self.limits["review"]:
                review = self._review()
                if review is None:
                    continue
                if all(r.status == "pass" for r in review.point_reviews) and not review.missing_contributions:
                    completed = True
                    break
                if (self.used["generate"] >= self.limits["generate"]
                        or self.used["review"] >= self.limits["review"]):
                    break
                self._repair(review)
            if not completed:
                self._event(C.BUDGET_TURNS, "harness", "review/repair budget ended with unresolved candidates", action="stop")
            return self._finish(completed)
        except Exception as exc:
            code = getattr(exc, "code", C.UNKNOWN)
            self._event(code, "harness", str(exc), action="stop")
            self.trace["execution_status"] = "failed"
            self.trace["approved_points"] = [p.model_dump(mode="json") for p in self.approved.values()]
            self.trace["unresolved_points"] = [p.model_dump(mode="json") for k, p in self.points.items() if k not in self.approved]
            raise
        finally:
            self.trace["budgets"] = {"used": dict(self.used), "limits": self.limits,
                "reader_attempts": self.read_attempts, "reader_limit": self.config.max_read_segments,
                "read_chars": sum(len(r["text"]) for r in self.reads.values()), "char_limit": self.config.max_read_chars}
            self.trace["total_model_calls_used"] = sum(self.used.values())

    def _finish(self, completed):
        result = list(self.approved.values())
        self.trace.update(execution_status="completed" if completed else "partial",
            final_points=[p.model_dump(mode="json") for p in result], final_count=len(result),
            approved_points=[p.model_dump(mode="json") for p in result],
            unresolved_points=[p.model_dump(mode="json") for k, p in self.points.items() if k not in self.approved],
            scope_status="pending_coverage", exit_reason="review_passed" if completed else "budget_exhausted",
            semantic_review_passed=completed)
        if not result:
            raise ExtractorHarnessError(C.MODEL_OUTPUT_SCHEMA, "no semantically approved contribution; inspect trace")
        return result
