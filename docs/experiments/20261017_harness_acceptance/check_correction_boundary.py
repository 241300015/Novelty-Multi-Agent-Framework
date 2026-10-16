"""Replay the recorded semantic-misattribution draft through the current workflow.

No model calls. Valid quotes are retained; this checks whether the existing repair
trigger notices the already manually identified contribution attribution error.
"""
import asyncio
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from scripts.compare_researcher_harness import read, write, PAPER
from novelty_agent_framework.agents import DefaultEvidenceValidator
from novelty_agent_framework.core.tool_call_harness import ToolCallHarnessEvent
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.schemas import ResearcherToolObservation, TaskResearchRequest
from novelty_agent_framework.tools import EvidenceCardBuilder, ResearcherToolRegistry
from novelty_agent_framework.workflows import TaskResearcherWorkflow


async def check(root):
    historical = ROOT/'outputs/harness-np1-schema-20261017/runs/choice_only/NP-1'
    draft = read(historical/'builder-calls.json')[0]['draft']
    recorded = read(historical/'result.json')
    request = TaskResearchRequest.model_validate(next(r for r in read(root/'fixture/requests.json') if r['novelty_point']['point_id']=='NP-1'))
    case = root/'correction_boundary'
    case.mkdir(exist_ok=False)
    shutil.copytree(root/'fixture/corpus'/PAPER,case/PAPER)
    trace = tuple(ToolCallHarnessEvent(kind='tool_result', observation=ResearcherToolObservation(
        tool_name='reader', succeeded=True, payload={'read_result':r})) for r in recorded['read_results'])

    class NoModel:
        calls = 0
        async def acomplete(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError('Unexpected model correction invocation')

    class RecordedHarness:
        config = None
        registry = ResearcherToolRegistry([])
        async def run(self, **kwargs):
            import json
            return SimpleNamespace(trace=trace, final_content=json.dumps(draft), turns_used=0, stop_reason=None)

    client = NoModel()
    store = ReferenceStore(case)
    worker = TaskResearcherWorkflow(client, ResearcherToolRegistry([]), EvidenceCardBuilder(store))
    worker.harness = RecordedHarness()
    result = await worker.ainvoke(request)
    validation = DefaultEvidenceValidator().validate(result.evidence_cards, tasks=[request.research_task],
        evidence=result.evidence, novelty_points=[request.novelty_point], paper_id=PAPER, reference_store=store)
    assert len(result.evidence_cards)==1 and len(validation.accepted)==1
    assert 'GSAERU' in result.evidence_cards[0].main_contribution
    assert not result.card_build_rejections and client.calls==0
    write(case/'result.json',result.model_dump(mode='json'))
    write(root/'correction-boundary.json',{
        'mode':'historical draft + recorded Reader observations; current workflow and disk Validator; no model call',
        'source':str(historical.relative_to(ROOT)),
        'manually_identified_issue':'Target GSAERU contribution attributed to the reference survey; valid quotations do not establish this attribution.',
        'card_title':result.evidence_cards[0].document_title,
        'main_contribution':result.evidence_cards[0].main_contribution,
        'builder_rejections':len(result.card_build_rejections),
        'validator_accepted':len(validation.accepted),
        'correction_model_calls':client.calls,
        'finding':'Semantic-error detection does not enter the current quote-repair trigger. This is a correction coverage gap, not a failed provenance validation.',
    })
    print('Historical semantic error still accepted by provenance checks; correction calls = 0')


if __name__=='__main__':
    asyncio.run(check(Path(sys.argv[1]).resolve()))
