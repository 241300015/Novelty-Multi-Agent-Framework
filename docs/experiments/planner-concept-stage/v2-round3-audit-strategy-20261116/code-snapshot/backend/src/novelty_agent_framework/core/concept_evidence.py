"""Deterministic, task-bound slices of the actual Planner input."""
import hashlib
import re


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def build_catalog(point, task):
    sources = []
    fields = {}
    binding = digest(f'{point.point_id}\0{task.task_id}')[:16]
    for field in ('claim', 'claim_en', 'technical_features', 'technical_features_en'):
        value = getattr(point, field)
        values = value if isinstance(value, list) else [value]
        for index, raw in enumerate(values):
            path = f'{field}[{index}]' if isinstance(value, list) else field
            fields[path] = digest(raw)
            # A full stop is a boundary only before whitespace/end, preserving
            # decimals, dotted identifiers and abbreviations within a sentence.
            cuts = [m.end() for m in re.finditer(r'[。！？!?]+|\n+|\.(?=\s|$)', raw)
                    if not (m.group() == '.' and re.search(
                        r'(?:\b(?:Dr|Mr|Mrs|Ms|Prof|Fig|Eq|vs|etc)|\b[A-Za-z]|e\.g|i\.e)\.$',
                        raw[:m.end()], re.IGNORECASE))]
            start = 0
            for end in sorted(set(cuts + [len(raw)])):
                text = raw[start:end]
                if text.strip():
                    sources.append(dict(source_id=f'S-{binding}-{len(sources)+1:03}',
                        field=field, field_index=index if isinstance(value, list) else None,
                        language='en' if field.endswith('_en') else 'zh',
                        start=start, end=end, text=text, sha256=digest(text)))
                start = end
    return dict(catalog_version=1, point_id=point.point_id, task_id=task.task_id,
                fields=fields, sources=sources)


def resolve_evidence(catalog, ids, point, task):
    if (catalog['point_id'], catalog['task_id']) != (point.point_id, task.task_id):
        raise ValueError('evidence.binding: catalog belongs to another point/task')
    expected = build_catalog(point, task)
    if catalog != expected:
        raise ValueError('evidence.integrity: catalog does not match current input')
    by_id = {s['source_id']: s for s in catalog['sources']}
    if len(ids) != len(set(ids)) or any(i not in by_id for i in ids):
        raise ValueError(f'evidence.id: expected issued IDs {list(by_id)}')
    return [by_id[i].copy() for i in ids]
