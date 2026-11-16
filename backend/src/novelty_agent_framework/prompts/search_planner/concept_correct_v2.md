---
name: search_planner.concept_correct_v2
version: 1
system: |
  仅处理当前候选及issues。输出完整concept，但只可修改issues.allowed_fields的并集，其他字段逐字保留。
  role错误仅更正分类，不得把对象换成方法。来源不支持时保留原候选给复核判断，不得改成原文中另一个受支持概念。
  terms组内非等价问题只保留原有词项中的等价表达，不得将多个方法步骤拼为同组OR词；新机制需要作为新候选处理。
  原候选为null时按missing问题生成一个新概念。保留任务语言及技术含义。不要维护预算或改写其他候选。
  有semantic_contract时保持事实的使用/去除/保留方向，按原始事实修复候选，不修改判据。
---
任务输入
{payload_json}

输出JSON契约
{schema_json}
