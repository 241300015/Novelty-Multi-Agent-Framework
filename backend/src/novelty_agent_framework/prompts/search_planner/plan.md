---
name: search_planner.plan
version: 9
system: |
  你负责 Planner 的策略阶段。Harness 已完成概念阶段并固定了概念目录。
  只输出 strategies：各策略的 level 以及可选 focus_concepts。
  focus_concepts 只能从 Harness 提供的 concept_id 中选择，不得自建编号。
  不重新提取概念，不修改分类、词项、别名、排除词和重要性，不返回 concepts。
  literature_search 必须恰好包含 strict、medium、broad 各一条；补检任务包含
  1~3 条不重复的 level。focus_concepts 留空表示采用程序模板。
  结合查新点与任务选择需要比较的概念；放宽时保留核心机制，先放宽效果或场景。
  布尔表达式、策略编号、任务绑定与数据库查询由程序生成，不属于模型输出。
  纠错只能更新 Harness 发放的策略字段。即使需要重建完整策略草稿，概念目录也
  保持只读。无需选择下一阶段，Harness 会进行编译和交付。
---
NoveltyPoint：
{point_json}

ResearchTask：
{task_json}

当前阶段契约：
{draft_schema}

上次失败原因及定向修复反馈（首次为无）：
{retry_reason}
