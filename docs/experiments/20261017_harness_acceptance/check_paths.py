"""Deterministic branch coverage using real frozen files, without any model call.

This is separate from the six live-model cases and is never counted as model quality.
"""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.compare_researcher_harness import (
    PAPER, FrozenDatabaseSearch, FrozenRetrieval, build_workflow, canonical,
    load_application_config, read, verify_fixture, write,
)
from backend.env import ModelResponse
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.schemas import TaskResearchRequest
from novelty_agent_framework.tools import EvidenceCardBuilder, ResearcherToolRegistry
from novelty_agent_framework.workflows import TaskResearcherWorkflow


class ScriptedDecisions:
    def __init__(self, first_title):
        self.first_title = first_title
        self.acquired = False
        self.audit = []

    async def acomplete(self, messages, *, options):
        prefix = 'Current Harness choices (only these option_id values are valid):\n'
        assert messages[-1].content.startswith(prefix), 'Unexpected repair/finalization path'
        menu = json.loads(messages[-1].content[len(prefix):].split('\nReturn', 1)[0])
        assert not options.tools and options.tool_choice == 'none'
        if not menu:
            assert options.response_format is None
            return ModelResponse(content='{"cards":[],"no_evidence_reason":"Deterministic path check; no model authored a card."}')
        assert options.response_format['json_schema']['schema']['properties']['option_id']['enum'] == list(menu)
        search = [key for key, label in menu.items() if label.startswith('执行第')]
        acquire = [key for key, label in menu.items() if label.startswith('获取此文献全文')]
        skip = [key for key, label in menu.items() if label.startswith('不获取')]
        target = [key for key, label in menu.items() if self.first_title in label]
        if search:
            selected = search[0]
        elif acquire and not self.acquired:
            selected = acquire[0]
            self.acquired = True
        elif skip:
            selected = skip[0]
        else:
            selected = (target or list(menu))[0]
        self.audit.append({'menu': menu, 'selected': selected})
        return ModelResponse(content=json.dumps({'option_id': selected}))


async def check(root):
    verify_fixture(root)
    summaries = {}
    for raw in read(root/'fixture/requests.json'):
        request = TaskResearchRequest.model_validate(raw)
        point = request.novelty_point.point_id
        case = root/'mechanical_paths'/point
        case.mkdir(parents=True, exist_ok=False)
        shutil.copytree(root/'fixture/corpus'/PAPER, case/PAPER)
        config = load_application_config(profile_path=ROOT/'docs/experiments/20261016_guided_reading/profile.json', environ={})
        config.researcher.harness.guided_reading = True
        config.researcher.harness.text_choices = True
        config.researcher.prompt = 'research/choice_only'
        standard = build_workflow(config, output_root=case).services.task_researcher
        store = ReferenceStore(case)
        manifest = store.load_manifest(PAPER)
        discoveries = read(root/'fixture/discoveries.json')
        ids = {row['source_record_id'] for row in discoveries[point]['source_records']}
        bodies = [a for a in manifest.artifacts if a.source_record_id in ids and a.role.value == 'extracted_text']
        body = min(bodies, key=lambda a: len(store.verify_artifact_file(PAPER, a.artifact_id)[2]))
        title = next(r.title for r in manifest.source_records if r.source_record_id == body.source_record_id)
        client = ScriptedDecisions(title)
        retrieval = FrozenRetrieval(store, discoveries, case/'retrieval-calls.json')
        database = FrozenDatabaseSearch({'arxiv':retrieval}, reference_store=store)
        registry = ResearcherToolRegistry([database if name == 'database_search' else standard.tools.get(name)
                                          for name in standard.tools.names])
        worker = TaskResearcherWorkflow(client, registry, EvidenceCardBuilder(store), config=standard.config)
        result = await worker.harness.run(system_prompt='Deterministic harness path verification.',
            initial_user_message='Exercise all three search rounds and one complete body.',
            scope=request, options=standard.config.model_options)
        discovery = [row for row in retrieval.calls if row['kind'] == 'discovery']
        levels = [[s['level'] for s in row['search_plan']['strategies']] for row in discovery]
        assert levels == [['strict','medium','broad'], ['medium','broad'], ['broad']], levels
        assert result.stop_reason is None, result.stop_reason
        reads = []
        last_discovery = -1
        for index, event in enumerate(result.trace):
            if event.kind != 'tool_call':
                continue
            call = event.tool_call
            if call.name == 'database_search' and not call.arguments.get('full_text_source_record_ids'):
                last_discovery = index
        for index, event in enumerate(result.trace):
            if event.observation is None or event.observation.tool_name != 'reader':
                continue
            assert event.observation.succeeded
            payload = event.observation.payload
            for row in payload.get('read_results') or [payload['read_result']]:
                artifact = next(a for a in manifest.artifacts if a.artifact_id == row['artifact_id'])
                if artifact.role.value == 'extracted_text':
                    assert index > last_discovery
                reads.append({k:row[k] for k in ('artifact_id','char_start','char_end','has_more')} | {'role':artifact.role.value})
        addresses = [(r['artifact_id'], r['char_start'], r['char_end']) for r in reads]
        assert len(addresses) == len(set(addresses))
        selected_reads = [r for r in reads if r['artifact_id'] == body.artifact_id]
        assert selected_reads and not selected_reads[-1]['has_more']
        assert selected_reads[-1]['char_end'] == len(store.verify_artifact_file(PAPER,body.artifact_id)[2].decode())
        assert not any(event.kind == 'error' for event in result.trace)
        summary = {'mode':'scripted choices, real disk Reader; zero model calls', 'task_sha256':canonical(raw),
            'passed':True,'discovery_levels':levels,'empty_strategy_calls':0,
            'abstract_reads':sum(r['role']=='abstract' for r in reads),'body_reads':len(selected_reads),
            'body_read_to_eof':True,'body_after_all_discovery':True,'duplicate_ranges':0,
            'harness_turns':result.turns_used,'tool_calls':result.tool_calls_used,
            'budgets':{'max_turns':standard.harness.config.max_turns,'max_tool_calls':standard.harness.config.max_tool_calls,
                       'per_tool_limits':standard.harness.config.per_tool_limits}}
        write(case/'summary.json',summary)
        write(case/'reads.json',reads)
        write(case/'decisions.json',client.audit)
        write(case/'trace.json',[asdict(event) for event in result.trace])
        summaries[point] = summary
    write(root/'mechanical-paths.json',summaries)
    print(json.dumps(summaries,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    asyncio.run(check(Path(sys.argv[1]).resolve()))
