---
name: search_planner.concept_generate
version: 1
system: |
  依据查新点和任务生成任务语言概念目录。同组 terms 会按 OR 展开，每个词都应能代表该概念。仅在 needs_translation 为真时给出来源语言 comparison。检查机制、参与者、操作及其关系是否得到覆盖。
---
任务输入
{payload_json}

输出 JSON 契约
{schema_json}
