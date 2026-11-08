"""Concise report projections of sealed reviews; no new novelty adjudication."""
from __future__ import annotations

import json

from ..core.report_wording import (
    INCOMPLETE_REVIEW_LABELS, LEGACY_REVIEW_SUMMARIES, NO_EFFECTIVE_EVIDENCE,
    incomplete_review_summary,
)


def _unique(values):
    return list(dict.fromkeys(value for value in values if value))


def concise_conclusions(data, cards, card_numbers, card_works, *, readable, label, summary):
    """Use existing bibliography numbers only when an explicit link is unambiguous."""
    points = data["points"].get("novelty_points", [])
    report = data["report"]
    conclusions = {row.get("novelty_point_id"): row for row in report.get("conclusions", [])}
    rows = [*(data.get("reviews", {}).get("card_reviews") or []),
            *(report.get("partial_card_reviews") or [])]
    overview, sections = [], []
    for index, point in enumerate(points, 1):
        pid = point.get("point_id")
        point_cards = {c["card_id"]: c for c in cards if c.get("novelty_point_id") == pid}
        conclusion = conclusions.get(pid, {})
        verdict = label(conclusion) if conclusion else "尚未形成结论"
        narrative = summary(conclusion, has_cards=bool(point_cards)) if conclusion else ""
        placeholders = {*LEGACY_REVIEW_SUMMARIES, NO_EFFECTIVE_EVIDENCE + "。",
                        *(v + "。" for v in INCOMPLETE_REVIEW_LABELS.values()),
                        incomplete_review_summary(None)}
        if narrative in placeholders:
            narrative = ""
        if not narrative and conclusion.get("review_status") == "reviewed":
            narrative = conclusion.get("verdict_reason") or ""
        overview.append(f"查新点 {index}：{verdict}。" + (readable(narrative) if narrative else ""))
        point_text = [f"#### 查新点 {index}：{point.get('claim', '未提供')}",
                      f"已有点级结论：{verdict}。"]
        if conclusion.get("review_status") == "reviewed" and conclusion.get("verdict_reason"):
            point_text.append("裁定依据：" + readable(conclusion["verdict_reason"]))
        if not point_cards:
            point_text.append(NO_EFFECTIVE_EVIDENCE + "。不能据此认定相关文献不存在。")

        # Only current-point, accepted-card reviews may contribute prose. Legacy
        # point-shaped single-card results are read without promoting their verdict.
        local = [r for r in rows if r.get("card_id") in point_cards
                 and r.get("novelty_point_id") == pid
                 and (r.get("review") or {}).get("novelty_point_id") == pid]
        completed = [r for r in local if r.get("status", "completed") == "completed"
                     and r["review"].get("status") == "reviewed"]
        sources = [conclusion, *(r["review"] for r in completed)]
        work_numbers = {}
        for cid in point_cards:
            for wid in card_works.get(cid, set()) - {None, ""}:
                work_numbers.setdefault(wid, set()).add(card_numbers[cid])
        for source in sources:
            for work in source.get("highly_relevant_works") or []:
                for cid in work.get("card_ids") or []:
                    if cid in point_cards:
                        work_numbers.setdefault(work.get("work_id"), set()).add(card_numbers[cid])
            for evidence in source.get("review_evidence") or []:
                cid = evidence.get("origin_card_id")
                if cid in point_cards:
                    work_numbers.setdefault(evidence.get("work_id"), set()).add(card_numbers[cid])

        entries = {}
        for cid, card in point_cards.items():
            entries.setdefault(card_numbers[cid], {"cards": [], "comparisons": [], "works": []})["cards"].append(card)

        def entry_for(wid):
            numbers = work_numbers.get(wid, set())
            key = next(iter(numbers)) if len(numbers) == 1 else ("unlinked", wid)
            return entries.setdefault(key, {"cards": [], "comparisons": [], "works": []})

        # Deduplicate identical records across point and card reviews while
        # retaining disagreements and source evidence restrictions.
        seen = set()
        for source in sources:
            for field, dest in (("feature_comparisons", "comparisons"), ("highly_relevant_works", "works")):
                for record in source.get(field) or []:
                    signature = (field, json.dumps(record, sort_keys=True, ensure_ascii=False))
                    if signature not in seen:
                        seen.add(signature)
                        entry_for(record.get("work_id"))[dest].append(record)

        features = {f"F{i}": text for i, text in enumerate(point.get("technical_features") or [], 1)}
        for key, entry in entries.items():
            group = entry["cards"]
            ids = {c["card_id"] for c in group}
            if isinstance(key, int):
                title = f"**文献[{key}]**"
            else:
                title = f"**未关联展示编号的文献（work_id: {key[1] or '未记录'}）**"
            lines = [title]
            if not isinstance(key, int):
                lines.append("文献身份与展示编号的关联缺失或不唯一；保留原始标识待核对。")
            contributions = _unique(readable(c.get("main_contribution") or "") for c in group)
            lines.append("相关工作：" + ("；".join(contributions) or "未记录可关联的论文工作概述。"))
            card_results = [r["review"] for r in completed if r["card_id"] in ids]
            support = _unique(readable(r.get("support_reason") or "") for r in card_results)
            if support:
                lines.append("材料评审：" + "；".join(support))
            if not card_results:
                lines.append("未提供已完成的单篇评审；已有点级记录仅按其原有范围展示。")

            # Existing card evidence IDs and resolved review evidence are the only
            # available local closure; do not invent a source or infer it by title.
            evidence_ids = {eid for c in group for eid in c.get("evidence_ids") or []}
            review_evidence = [e for s in sources for e in s.get("review_evidence") or []
                               if e.get("origin_card_id") in ids]
            for work in entry["works"]:
                refs = work.get("evidence_ids") or []
                valid = evidence_ids | {e.get("evidence_id") for e in review_evidence
                                        if e.get("work_id") == work.get("work_id")}
                suffix = "" if refs and set(refs) <= valid else "（证据引用缺失或无法核对，相关性尚待确认）"
                lines.append("已有相关性记录：" + readable(work.get("relevance_reason") or "未记录依据") + suffix)

            covered = set()
            unresolved = []
            for comparison in entry["comparisons"]:
                fid = comparison.get("feature_id")
                covered.add(fid)
                feature = features.get(fid, f"特征标识 {fid or '未记录'}（名称未记录）")
                relation = comparison.get("relation")
                refs = comparison.get("evidence_refs") or []
                valid = evidence_ids | {e.get("evidence_id") for e in review_evidence
                                        if e.get("work_id") == comparison.get("work_id")}
                grounded = bool(refs) and set(refs) <= valid
                assessment = {"supported": "与该特征相符", "partially_supported": "与该特征部分相符",
                              "contradicted": "与该特征不符", "unknown": "尚不能确认该特征"}.get(relation, "比较状态未记录")
                reason = readable(comparison.get("reason") or "未记录比较依据").rstrip("。；;")
                if relation == "unknown":
                    unresolved.append(feature)
                    detail = f"尚不能确认该特征：{reason}；未读到或未提及不能作为已证实差异。"
                elif not grounded:
                    unresolved.append(feature)
                    detail = f"原比较记录为“{assessment}”：{reason}；证据引用缺失或无法核对，不能确认该判断。"
                else:
                    detail = f"{assessment}：{reason}"
                source_context = {"author_method": "作者方法", "related_work": "相关工作",
                                  "baseline": "对比基线", "ablation": "消融实验"}.get(comparison.get("source_context"))
                if source_context:
                    detail += f"（依据涉及：{source_context}）"
                lines.append(f"- {feature}：{detail}")
            unresolved.extend(text for fid, text in features.items() if fid not in covered)
            if unresolved:
                lines.append("尚不能确认：" + "；".join(_unique(unresolved)) + "。")
            elif not entry["comparisons"]:
                lines.append("尚不能确认：未记录逐特征比较，证据不足以补写对应或差异。")
            else:
                lines.append("比较范围限于上述已记录特征与证据，不外推到未评审内容。")
            point_text.append("\n\n".join(lines))
        sections.append("\n\n".join(point_text))
    return "\n\n".join(overview) or "尚未形成查新结论。", "\n\n".join(sections) or "未生成查新点。"
