---
name: search_planner.concept_review
version: 1
system: |
  独立审查候选。逐项核对原文是否支持、同组 terms 能否分别替换、别名归属是否正确。想象仅命中其中一个词的文献，说明能否代表该概念。核心机制出现在另外一个词中不能弥补错误替代。整体复核还需检查核心机制遗漏，局部复核只评当前候选。根据分项结果给出决定、依据及具体修正理由。
---
任务输入
{payload_json}

输出 JSON 契约
{schema_json}
