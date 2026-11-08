---
name: search_planner.concept_correct
version: 1
system: |
  根据查新点及问题描述，仅修复当前候选或生成缺失概念。保持任务语言，确保同组检索词可互相替代，别名归属一致。
---
任务输入
{payload_json}

输出 JSON 契约
{schema_json}
