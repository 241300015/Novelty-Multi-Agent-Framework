import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_search_planner import make_point, make_task
from novelty_agent_framework.core.concept_audit import ConceptAudit, read_published_attempts


def test_concurrent_publication_is_bound_and_verified(tmp_path):
    point = make_point()
    publication = tmp_path/'run/planner'
    def finish(task_id):
        task=make_task().model_copy(update={'task_id':task_id})
        audit=ConceptAudit(tmp_path,point,task,'run-1',protocol='v2',publish_root=publication)
        audit.event(0,'concept_review_progress',dict(accepted=[1],unresolved={},issues={}))
        audit.finish()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(finish,['T-1','T-2']))
    assert len(json.loads((publication/'index.json').read_text())['attempts'])==2
    records=read_published_attempts(publication,run_id='run-1',point_id=point.point_id,task_id='T-1')
    assert len(records)==1
    assert {'report.md','issues.json','resolved-evidence.json'} <= records[0].keys()
    folder=next(p.parent for p in publication.glob('attempts/*/input.json') if json.loads(p.read_text())['task_id']=='T-1')
    (folder/'report.md').write_text('篡改')
    with pytest.raises(ValueError,match='摘要不一致'):
        read_published_attempts(publication,run_id='run-1',point_id=point.point_id,task_id='T-1')


@pytest.mark.parametrize('cancelled',[False,True])
def test_failure_keeps_results_and_budget_rejection_is_not_a_call(tmp_path,cancelled):
    audit=ConceptAudit(tmp_path,make_point(),make_task(),'run-1',protocol='v2')
    audit.event(0,'concept_review_progress',dict(accepted=[1,2],unresolved={'3':'待处理'},issues={'3':[{'issue_id':'I-C3-role','status':'unresolved'}]}))
    audit.event(0,'strategy_review_progress',dict(accepted_strategies=[{'level':'strict'}],failed_levels={}))
    audit.event(0,'strategy_budget_exhausted',dict(level='medium',operation='generate'))
    audit.finish(error='CancelledError' if cancelled else 'budget exhausted',cancelled=cancelled)
    summary=json.loads((audit.directory/'summary.json').read_text())
    assert summary['status']==('cancelled' if cancelled else 'failed')
    assert summary['concept_result']['accepted']==[1,2]
    assert summary['strategy_result']['accepted_strategies']==[{'level':'strict'}]
    assert summary['strategy_calls']==0
    assert summary['metrics']['strategy_budget_exhausted']==1
    assert len(summary['implementation_sha256'])==64


def test_replayed_generation_is_separated_from_request_records(tmp_path):
    audit=ConceptAudit(tmp_path,make_point(),make_task(),'run-1')
    audit.event(0,'concept_call',dict(replayed=True,output='fixture'))
    audit.event(0,'concept_call',dict(output='actual model response'))
    audit.finish()
    summary=json.loads((audit.directory/'summary.json').read_text())
    assert summary['concept_calls']==2
    assert summary['replayed_generation_records']==1
    assert summary['concept_request_records']==1
