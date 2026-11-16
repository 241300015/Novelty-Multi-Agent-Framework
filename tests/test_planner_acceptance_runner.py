import importlib.util
from pathlib import Path
import sys

import pytest

spec = importlib.util.spec_from_file_location('acceptance_runner', Path(__file__).parents[1]/'scripts/run_planner_acceptance.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_matrix_contains_exact_original_scenarios_and_independent_tasks():
    jobs = runner.matrix({'samples': [{'id': 'graph'}, {'id': 'music'}]})
    assert len(jobs) == len({j['case_id'] for j in jobs}) == 30
    assert len({(j['sample'], j['scenario'], j['repeat']) for j in jobs}) == 24
    for sample in ('graph', 'music'):
        for repeat in (1, 2, 3):
            assert {j['language'] for j in jobs if j['sample'] == sample and j['scenario'] == 'D' and j['repeat'] == repeat} == {'zh', 'en'}


@pytest.mark.parametrize('scenario,zh,en', [('A', True, False), ('B', True, False), ('C', False, True), ('D', True, True)])
def test_source_language_mask_matches_original_matrix(scenario, zh, en):
    import json
    sample = json.loads(runner.BENCHMARK.read_text())['samples'][0]
    point, _ = runner.task_point(sample, scenario)
    assert bool(point.claim) == zh
    assert bool(point.claim_en) == en
    assert bool(point.technical_features) == zh
    assert bool(point.technical_features_en) == en


def draft():
    from novelty_agent_framework.core.concept_stage import ConceptBatch
    return ConceptBatch.model_validate(dict(concepts=[
        dict(terms=['细粒度注意力'], alias=['细粒度注意力机制'], role='method', importance=1),
        dict(terms=['粗粒度注意力'], alias=[], role='method', importance=1)], comparison=[]))


def test_alias_injection_moves_one_existing_alias_and_preserves_original():
    before = draft()
    changed = runner.inject(before, dict(kind='alias', target='细粒度注意力', alias='细粒度注意力机制', destination='粗粒度注意力'))
    assert before.concepts[0].alias == ['细粒度注意力机制']
    assert changed.concepts[0].alias == []
    assert changed.concepts[1].alias == ['细粒度注意力机制']
    assert [c.terms for c in changed.concepts] == [c.terms for c in before.concepts]


def test_ineligible_injection_is_not_fabricated():
    with pytest.raises(ValueError, match='injection_condition_not_met'):
        runner.inject(draft(), dict(kind='alias', target='细粒度注意力', alias='不存在的别名', destination='粗粒度注意力'))
    changed = runner.inject(draft(), dict(kind='omit', target='细粒度注意力'))
    assert len(changed.concepts) == 1


def test_raw_outputs_must_be_in_ignored_report_directory():
    with pytest.raises(ValueError, match='local-reports'):
        runner.output_root(runner.ROOT/'docs/raw-model-output')


def test_missing_credentials_stop_before_creating_a_batch(tmp_path, monkeypatch):
    def missing():
        raise ValueError('Configured model credential is missing')
    monkeypatch.setattr(runner, 'configured_agent', missing)
    with pytest.raises(ValueError, match='credential is missing'):
        runner.prepare(tmp_path)
    assert not (tmp_path/'manifest.json').exists()


def test_injection_task_ids_do_not_reveal_the_injection_or_expected_answer():
    sys.path.insert(0, str(runner.ROOT/'scripts'))
    from prepare_planner_injections import jobs_for_fixture
    jobs = [j for kind in ('omit', 'alias') for j in jobs_for_fixture(kind, {}, 'source', 'fixture.json')]
    assert len({j['case_id'] for j in jobs}) == 6
    assert all(not any(word in j['case_id'] for word in ['omit', 'alias', 'inject']) for j in jobs)


def test_runtime_completed_is_not_independent_semantic_acceptance():
    sys.path.insert(0, str(runner.ROOT/'scripts'))
    from summarize_planner_acceptance import classify, DIMENSIONS
    row = dict(audit_verified=True, execution='completed', has_plan=True, accepted_strategies=[
        {'level': level} for level in ('strict', 'medium', 'broad')], unresolved={})
    assert classify(row, None) == 'unverified'
    review = dict(basis='independent_source_and_approved_rules', dimensions={
        key: dict(status='pass', reason='Source-grounded check', evidence=['actual-output']) for key in DIMENSIONS})
    assert classify(row, review) == 'complete'
    review['dimensions']['terms_aliases']['status'] = 'fail'
    assert classify(row, review) == 'not_met'


@pytest.mark.parametrize('changed_source', [False, True])
def test_replayed_source_standard_rebinds_evidence_but_rejects_changed_input(tmp_path, monkeypatch, changed_source):
    import asyncio
    import hashlib
    import json
    from types import SimpleNamespace
    sys.path.insert(0, str(runner.ROOT/'scripts'))
    import run_planner_control_replay as driver
    from test_task_semantics import contract_fixture
    from novelty_agent_framework.core.concept_evidence import build_catalog
    from novelty_agent_framework.core.task_semantics import validate_contract_binding
    point, task, contract = contract_fixture()
    (tmp_path/'fixture.json').write_text(json.dumps(dict(contract=contract, catalog=build_catalog(point, task))))
    (tmp_path/'manifest.json').write_text(json.dumps(dict(
        control_replay_driver_sha256=hashlib.sha256(Path(driver.__file__).read_bytes()).hexdigest(),
        injection_jobs=[dict(case_id='control-07-zh', semantic_fixture='fixture.json')])))
    current_task = task.model_copy(update={'task_id': 'control-07-zh'})
    current_point = point.model_copy(update={'claim': point.claim+' changed'}) if changed_source else point
    monkeypatch.setattr(driver.runner, 'configured_agent', lambda: SimpleNamespace())
    records = []
    async def fake_case(root, case_id):
        agent = driver.runner.configured_agent()
        value = await agent._semantic_phase(current_point, current_task, True, lambda *event: records.append(event))
        validate_contract_binding(value, current_point, current_task)
        known = {s['source_id'] for s in build_catalog(point, current_task)['sources']}
        assert all(set(f['evidence_ids']) <= known for f in value['facts'].values())
        assert value['contract_id'] != contract['contract_id']
    monkeypatch.setattr(driver.runner, 'case', fake_case)
    if changed_source:
        with pytest.raises(ValueError, match='same source input'):
            asyncio.run(driver.run_case(tmp_path, 'control-07-zh'))
    else:
        asyncio.run(driver.run_case(tmp_path, 'control-07-zh'))
        assert len([e for e in records if e[1]=='semantic_call' and e[2]['replayed']]) == 2
