import json

import pytest

from test_renderer import seed_workspace, refresh_fixture_versions
from novelty_agent_framework.schemas import PaperInput, ResearchTask, SearchPlan
from novelty_agent_framework.core.concept_audit import ConceptAudit
from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.persistence import persist_retrieval_plans
from novelty_agent_framework.tools.renderer import render_report


def publish_and_persist(tmp_path, *, tamper=False, mismatch=False, no_plan=False, cancelled=False):
    seed_workspace(tmp_path)
    workspace=tmp_path/'renderer-paper'
    paper=PaperInput.model_validate_json((workspace/'paper-input/others/paper.json').read_text())
    data=json.loads((workspace/'retrieval-plans.json').read_text())
    item=data['novelty_point_plans'][0]
    task=ResearchTask.model_validate(item['research_tasks'][0])
    plan=SearchPlan.model_validate(item['search_plans'][0])
    from novelty_agent_framework.schemas import NoveltyPoint
    point=NoveltyPoint.model_validate(json.loads((workspace/'novelty-points.json').read_text())['novelty_points'][0])
    manager=RuntimeArtifactManager(paper.paper_id,config=RuntimeDebugConfig(output_root=tmp_path,archive_root=tmp_path/'archive'))
    with manager:
        audit=ConceptAudit(tmp_path,point,task,manager.run_id,publish_root=manager.run_dir/'planner')
        audit.event(0,'concept_review_progress',dict(accepted=[1],unresolved={'2':'语义未解决'}))
        audit.event(0,'strategy_review_progress',dict(accepted_strategies=[s.model_dump() for s in plan.strategies],
            failed_levels={'medium':'internal diagnostic not exported','broad':'internal diagnostic not exported'},review_performed=True))
        audit.finish(error='internal diagnostic not exported', cancelled=cancelled) if no_plan else audit.finish(plan=plan)
        if tamper:
            published=next((manager.run_dir/'planner/attempts').iterdir())
            (published/'summary.json').write_text('{}')
        if mismatch:
            plan=plan.model_copy(update={'strategies':[s.model_copy(update={'use_exclude':not s.use_exclude}) for s in plan.strategies]})
        persist_retrieval_plans(paper,[task],search_plans=[] if no_plan else [plan],rounds=1,output_root=tmp_path)
    refresh_fixture_versions(workspace,'retrieval-plans.json','evidence-cards.json','report.json')
    return workspace


@pytest.mark.parametrize('template',['default','concise'])
def test_partial_planning_is_visible_in_final_report(tmp_path,template):
    workspace=publish_and_persist(tmp_path)
    payload=json.loads((workspace/'retrieval-plans.json').read_text())
    row=payload['novelty_point_plans'][0]['planner_statuses'][0]
    assert row['status']=='partial'
    assert row['failed_levels']==['broad','medium']
    assert 'internal diagnostic' not in json.dumps(row)
    report=render_report(paper_name='renderer-paper',output_root=tmp_path,template_name=template).read_text()
    assert '规划部分完成' in report
    assert '未解决概念1项' in report
    assert '未通过档位' in report and '宽泛' in report and '适中' in report


def test_tampered_planner_audit_cannot_become_report_fact(tmp_path):
    with pytest.raises(ValueError,match='摘要不一致'):
        publish_and_persist(tmp_path,tamper=True)


def test_audit_for_a_different_plan_does_not_certify_current_report(tmp_path):
    workspace=publish_and_persist(tmp_path,mismatch=True)
    payload=json.loads((workspace/'retrieval-plans.json').read_text())
    row=payload['novelty_point_plans'][0]['planner_statuses'][0]
    assert row['status']=='unverified'


@pytest.mark.parametrize('status,label',[('failed','规划失败'),('cancelled','规划已取消')])
def test_failure_and_cancellation_are_not_presented_as_successful_partial_results(status,label):
    from novelty_agent_framework.core.planner_report_status import planner_status_notes
    notes=planner_status_notes([{'planner_statuses':[dict(novelty_point_id='NP-1',task_id='T-1',status=status)]}])
    assert label in notes[0] and '规划部分完成' not in notes[0]


@pytest.mark.parametrize('template', ['default', 'concise'])
@pytest.mark.parametrize('cancelled,label', [(False, '规划失败'), (True, '规划已取消')])
def test_task_without_a_plan_is_still_visible_in_final_report(tmp_path, template, cancelled, label):
    workspace = publish_and_persist(tmp_path, no_plan=True, cancelled=cancelled)
    payload = json.loads((workspace/'retrieval-plans.json').read_text())
    item = payload['novelty_point_plans'][0]
    assert item['search_plans'] == []
    assert item['planner_statuses'][0]['status'] == ('cancelled' if cancelled else 'failed')
    report = render_report(paper_name='renderer-paper', output_root=tmp_path, template_name=template).read_text()
    assert label in report
    assert 'internal diagnostic' not in report
