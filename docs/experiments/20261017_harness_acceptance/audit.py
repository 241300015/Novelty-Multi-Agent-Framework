"""Audit persisted live runs; no model/network calls and no production mutations."""
from pathlib import Path
import json
import sys
from collections import Counter

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.compare_researcher_harness import read, write, canonical, verify_fixture


def audit(root):
    verify_fixture(root)
    report = {'baseline':read(root/'acceptance-start.json'), 'cases':{}, 'comparability':{}}
    inputs = {}
    for case in sorted((root/'runs').glob('*/*')):
        if not (case/'metrics.json').exists():
            continue
        arm, point = case.parts[-2:]
        identity = f'{arm}/{point}'
        actual = read(case/'actual-input.json')
        result = read(case/'result.json')
        metrics = read(case/'metrics.json')
        calls = [read(p) for p in sorted(case.glob('MF2033k6lC/runtime/*/llm_calls/*.json'))]
        inputs.setdefault(point, {})[arm] = actual
        formats, invalid, schemas, native, roles, catalogs, repair = [], [], [], 0, 0, 0, []
        settings = []
        response_native = 0
        explicit_no_tools = []
        prefix = 'Current Harness choices (only these option_id values are valid):\n'
        for call in calls:
            payload = call.get('request_payload') or {}
            messages = payload.get('messages') or []
            response = call.get('response') or {}
            response_native += bool(response.get('tool_calls'))
            explicit_no_tools.append(payload.get('tool_choice') == 'none')
            settings.append({k:payload.get(k) for k in ('model','temperature','max_tokens')}
                            | {'endpoint':call.get('request_options',{}).get('endpoint')})
            native += bool(payload.get('tools'))
            roles += any(m['role'] == 'tool' for m in messages)
            catalogs += any('Available tools' in (m.get('content') or '') for m in messages if isinstance(m.get('content'),str))
            last = messages[-1].get('content') if messages else None
            if isinstance(last,str):
                try:
                    value = json.loads(last)
                    if isinstance(value,dict) and 'rejected_cards' in value:
                        repair.append({'call_id':call['llm_call_id'],'rejected_codes':[r.get('error_code') for r in value['rejected_cards']]})
                except ValueError:
                    pass
            if isinstance(last,str) and last.startswith(prefix):
                menu = json.loads(last[len(prefix):].split('\nReturn',1)[0])
                schema = (payload.get('response_format') or {}).get('json_schema',{}).get('schema',{})
                if menu:
                    enums = schema.get('properties',{}).get('option_id',{}).get('enum')
                    schemas.append(enums == list(menu) and schema.get("required") == ["option_id"]
                                   and schema.get("additionalProperties") is False
                                   and (payload.get("response_format") or {}).get("type") == "json_schema"
                                   and payload["response_format"]["json_schema"].get("strict") is True)
                    try:
                        value = json.loads(response.get('content') or '')
                    except ValueError:
                        value = None
                    valid = isinstance(value,dict) and set(value)=={'option_id'} and isinstance(value['option_id'],str) and value['option_id'] in menu
                    formats.append(valid)
                    if not valid:
                        invalid.append(call['llm_call_id'])
                else:
                    assert not schema.get('properties',{}).get('option_id'), identity
        retrieval = read(case/'retrieval-calls.json') if (case/'retrieval-calls.json').exists() else []
        discovery = [r for r in retrieval if r['kind']=='discovery']
        manifest = read(case/'MF2033k6lC/references/list.json')
        artifacts = {a['artifact_id']:a for a in manifest['artifacts']}
        read_rows = result.get('read_results',[])
        read_kinds = Counter(artifacts.get(r['artifact_id'],{}).get('role','unknown') for r in read_rows)
        builder = read(case/'builder-calls.json')
        row = {'metrics':metrics, 'task_sha256':actual['task_sha256'], 'model_settings':list({canonical(s):s for s in settings}.values()),
            'native_tool_responses':response_native, 'all_tool_choices_none':all(explicit_no_tools),
            'requests_with_tools':native, 'requests_with_tool_role':roles, 'requests_with_available_tools_text':catalogs,
            'decision_count':len(formats),'valid_decisions':sum(formats),'invalid_decision_calls':invalid,
            'decision_schemas_match':all(schemas) if schemas else None,
            'search_strategy_levels':[[s['level'] for s in r['search_plan']['strategies']] for r in discovery],
            'acquisition_calls':sum(r['kind']=='acquisition' for r in retrieval),
            'read_result_roles':dict(read_kinds), 'fulltext_read_to_eof':sum(not r['has_more'] and artifacts.get(r['artifact_id'],{}).get('role')=='extracted_text' for r in read_rows),
            'correction_requests':repair,
            'builder_rejections_per_attempt':[b['result'].get('rejections',[]) for b in builder],
            'cards':[{'card_id':c['card_id'],'title':c['document_title'],'main_contribution':c['main_contribution'],
                'overlaps':c['overlaps'],'differences':c['differences'],'evidence_ids':c['evidence_ids']} for c in result['evidence_cards']],
            'harness_diagnostic_counts':dict(Counter(r['code'] for r in result.get('harness_diagnostics',[])))}
        if arm == 'choice_only':
            row['protocol_pass'] = bool(schemas) and all(schemas) and all(formats) and not native and not roles and not catalogs and not response_native and all(explicit_no_tools)
            row['bounded_discovery_pass'] = len(discovery)<=3 and all(r['search_plan']['strategies'] for r in discovery)
        report['cases'][identity] = row
    for point, variants in inputs.items():
        names = ('max_steps','max_tool_calls','max_chars_per_read','max_total_read_chars','per_tool_limits')
        report['comparability'][point] = {'all_three_arms_finished':set(variants)=={'original','split_menu','choice_only'},
            'identical_tasks':len({v['task_sha256'] for v in variants.values()})==1,
            'identical_budgets':len({canonical({k:v['budgets'][k] for k in names}) for v in variants.values()})==1,
            'identical_model_settings':len({canonical(s) for arm in variants
                for s in report['cases'][f'{arm}/{point}']['model_settings']})==1}
    report['mechanical_paths'] = read(root/'mechanical-paths.json')
    write(root/'acceptance-audit.json',report)
    print(json.dumps({'finished_cases':list(report['cases']),'comparability':report['comparability']},ensure_ascii=False,indent=2))


if __name__=='__main__':
    audit(Path(sys.argv[1]).resolve())
