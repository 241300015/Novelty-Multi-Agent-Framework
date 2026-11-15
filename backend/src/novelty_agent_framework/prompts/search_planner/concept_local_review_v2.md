---
name: search_planner.concept_local_review_v2
version: 1
system: |
  这是独立的局部复核，不是整体初审。missing必须=[]，issue_ids精确列出输入issues的编号。
  核对原问题、previous_candidate和当前候选：修复后是否仍是原概念，是否仅修改允许字段。修分类不等于换成另一种技术；共同组成方法的步骤不等于同义OR替代词。
  issue_resolution必须为fixed、invalid或unresolved，resolution_reason必须解释具体依据。未修改不可判fixed；初审理由不成立时判invalid并说明，不得因初审断言缺支撑就忽视原文证据。
  所有候选、词项、别名和证据都只返回输入发放的ID，不复制原文。supported必须选择直接支持的evidence_ids。原文仅用全称不能证明简称错误；上下位词、不同机制不能作为等义别名。
  逐项返回terms/aliases的替代性判断。单词项的组内替代性填true，原文支撑另外判断。有comparison才判断翻译一致性，无comparison必须null。
  contract_repair=true时只修发放候选的契约，不新增覆盖发现或处理其他候选。
---
任务输入
{payload_json}

输出JSON契约
{schema_json}
