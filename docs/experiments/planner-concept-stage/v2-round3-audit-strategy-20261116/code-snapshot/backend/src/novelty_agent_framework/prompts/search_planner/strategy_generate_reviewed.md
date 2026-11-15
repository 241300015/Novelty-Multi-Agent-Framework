---
name: search_planner.strategy_generate_reviewed
version: 2
system: |
  只生成当前level的检索表达式。只能使用当前通过目录中的概念ID、AND、OR和括号，不使用NOT或新概念。
  strict较完整地覆盖核心技术组合，medium合理放宽，broad仍保留能识别查新机制的必要条件，不能只剩“微调模型”“新闻摘要”等泛称。
  不要求三档机械嵌套，不能与最近通过档的实际展开式完全重复。previous_passed为空表示尚无通过策略。
  medium/broad允许减少strict的必要AND条件或用合理的OR分组覆盖相关路径；不能把strict里的每个技术细节都视为不可省略。保留能识别目标机制的组合即可，不要在“去掉一项”和“全部加回”之间来回振荡。
  修复时结合feedback与previous_candidate，仅改当前策略，不修改通过概念或其他档位。输出expression、use_alias、use_exclude。
---
任务输入
{payload_json}

输出JSON契约
{schema_json}
