---
name: search_planner.concept_review
version: 2
system: |
  独立复核每个候选。source_supported 判断查新点是否支持此概念，source_quote 必须逐字引用输入查新点或技术特征，不引用来源位置。
  terms 中每个输入词项逐一给出判断与理由；aliases 中每个别名逐一判断单独命中时能否保留主概念的必要技术含义。不得混淆 terms 和 aliases，空数组返回空数组。
  完整复核指出核心机制遗漏。局部复核只审当前候选及提供的原问题，结合修正前后，issue_resolution 填 fixed（问题成立且已修复）、invalid（原问题不成立）或 unresolved（仍未解决）；resolution_reason 解释依据。候选未修改时不可声称 fixed。
---
任务输入
{payload_json}

输出 JSON 契约
{schema_json}
