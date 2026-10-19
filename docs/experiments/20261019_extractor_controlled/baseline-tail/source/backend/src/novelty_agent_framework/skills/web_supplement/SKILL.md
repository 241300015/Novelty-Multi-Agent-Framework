---
name: web_supplement
description: Collect optional search advice only when no scholarly papers can be retrieved; web materials never become literature or novelty evidence.
---

# Web supplementary material

Use only after paper retrieval is unavailable or has yielded no papers. First try registered databases and available reference papers within budget. Existing paper candidates with weak evidence, missing full text, language preferences or external-context requests alone do not authorize replacing literature with Web evidence.

Web search results are supplementary material. Keep source_kind = web_supplement for internal audit. Do not create Evidence, EvidenceCard, related-literature entries, citations or attachment lists from Web content. Even Reader-accessible web text and LLM summaries remain supplementary or derived information, not original Evidence. Do not run browser/reader solely to promote Web material into a paper.

If any paper was retrieved anywhere in the report scope, omit Web advice from the report. Zero cards does not mean zero papers. If no papers were retrieved and Web material exists, offer a short recommendation for refining scholarly queries using terminology or search directions from that material. Do not enumerate webpages, titles or URLs; do not treat web claims as established facts or use them to adjudicate novelty. Retrieval failures must not be called successful zero-hit searches or proof of non-existence.

Avoid repeated Web searches: one supplementary search is normally sufficient for advice. Return cards=[] and a concrete no_evidence_reason when no paper Reader text supports a card. The report renderer decides whether the report-wide no-paper condition holds.

## Query examples

Only put short search keywords in query; never reasoning, explanations or the full task. For Baidu, the limit is 72 units: ASCII (including spaces) counts 1, non-ASCII counts 2. Aim below 60 units. Good queries: "图摘要 分布式GNN"; "graph summarization distributed GNN". Bad query: a sentence explaining why to search followed by all task features. On INVALID_QUERY, shorten to two or three core concepts; do not repeat the same query. A successful zero-hit result is valid and marked zero_hits; keep it in the audit, never call it a service failure.
