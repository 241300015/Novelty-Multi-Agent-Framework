"""Persist one Planner attempt independently of runtime debug tracing."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4


class ConceptAudit:
    def __init__(self, root, point, task, run_id):
        attempt = uuid4().hex
        self.directory = Path(root) / "planner-attempts" / attempt / "planner"
        self.directory.mkdir(parents=True)
        self.binding = dict(run_id=run_id or attempt, attempt=attempt, point_id=point.point_id,
                            task_id=task.task_id, language=task.language)
        self.calls = 0
        self.result = {}
        self.files = {}
        self.write("input.json", dict(**self.binding, point=point.model_dump(), task=task.model_dump()))
        self.index("running")

    def write(self, name, value):
        path = self.directory / name
        path.parent.mkdir(exist_ok=True)
        data = json.dumps(value, ensure_ascii=False, default=str).encode()
        path.write_bytes(data)
        self.files[name] = hashlib.sha256(data).hexdigest()

    def index(self, status):
        path = self.directory / "index.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(**self.binding, status=status, files=self.files), ensure_ascii=False))
        temporary.replace(path)

    def event(self, attempt, step, payload):
        if step == "concept_stage_finished":
            self.result = payload
        if step == "concept_call":
            self.calls += 1
            self.write(f"calls/call-{self.calls:05}.json", payload)
        event = dict(attempt=attempt, step=step, payload=payload)
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def finish(self, plan=None, error=None):
        status = "failed" if error is not None else ("partial" if self.result.get("unresolved") else "completed")
        if plan is not None:
            self.write("plan.json", plan.model_dump(mode="json"))
        events = self.directory / "events.jsonl"
        if events.exists():
            self.files[events.name] = hashlib.sha256(events.read_bytes()).hexdigest()
        self.write("summary.json", dict(**self.binding, status=status, concept_calls=self.calls, concept_result=self.result, error=error))
        self.index(status)


def read_attempt(directory, *, run_id, point_id, task_id):
    """Load terminal records after checking their binding and bytes."""
    root = Path(directory).resolve()
    index = json.loads((root / "index.json").read_bytes())
    for key, expected in (("run_id", run_id), ("point_id", point_id), ("task_id", task_id)):
        if index[key] != expected:
            raise ValueError(f"Planner 审计绑定不一致: {key}")
    if index["status"] == "running":
        raise ValueError("规划尚未结束")
    records = {}
    for name, digest in index["files"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Planner 审计路径越界")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"Planner 审计摘要不一致: {name}")
        records[name] = data
    return records
