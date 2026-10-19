"""Check stored experiment evidence, without model calls or PDF access."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend/src'), str(ROOT)]
from novelty_agent_framework.agents.point_extractor import build_paper_digest
from novelty_agent_framework.schemas import PaperInput


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    root = parser.parse_args().directory

    def read(run, file):
        return json.loads((root / run / file).read_text())

    summary = []
    for path in sorted(root.glob('*/result.json')):
        result = json.loads(path.read_text())
        trace = read(path.parent.name, 'trace.json')
        summary.append({'run': path.parent.name, 'status': result['status'],
            'execution_status': result.get('execution_status'), 'calls': result['calls'],
            'seconds': round(result['duration_seconds'], 3), 'tokens': result['usage']['total_tokens'],
            'points': len(result.get('points', [])), 'budgets': result.get('budgets'),
            'read_ranges': [[r['start'], r['end']] for r in trace['reading']['reads']]})

    checks = {}
    tail = read('final-tail', 'trace.json')
    result = read('final-tail', 'result.json')
    checks['synthetic_late_contribution'] = (result['status'] == 'completed'
        and not result['digest_has_late_marker']
        and any('HELIOS-LATE' in r['text'] and r['start'] == 8000 for r in tail['reading']['reads'])
        and any('HELIOS-LATE' in str(p) and any('full_text[8000:8351]' in s
            for s in p['source_locations']) for p in result['points']))

    real = read('final-real-tail', 'trace.json')
    result = read('final-real-tail', 'result.json')
    ref = result['input_reference']
    # Resolve within the evidence directory, so a moved checkout remains auditable.
    source = root / 'final-real-tail/inputs' / ref['paper_id'] / Path(ref['index_path']).parent / 'paper.json'
    paper = PaperInput.model_validate_json(source.read_text())
    body = paper.full_text
    passage = 'DSGNN 框架采用一个基于损失函数值的\n注意力机制'
    start = body.index(passage)
    end = body.index('其原理如图4 6所示。', start) + len('其原理如图4 6所示。')
    original = json.dumps(build_paper_digest(paper).model_dump(mode='json'), ensure_ascii=False)
    generation = next(c for c in real['calls'] if c['input']['phase'] == 'generate')
    checks['real_tail_body_delivered_verbatim'] = (len(body) == 81079
        and generation['input']['reads'][0]['text'] == body[52000:56000]
        and 52000 <= start < end <= 56000 and passage not in original)
    checks['real_tail_mechanism_cited'] = (result['status'] == 'completed'
        and any(any('注意力' in f and '拓扑' in f and '弥补' in f for f in p['technical_features'])
            and any('full_text[52000:56000]' in s for s in p['source_locations']) for p in result['points']))

    duplicate = read('fault-duplicate-v5', 'trace.json')
    duplicate_result = read('fault-duplicate-v5', 'result.json')
    checks['duplicate_review_repair_review'] = (duplicate_result['injection_applied']
        and len(duplicate['versions'][0]['points']) == 2 and len(duplicate_result['points']) == 1
        and any(i['code'] == 'duplicate' for r in duplicate['reviews'][0]['point_reviews'] for i in r['issues'])
        and any(c['kind'] == 'semantic' for c in duplicate['corrections'])
        and duplicate['reviews'][-1]['version'] == duplicate['versions'][-1]['version']
        and duplicate['semantic_review_passed'])
    for name, role in [('reader', 'read'), ('generation', 'generate'), ('review', 'review')]:
        run = f'final-invalid-{name}'
        trace, result = read(run, 'trace.json'), read(run, 'result.json')
        checks[f'{name}_legality_corrected'] = (result['status'] == 'completed' and result['injection_applied']
            and any(c['kind'] == 'legality' and c['role'] == role and c['status'] == 'verified'
                for c in trace['corrections']) and trace['budgets']['used'][role] == 2)

    suites = ET.parse(root / 'regression.xml').getroot()
    tests = [case for case in suites.iter('testcase')]
    checks['both_entries_fresh_process_in_both_modes'] = (len([c for c in tests
        if c.attrib['name'].startswith('test_fresh_process_reads_entire_input')]) == 4)
    checks['regression_passed'] = all(int(s.attrib['errors']) + int(s.attrib['failures']) == 0 for s in suites)
    production_files = ['backend/src/novelty_agent_framework/core/extractor_harness.py'] + [
        f'backend/src/novelty_agent_framework/prompts/extractor/{name}.md'
        for name in ('generate', 'semantic_review', 'reader_choices', 'correct')]
    for run in ('final-tail', 'final-real-tail', 'final-invalid-reader', 'final-invalid-generation',
                'final-invalid-review', 'fault-duplicate-v5'):
        files = {f['path']: f['sha256'] for f in read(run, 'manifest.json')['files']}
        checks[f'{run}_matches_current_harness_and_prompts'] = all(
            hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == files[p] for p in production_files)

    evidence = {'checks': checks, 'regression_tests': len(tests), 'runs': summary,
        'real_tail_detail': {'start': start, 'end': end, 'text': body[start:end],
            'reading_was_pinned': True, 'autonomous_selection_verified': False,
            'formula_detail_retained': False},
        'limitations': ['These checks verify recorded actions and selected examples, not universal semantic correctness.',
            'fault-duplicate-v2 falsely passed duplicate candidates.',
            'real-paper-v3 read only the opening and passed a proportion/reduction misstatement.',
            'The real-paper tail selection was supplied by the evaluator; full-paper coverage is not verified.']}
    (root / 'acceptance.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'checks_passed': sum(checks.values()), 'checks_total': len(checks),
                      'regression_tests': len(tests)}, ensure_ascii=False))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
