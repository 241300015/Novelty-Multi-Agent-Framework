---
name: search_planner.concept_generate_v2
version: 1
system: |
  根据查新点生成任务语言概念。role只能选object、method、feature、setting、escape。
  object是研究对象或参与实体，method是算法/技术手段，feature是机制特征或操作属性，setting是数据集/场景/条件，escape是任务要求的兜底检索概念。操作不是新role，按内容归入method或feature；参与者归object。
  每个概念先给一个明确主词。多个不同机制分别建概念，不得把共同组成方法的步骤列为同组OR词项。terms只放能在检索中替代同一概念的表达，alias只放已知同义词或简称；没有可靠别名返回空数组，不为凑数量生成别名。
  仅在needs_translation为true时生成来源语言comparison。遵守输入数量与词项限制，覆盖核心机制。
  有semantic_contract时保留其技术事实的动作方向和组成关系，不能把去除某机制写成使用该机制。使用冻结判据覆盖核心事实，不改写判据。
---
任务输入
{payload_json}

输出JSON契约
{schema_json}
