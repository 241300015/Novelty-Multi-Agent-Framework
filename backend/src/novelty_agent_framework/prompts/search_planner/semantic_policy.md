---
name: search_planner.semantic_policy
version: 1
system: |
  只依据原始输入和已发放事实F编号制定三档最低要求，不参考后续生成结果。每档requirements列表为AND关系，每个any_of内部为OR关系。
  strict应逐项要求每个defining技术机制。medium可保留主要机制组合，省略部分细节。broad可以用多个有辨识力的机制组覆盖替代路径，不需要要求strict全部条件。
  每档至少有一个由defining技术机制组成的必要组，不能只要求模型名字、数据集、泛称方法或研究目标。只能引用输入中发放的F编号，不创造事实。
  defining事实并不表示它在所有档位都必须出现；应判断去掉它之后剩余组合是否仍能识别本任务。给出relaxation_reason和每组的技术理由，不机械限制条件数量。
  strict的每个defining事实必须各占一个单元素any_of组，不能把全部defining放入同一个OR组冒充同时覆盖。medium/broad不能把supporting细节作为必须条件。只输出strict、medium、broad三个数据字段，不输出JSON schema或$defs。
---
任务输入
{payload_json}

输出JSON契约
{schema_json}
