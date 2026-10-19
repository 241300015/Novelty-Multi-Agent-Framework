---
name: reviewer-information-adjudication
description: 查新点级 Reviewer 的最小新颖性判定原则占位。
---

# Reviewer information adjudication

Reviewer evaluates novelty at NoveltyPoint level.

- Compare the point with the available prior works.
- Use source evidence rather than unsupported model knowledge.
- Distinguish complete overlap, partial overlap, and remaining differences.
- Do not interpret "not found" as proof of non-existence.
- When evidence is insufficient, return `insufficient_evidence`.

This V0 skill is a future methodology extension point. It does not define a
decision tree, scoring system, search behavior, or workflow routing policy.
