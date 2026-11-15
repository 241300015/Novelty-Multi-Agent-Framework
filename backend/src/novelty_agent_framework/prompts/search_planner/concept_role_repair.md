---
name: search_planner.concept_role_repair
version: 1
system: |
  只给invalid_roles每个字段选择合法role，updates按field_path精确覆盖，不重写概念。
  object=对象/参与实体；method=算法/技术手段；feature=机制特征/操作属性；setting=数据集/场景/条件；escape=任务指定兜底概念。
  operation/participant/application都不是合法role，应根据概念具体含义重新选择，不使用一对一字符串映射。
---
任务输入
{payload_json}

输出JSON契约
{schema_json}
