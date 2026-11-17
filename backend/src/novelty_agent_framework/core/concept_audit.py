"""Persist one Planner attempt independently of runtime debug tracing."""
import hashlib
import json
import shutil
import threading
from collections import Counter
from copy import deepcopy
from pathlib import Path
from uuid import uuid4


_publish_lock = threading.Lock()


def implementation_digest():
    package = Path(__file__).parents[1]
    paths = [*package.glob('core/concept_*.py'), package/'core/strategy_stage.py', package/'core/task_semantics.py',
             package/'agents/search_planner.py', *package.glob('prompts/search_planner/*.md')]
    checksum = hashlib.sha256()
    for path in sorted(paths):
        if path.exists():
            checksum.update(str(path.relative_to(package)).encode())
            checksum.update(path.read_bytes())
    return checksum.hexdigest()


class ConceptAudit:
    def __init__(self, root, point, task, run_id, *, protocol='v1', publish_root=None):
        attempt = uuid4().hex
        self.directory = Path(root) / "planner-attempts" / attempt / "planner"
        self.directory.mkdir(parents=True)
        self.binding = dict(run_id=run_id or attempt, attempt=attempt, point_id=point.point_id,
                            task_id=task.task_id, language=task.language)
        self.calls = 0
        self.strategy_calls = 0
        self.semantic_calls = 0
        self.semantic_exclusions = {}
        self.replayed = 0
        self.metrics = Counter()
        self.issues = {}
        self.issue_history = []
        self.evidence = []
        self.strategy_result = {}
        self.publish_root = Path(publish_root) if publish_root else None
        self.metadata = dict(schema_version=2, protocol=protocol, implementation_sha256=implementation_digest())
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
        temporary.write_text(json.dumps(dict(**self.binding, **self.metadata, status=status, files=self.files), ensure_ascii=False))
        temporary.replace(path)

    def event(self, attempt, step, payload):
        if step=='task_semantic_contract':
            self.write('semantic-contract.json',payload)
            self.metadata['semantic_contract_id']=payload['contract_id']
        if step == 'semantic_action_excluded':
            self.semantic_exclusions.update(payload['facts'])
        if step=='concept_stage_finished' and payload.get('concept_fact_links') is not None:
            self.write('concept-fact-links.json',payload['concept_fact_links'])
        if step == 'concept_source_catalog':
            self.write('sources.json', payload)
        if step in ("concept_stage_finished", "concept_review_progress"):
            self.result = payload
        if step == 'concept_review_progress' and 'issues' in payload:
            self.issues = deepcopy(payload['issues'])
        if step == 'concept_issue_resolved':
            self.issue_history.append(deepcopy(payload))
            self.metrics['semantic_' + payload['resolution']] += 1
            self.metrics['content_changes'] += bool(payload['changed'])
        if step == 'concept_evidence_resolved':
            self.evidence.extend(deepcopy(payload['decisions']))
        if step in ('strategy_review_progress', 'strategy_stage_finished'):
            self.strategy_result = deepcopy(payload)
        if step in ('concept_contract_retry', 'strategy_contract_retry', 'concept_budget_exhausted',
                    'strategy_budget_exhausted', 'semantic_budget_exhausted', 'semantic_contract_retry',
                    'concept_alias_quarantined', 'strategy_semantic_rejected', 'strategy_fact_rejected', 'semantic_contract_rejected'):
            self.metrics[step] += 1
        if step == "concept_call":
            self.calls += 1
            self.replayed += bool(payload.get('replayed'))
            self.write(f"calls/call-{self.calls:05}.json", payload)
        if step == 'strategy_call':
            self.strategy_calls += 1
            self.write(f'strategy-calls/call-{self.strategy_calls:05}.json', payload)
        if step=='semantic_call':
            self.semantic_calls+=1
            self.write(f'semantic-calls/call-{self.semantic_calls:05}.json',payload)
        event = dict(attempt=attempt, step=step, payload=payload)
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def finish(self, plan=None, error=None, *, cancelled=False):
        if plan is not None and not self.strategy_result:
            self.strategy_result = dict(accepted_strategies=[s.model_dump() for s in plan.strategies],
                failed_levels={}, review_performed=False)
        status = ('cancelled' if cancelled else 'failed') if error is not None else (
            'partial' if self.result.get('unresolved') or self.strategy_result.get('failed_levels')
            or self.semantic_exclusions else 'completed')
        if plan is not None:
            self.write("plan.json", plan.model_dump(mode="json"))
        events = self.directory / "events.jsonl"
        if events.exists():
            self.files[events.name] = hashlib.sha256(events.read_bytes()).hexdigest()
        self.write('issues.json', dict(current=self.issues, history=self.issue_history))
        self.write('resolved-evidence.json', self.evidence)
        summary = dict(**self.binding, **self.metadata, status=status, concept_calls=self.calls,
            concept_request_records=self.calls-self.replayed, replayed_generation_records=self.replayed,
            strategy_calls=self.strategy_calls, semantic_calls=self.semantic_calls, metrics=dict(self.metrics), concept_result=self.result,
            strategy_result=self.strategy_result, semantic_exclusions=self.semantic_exclusions, error=error)
        self.write('summary.json', summary)
        report = (f'# Planner attempt\n\nStatus: {status}\n\n'
            f'Point: {self.binding["point_id"]}; task: {self.binding["task_id"]}\n\n'
            f'Accepted concepts: {len(self.result.get("accepted", []))}; unresolved: {len(self.result.get("unresolved", {}))}\n\n'
            f'Accepted strategies: {len(self.strategy_result.get("accepted_strategies", []))}; '
            f'failed levels: {json.dumps(self.strategy_result.get("failed_levels", {}), ensure_ascii=False)}\n\n'
            f'Concept request records: {self.calls-self.replayed}; replay records: {self.replayed}; '
            f'strategy request records: {self.strategy_calls}; semantic contract request records: {self.semantic_calls}\n\n'
            f'Metrics: {json.dumps(dict(self.metrics), ensure_ascii=False)}\n\n'
            f'Strategy review performed: {self.strategy_result.get("review_performed", bool(self.strategy_calls))}\n\n'
            f'Error: {error or "none"}\n\nSemantic quality is evaluated against agreed benchmarks and exception sampling.\n')
        (self.directory/'report.md').write_text(report, encoding='utf-8')
        self.files['report.md'] = hashlib.sha256(report.encode()).hexdigest()
        self.index(status)
        if self.publish_root:
            with _publish_lock:
                target = self.publish_root/'attempts'/self.binding['attempt']
                shutil.copytree(self.directory, target)
                registry_path = self.publish_root/'index.json'
                registry = json.loads(registry_path.read_text()) if registry_path.exists() else dict(schema_version=1, attempts=[])
                registry['attempts'].append(dict(**self.binding, status=status,
                    directory=f'attempts/{self.binding["attempt"]}'))
                temporary = registry_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(registry, ensure_ascii=False))
                temporary.replace(registry_path)


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


def read_published_attempts(root, *, run_id, point_id, task_id):
    """Report consumers use the run-level index, then verify each bound attempt."""
    root = Path(root).resolve()
    registry = json.loads((root/'index.json').read_text())
    records = []
    for item in registry['attempts']:
        if (item['run_id'], item['point_id'], item['task_id']) != (run_id, point_id, task_id):
            continue
        path = (root/item['directory']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Planner published path escapes run directory')
        records.append(read_attempt(path, run_id=run_id, point_id=point_id, task_id=task_id))
    return records
