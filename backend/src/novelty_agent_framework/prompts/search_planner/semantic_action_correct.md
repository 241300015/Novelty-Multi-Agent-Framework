---
name: search_planner.semantic_action_correct
version: 1
system: |
  根据原始输入和review_reasons一次性纠正facts_to_review中的事实。只返回发放的事实ID，保留同一技术项，修正动作方向、条件和来源。不要把整条事实替换成另一个机制。每条提供完整描述，并只用当前目录中的证据ID。
---
任务输入
{payload_json}

输出JSON契约
{schema_json}
