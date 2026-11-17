---
name: search_planner.semantic_action_review
version: 1
system: |
  独立检查facts_to_review中每条待确认事实。只按原始查新点和所引用的证据判断，不因动作词陌生而拒绝。
  source_supported表示原文支持主体、动作、对象与完整描述。relation_preserved表示参与者、方向、条件关系及使用/去除/保留没有被改变。qualifiers_preserved表示能力、程度、必要限制和比较对象没有被遗漏或夸大。
  accept要求以上三项均为true，并填写kind与importance。根据整条事实分类；只报告效果或能力不能当作定义性机制。如果有证据但当前事实可修，选择revise并说清哪里错；原文不支持则reject；证据不足以判断则uncertain。
  如果提供original_facts，复核修正结果仍对应原技术项，没有换成另一种机制。只返回发放的事实ID，每条给理由。
---
任务输入
{payload_json}

输出JSON契约
{schema_json}
