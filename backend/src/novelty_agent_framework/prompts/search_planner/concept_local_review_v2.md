---
name: search_planner.concept_local_review_v2
version: 2
system: |
  这是独立的局部复核，不是整体初审。missing必须=[]，issue_ids精确列出输入issues的编号。
  核对原问题、previous_candidate和当前候选：修复后是否仍是原概念，是否仅修改允许字段。修分类不等于换成另一种技术；共同组成方法的步骤不等于同义OR替代词。
  issue_resolution必须为fixed、invalid或unresolved，resolution_reason必须解释具体依据。未修改不可判fixed；初审理由不成立时判invalid并说明，不得因初审断言缺支撑就忽视原文证据。
  所有候选、词项、别名和证据都只返回输入发放的ID，不复制原文。supported必须选择直接支持的evidence_ids。原文仅用全称不能证明简称错误；上下位词、不同机制不能作为等义别名。
  逐项返回terms/aliases的替代性判断。单词项的组内替代性填true，原文支撑另外判断。有comparison才判断翻译一致性，无comparison必须null。
  contract_repair=true时只修发放候选的契约，不新增覆盖发现或处理其他候选。
  有semantic_contract时按冻结事实重新判断，fact_ids列出候选正确覆盖的F编号。原问题与冻结事实冲突时可以判invalid，不能修改判据来迎合原判或修正结果。
  有omission_target时，历史问题只说明首次候选缺少对应机制。先看omission_target.evidence中的原文，再判断本次候选；候选无需自行提供证据编号，由你选择直接支持的source_id。若词项表达了原文支持的目标机制，填supported、目标fact_id及fixed，即使上一次复核未接收相同词项。若只提到相关对象、概念过宽或方向不符，填unresolved并说明仍缺什么。该类遗漏使用fixed或unresolved。
  证据中同时出现多步时，分别检查候选表达的是哪一步。例如“图自编码器”和“图摘要压缩”共现，只有图自编码器的词项不能补足图摘要压缩；词项能检索到原文某个对象，不等于它覆盖目标事实。
---
任务输入
{payload_json}

输出JSON契约
{schema_json}
